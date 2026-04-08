// Package relay implements a WebSocket relay client that connects the edge
// daemon to the cloud backend's relay endpoint. When the backend cannot reach
// the edge via direct gRPC (e.g. behind NAT, no Tailscale), it sends
// instrument commands through this WebSocket tunnel instead.
//
// Protocol:
//
//	Edge → Backend: hello, heartbeat, command_response
//	Backend → Edge: command_request
//
// The client maintains a persistent connection with automatic reconnect and
// exponential backoff.
package relay

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"math"
	"math/rand/v2"
	"net/http"
	"sync"
	"time"

	edgepb "github.com/galois-labs/edge/proto/gen/go/edge/v1"
	"github.com/gorilla/websocket"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const (
	// heartbeatInterval is how often the edge sends a heartbeat to the relay.
	heartbeatInterval = 30 * time.Second

	// backoffInitial is the starting reconnect delay.
	backoffInitial = 2 * time.Second

	// backoffMax is the ceiling for exponential backoff.
	backoffMax = 5 * time.Minute

	// backoffJitter is the fraction of random jitter added to backoff
	// delays (up to 25% extra).
	backoffJitter = 0.25

	// grpcCallTimeout is the per-command timeout for local gRPC calls to
	// the Python daemon.
	grpcCallTimeout = 30 * time.Second

	// wsWriteWait is the write deadline applied to each WebSocket write.
	wsWriteWait = 10 * time.Second
)

// ---------------------------------------------------------------------------
// JSON message types — mirrors the cloud backend relay.RelayMessage
// ---------------------------------------------------------------------------

// relayMessage is the JSON envelope for all relay WebSocket messages.
type relayMessage struct {
	Type string `json:"type"`

	// hello fields (edge → backend)
	EdgeID   string `json:"edge_id,omitempty"`
	EdgeName string `json:"edge_name,omitempty"`
	Version  string `json:"version,omitempty"`

	// heartbeat fields (edge → backend)
	TimestampMs int64 `json:"timestamp_ms,omitempty"`

	// command_request fields (backend → edge)
	RequestID    string            `json:"request_id,omitempty"`
	InstrumentID string            `json:"instrument_id,omitempty"`
	CommandName  string            `json:"command_name,omitempty"`
	Parameters   map[string]string `json:"parameters,omitempty"`
	IsQuery      bool              `json:"is_query,omitempty"`

	// command_response fields (edge → backend)
	Success         bool   `json:"success,omitempty"`
	Data            string `json:"data,omitempty"`
	ErrorMessage    string `json:"error_message,omitempty"`
	ScpiCommand     string `json:"scpi_command,omitempty"`
	ExecutionTimeMs int64  `json:"execution_time_ms,omitempty"`
}

// ---------------------------------------------------------------------------
// Client
// ---------------------------------------------------------------------------

// Client is a WebSocket relay client that connects to the cloud backend and
// forwards instrument commands to the local Python gRPC server.
type Client struct {
	edgeID     string
	edgeName   string
	version    string
	backendURL string // e.g. "ws://cloud.galoislabs.ai:8000/api/v1/relay/ws"
	authToken  string // API key
	localGRPC  string // "127.0.0.1:50052" — the local Python daemon

	logger *slog.Logger
}

// NewClient creates a new relay Client.
//
// Parameters:
//   - edgeID: the edge daemon's UUID (from registration)
//   - edgeName: human-readable edge name
//   - version: daemon version string
//   - backendURL: WebSocket URL to the relay endpoint (ws:// or wss://)
//   - authToken: API key for authentication
//   - localGRPCAddr: address of the local Python gRPC server (e.g. "127.0.0.1:50052")
//   - logger: structured logger
func NewClient(edgeID, edgeName, version, backendURL, authToken, localGRPCAddr string, logger *slog.Logger) *Client {
	return &Client{
		edgeID:     edgeID,
		edgeName:   edgeName,
		version:    version,
		backendURL: backendURL,
		authToken:  authToken,
		localGRPC:  localGRPCAddr,
		logger: logger.With(
			"component", "relay",
		),
	}
}

// Run is the main loop. It connects to the backend WebSocket, sends hello,
// starts the heartbeat, and processes incoming command requests. On disconnect
// it reconnects with exponential backoff. Run blocks until ctx is cancelled.
func (c *Client) Run(ctx context.Context) {
	attempts := 0

	for {
		// Check if context is done before attempting connection.
		select {
		case <-ctx.Done():
			c.logger.Info("relay client shutting down")
			return
		default:
		}

		// Apply backoff before reconnect (skip on first attempt).
		if attempts > 0 {
			delay := BackoffDelay(attempts)
			c.logger.Info("waiting before reconnect",
				"delay", delay,
				"attempt", attempts,
			)
			select {
			case <-time.After(delay):
			case <-ctx.Done():
				c.logger.Info("relay client shutting down during backoff")
				return
			}
		}

		err := c.connectAndServe(ctx)
		if err != nil && ctx.Err() == nil {
			c.logger.Warn("relay connection lost", "error", err)
		}

		// If context was cancelled, exit without incrementing.
		if ctx.Err() != nil {
			c.logger.Info("relay client shutting down")
			return
		}

		attempts++
	}
}

// connectAndServe performs a single WebSocket session: connect, hello,
// heartbeat, read loop. Returns when the connection is lost or ctx is
// cancelled. On successful hello + read loop entry, the backoff counter
// is conceptually "reset" (handled by the caller).
func (c *Client) connectAndServe(ctx context.Context) error {
	// Build the dial URL with auth token.
	dialURL := c.backendURL + "?token=" + c.authToken

	c.logger.Info("connecting to relay",
		"url", c.backendURL,
	)

	// Dial the WebSocket.
	dialer := websocket.Dialer{
		HandshakeTimeout: 15 * time.Second,
	}
	ws, resp, err := dialer.DialContext(ctx, dialURL, http.Header{})
	if err != nil {
		if resp != nil {
			return fmt.Errorf("websocket dial failed (status %d): %w", resp.StatusCode, err)
		}
		return fmt.Errorf("websocket dial failed: %w", err)
	}
	defer ws.Close()

	c.logger.Info("connected to relay")

	// Mutex for thread-safe WebSocket writes.
	var writeMu sync.Mutex

	// Send hello message.
	hello := relayMessage{
		Type:     "hello",
		EdgeID:   c.edgeID,
		EdgeName: c.edgeName,
		Version:  c.version,
	}
	if err := c.writeJSON(&writeMu, ws, &hello); err != nil {
		return fmt.Errorf("send hello: %w", err)
	}
	c.logger.Info("sent hello to relay")

	// Start heartbeat goroutine.
	heartbeatCtx, heartbeatCancel := context.WithCancel(ctx)
	defer heartbeatCancel()
	go c.heartbeatLoop(heartbeatCtx, &writeMu, ws)

	// Read loop — process messages from the backend.
	for {
		select {
		case <-ctx.Done():
			// Send close frame.
			c.writeClose(&writeMu, ws)
			return ctx.Err()
		default:
		}

		var msg relayMessage
		if err := ws.ReadJSON(&msg); err != nil {
			if websocket.IsUnexpectedCloseError(err, websocket.CloseGoingAway, websocket.CloseNormalClosure) {
				return fmt.Errorf("read error: %w", err)
			}
			return fmt.Errorf("connection closed: %w", err)
		}

		switch msg.Type {
		case "command_request":
			c.logger.Info("received command request",
				"request_id", msg.RequestID,
				"instrument_id", msg.InstrumentID,
				"command", msg.CommandName,
				"is_query", msg.IsQuery,
			)
			// Handle each command in its own goroutine.
			go c.handleCommand(ctx, &writeMu, ws, msg)

		default:
			c.logger.Debug("ignoring unknown message type", "type", msg.Type)
		}
	}
}

// heartbeatLoop sends periodic heartbeat messages over the WebSocket.
func (c *Client) heartbeatLoop(ctx context.Context, mu *sync.Mutex, ws *websocket.Conn) {
	ticker := time.NewTicker(heartbeatInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			hb := relayMessage{
				Type:        "heartbeat",
				TimestampMs: time.Now().UnixMilli(),
			}
			if err := c.writeJSON(mu, ws, &hb); err != nil {
				c.logger.Warn("failed to send heartbeat", "error", err)
				return
			}
			c.logger.Debug("sent heartbeat")
		}
	}
}

// handleCommand processes a single command_request: forwards it to the local
// Python gRPC server and sends the result back as a command_response.
func (c *Client) handleCommand(ctx context.Context, mu *sync.Mutex, ws *websocket.Conn, req relayMessage) {
	start := time.Now()

	// Dial the local Python gRPC server (fresh connection per command to
	// avoid stale connections if Python restarts).
	conn, err := grpc.NewClient(
		c.localGRPC,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		c.sendErrorResponse(mu, ws, req.RequestID, "", 0,
			fmt.Sprintf("failed to connect to local gRPC: %v", err))
		return
	}
	defer conn.Close()

	svc := edgepb.NewEdgeDaemonServiceClient(conn)

	// Build the ExecuteCommand request.
	grpcReq := &edgepb.ExecuteCommandRequest{
		CommandId:    req.RequestID,
		InstrumentId: req.InstrumentID,
		CommandName:  req.CommandName,
		Parameters:   req.Parameters,
		IsQuery:      req.IsQuery,
	}

	// Call ExecuteCommand with a timeout.
	callCtx, cancel := context.WithTimeout(ctx, grpcCallTimeout)
	defer cancel()

	grpcResp, err := svc.ExecuteCommand(callCtx, grpcReq)
	elapsed := time.Since(start).Milliseconds()

	if err != nil {
		c.logger.Warn("gRPC ExecuteCommand failed",
			"request_id", req.RequestID,
			"error", err,
		)
		c.sendErrorResponse(mu, ws, req.RequestID, "", elapsed,
			fmt.Sprintf("gRPC error: %v", err))
		return
	}

	// Build and send the command_response.
	resp := relayMessage{
		Type:            "command_response",
		RequestID:       req.RequestID,
		Success:         grpcResp.GetSuccess(),
		Data:            grpcResp.GetData(),
		ErrorMessage:    grpcResp.GetErrorMessage(),
		ScpiCommand:     grpcResp.GetScpiCommand(),
		ExecutionTimeMs: grpcResp.GetExecutionTimeMs(),
	}

	if err := c.writeJSON(mu, ws, &resp); err != nil {
		c.logger.Warn("failed to send command_response",
			"request_id", req.RequestID,
			"error", err,
		)
		return
	}

	c.logger.Info("sent command response",
		"request_id", req.RequestID,
		"success", grpcResp.GetSuccess(),
		"execution_time_ms", grpcResp.GetExecutionTimeMs(),
	)
}

// sendErrorResponse sends a command_response with success=false.
func (c *Client) sendErrorResponse(mu *sync.Mutex, ws *websocket.Conn, requestID, scpiCmd string, elapsedMs int64, errMsg string) {
	resp := relayMessage{
		Type:            "command_response",
		RequestID:       requestID,
		Success:         false,
		ErrorMessage:    errMsg,
		ScpiCommand:     scpiCmd,
		ExecutionTimeMs: elapsedMs,
	}
	if err := c.writeJSON(mu, ws, &resp); err != nil {
		c.logger.Warn("failed to send error response",
			"request_id", requestID,
			"error", err,
		)
	}
}

// writeJSON writes a JSON message to the WebSocket with proper locking and
// write deadline.
func (c *Client) writeJSON(mu *sync.Mutex, ws *websocket.Conn, msg *relayMessage) error {
	mu.Lock()
	defer mu.Unlock()
	ws.SetWriteDeadline(time.Now().Add(wsWriteWait))
	return ws.WriteJSON(msg)
}

// writeClose sends a WebSocket close frame.
func (c *Client) writeClose(mu *sync.Mutex, ws *websocket.Conn) {
	mu.Lock()
	defer mu.Unlock()
	ws.SetWriteDeadline(time.Now().Add(wsWriteWait))
	ws.WriteMessage(websocket.CloseMessage,
		websocket.FormatCloseMessage(websocket.CloseNormalClosure, ""))
}

// ---------------------------------------------------------------------------
// Exponential backoff
// ---------------------------------------------------------------------------

// BackoffDelay calculates the backoff duration for the given attempt number.
// Formula: min(initial * 2^(attempts-1), max) * (1 + rand(0, jitter))
//
// Examples (before jitter):
//
//	attempt 1 -> 2s
//	attempt 2 -> 4s
//	attempt 3 -> 8s
//	attempt 4 -> 16s
//	...
//	attempt 8+ -> 5min (capped)
func BackoffDelay(attempts int) time.Duration {
	if attempts < 1 {
		attempts = 1
	}
	base := float64(backoffInitial) * math.Pow(2, float64(attempts-1))
	capped := math.Min(base, float64(backoffMax))
	jittered := capped * (1.0 + rand.Float64()*backoffJitter)
	return time.Duration(jittered)
}

// BackoffDelayDeterministic calculates the backoff duration without jitter.
// Used for testing.
func BackoffDelayDeterministic(attempts int) time.Duration {
	if attempts < 1 {
		attempts = 1
	}
	base := float64(backoffInitial) * math.Pow(2, float64(attempts-1))
	capped := math.Min(base, float64(backoffMax))
	return time.Duration(capped)
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// MarshalMessage serializes a relayMessage to JSON. Exported for testing.
func MarshalMessage(msg *relayMessage) ([]byte, error) {
	return json.Marshal(msg)
}

// UnmarshalMessage deserializes a relayMessage from JSON. Exported for testing.
func UnmarshalMessage(data []byte) (*relayMessage, error) {
	var msg relayMessage
	err := json.Unmarshal(data, &msg)
	return &msg, err
}
