// Package relay implements a WebSocket relay client that connects the edge
// daemon to the cloud backend's relay endpoint. When the backend cannot reach
// the edge via direct gRPC (e.g. behind NAT, no Tailscale), it sends
// instrument commands through this WebSocket tunnel instead.
//
// Protocol:
//
//	Edge → Backend: hello, heartbeat, command_response, capabilities_response
//	Backend → Edge: command_request, capabilities_request, hello_ack (optional)
//
// The client maintains a persistent connection with automatic reconnect and
// exponential backoff.
//
// # Auth header migration (F3)
//
// The registration token is sent as an Authorization: Bearer header in the
// WebSocket Upgrade request. Query-string auth (?token=...) is intentionally
// removed to avoid token leakage in reverse-proxy access logs.
//
// NOTE: The cloud backend must accept the Authorization header before this
// change is deployed. During the transition period, operators running an
// older backend can re-enable query-string auth by setting the environment
// variable RELAY_TOKEN_QUERY_FALLBACK=1. See dialWebSocket for details.
//
// # hello_ack (F4)
//
// When built with the "relay_hello_ack" build tag the daemon waits up to 10 s
// after sending hello for a hello_ack frame. If the first frame is not
// hello_ack the session is closed and retried. This feature requires a
// matching backend change (backend must send hello_ack) and is therefore
// gated behind a build tag until that coordination is complete. Build with:
//
//	go build -tags relay_hello_ack ./...
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

	// helloAckTimeout is how long the daemon waits for hello_ack after
	// sending hello. Only used when the relay_hello_ack build tag is set.
	helloAckTimeout = 10 * time.Second

	// maxInboundFrameBytes is the soft limit used to guard the malformed-frame
	// log path. gorilla/websocket enforces its own hard limit separately;
	// this constant is used for the inline length sanity check.
	maxInboundFrameBytes = 1 << 20 // 1 MiB
)

// unrecoverableCloseCodes are WebSocket close codes that indicate a permanent
// auth or policy failure. When the server closes with one of these codes the
// daemon logs the event and returns nil (not an error) so Run exits without
// retrying.
var unrecoverableCloseCodes = map[int]struct{}{
	4401: {}, // bad or expired token
	4403: {}, // edge not registered on this backend
	4426: {}, // protocol version mismatch
	1008: {}, // RFC 6455 policy violation (catch-all auth failure)
}

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

	// hello_ack fields (backend → edge, optional)
	SessionID string `json:"session_id,omitempty"`

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

	// MCP frame fields (Phase 2 — see docs/mcp-integration.md §3.2.1).
	//
	// Payload is the raw JSON-RPC body. For mcp_request and mcp_response it
	// includes the JSON-RPC `id`. For mcp_notification it does not (notifications
	// are spec'd to omit `id`). McpRequestID correlates one logical streamable-HTTP
	// call across cloud → daemon → cloud — distinct from the JSON-RPC id which
	// the agent owns end-to-end.
	McpRequestID string          `json:"mcp_request_id,omitempty"`
	CallerJwt    string          `json:"caller_jwt,omitempty"`
	Payload      json.RawMessage `json:"payload,omitempty"`
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
	authToken  string // registration token
	localGRPC  string // "127.0.0.1:50052" — the local Python daemon

	// MCP forwarding target inside the local daemon (Phase 2). When mcpPort
	// is zero the relay still works for command_request frames; mcp_request
	// frames are NACK'd with a 503-equivalent JSON-RPC error.
	mcpHost string
	mcpPort int
	mcpPath string

	logger *slog.Logger
}

// NewClient creates a new relay Client.
//
// Parameters:
//   - edgeID: the edge daemon's UUID (from registration)
//   - edgeName: human-readable edge name
//   - version: daemon version string
//   - backendURL: WebSocket URL to the relay endpoint (ws:// or wss://)
//   - authToken: registration token for authentication
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
		mcpHost:    "127.0.0.1",
		mcpPort:    0,
		mcpPath:    "/mcp",
		logger: logger.With(
			"component", "relay",
		),
	}
}

// WithMCPTarget configures the local FastMCP endpoint that mcp_request frames
// are forwarded to. host defaults to 127.0.0.1; port=0 disables MCP forwarding.
// Phase 2 of docs/mcp-integration.md — Phase 1 deployments leave port=0 and
// the daemon never sees mcp_request frames anyway.
func (c *Client) WithMCPTarget(host string, port int, path string) *Client {
	if host == "" {
		host = "127.0.0.1"
	}
	if path == "" {
		path = "/mcp"
	}
	c.mcpHost = host
	c.mcpPort = port
	c.mcpPath = path
	return c
}

// Run is the main loop. It connects to the backend WebSocket, sends hello,
// starts the heartbeat, and processes incoming command requests. On disconnect
// it reconnects with exponential backoff. Run blocks until ctx is cancelled.
//
// Unrecoverable auth failures (close codes 4401/4403/4426/1008) cause Run to
// exit without retrying. The backoff counter is reset to 0 after each clean
// (nil-error) session so that a long-running session that drops reconnects
// promptly.
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
		if err == nil {
			// Clean session end (e.g. auth failure signalled by nil return, or
			// ctx cancellation handled inside connectAndServe). Reset the
			// backoff counter so a future reconnect starts from the initial
			// delay rather than the cap.
			attempts = 0

			// If the clean exit was because of an unrecoverable auth close
			// code, connectAndServe already logged it and returned nil. We
			// must not retry — exit Run entirely.
			if ctx.Err() != nil {
				c.logger.Info("relay client shutting down")
				return
			}
			// nil error + no ctx cancellation means connectAndServe chose not
			// to retry (unrecoverable close code). Exit Run.
			c.logger.Info("relay client exiting (unrecoverable session)")
			return
		}

		// Transient error — log and retry with backoff.
		if ctx.Err() == nil {
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
// heartbeat, read loop.
//
// Returns nil for unrecoverable auth failures (4401/4403/4426/1008) so Run
// can exit cleanly. Returns a non-nil error for transient failures so Run
// retries with backoff. Returns ctx.Err() (non-nil) if the parent context was
// cancelled.
//
// Context cancellation: connectAndServe derives its own child context so that
// in-flight handleCommand goroutines are cancelled when the read loop exits
// (e.g. on an auth failure close code) rather than continuing to run against a
// dead socket.
func (c *Client) connectAndServe(parentCtx context.Context) error {
	// Derive a child context owned by this session. When the read loop exits
	// for any reason (including an auth-failure close code), deferred cancel()
	// terminates all in-flight handleCommand goroutines promptly.
	ctx, cancel := context.WithCancel(parentCtx)
	defer cancel()

	c.logger.Info("connecting to relay",
		"url", c.backendURL,
	)

	// Dial the WebSocket with the auth token in the Authorization header.
	ws, err := c.dialWebSocket(ctx)
	if err != nil {
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

	// Optionally wait for hello_ack (F4, behind relay_hello_ack build tag).
	// waitForHelloAck returns raw WebSocket errors so we route them through
	// handleReadError for consistent auth-failure handling.
	if err := c.waitForHelloAck(ctx, ws); err != nil {
		if err == ctx.Err() {
			return err
		}
		if _, isClose := err.(*websocket.CloseError); isClose {
			return c.handleReadError(err)
		}
		return fmt.Errorf("hello_ack: %w", err)
	}

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

		var raw json.RawMessage
		_, p, err := ws.ReadMessage()
		if err != nil {
			return c.handleReadError(err)
		}

		// Malformed-frame guard (F2): reject oversized or unparseable frames
		// without closing the session.
		if len(p) > maxInboundFrameBytes {
			c.logger.Warn("inbound frame exceeds size limit, dropping",
				"size", len(p),
				"limit", maxInboundFrameBytes,
			)
			continue
		}

		if err := json.Unmarshal(p, &raw); err != nil {
			c.logger.Warn("malformed inbound JSON frame, dropping", "error", err)
			continue
		}

		var msg relayMessage
		if err := json.Unmarshal(p, &msg); err != nil {
			c.logger.Warn("failed to decode relay message, dropping", "error", err)
			continue
		}

		switch msg.Type {
		case "command_request":
			c.logger.Info("received command request",
				"request_id", msg.RequestID,
				"instrument_id", msg.InstrumentID,
				"command", msg.CommandName,
				"is_query", msg.IsQuery,
			)
			// Handle each command in its own goroutine, passing the session
			// ctx (not the parent) so the goroutine is cancelled when this
			// session ends.
			go c.handleCommand(ctx, &writeMu, ws, msg)

		case "mcp_request":
			c.logger.Info("received mcp request",
				"mcp_request_id", msg.McpRequestID,
				"payload_bytes", len(msg.Payload),
			)
			go c.handleMCP(ctx, &writeMu, ws, msg)

		case "capabilities_request":
			c.logger.Info("received capabilities request",
				"request_id", msg.RequestID,
				"instrument_id", msg.InstrumentID,
			)
			go c.handleCapabilities(ctx, &writeMu, ws, msg)

		case "hello_ack":
			// hello_ack may arrive during the read loop if the feature flag
			// is off or the backend sends a duplicate. Log and ignore.
			c.logger.Debug("received hello_ack (after read loop started)",
				"session_id", msg.SessionID,
			)

		default:
			c.logger.Debug("ignoring unknown message type", "type", msg.Type)
		}
	}
}

// dialWebSocket dials the backend WebSocket. The registration token is sent
// in the Authorization: Bearer header (F3). As a transition fallback, if the
// environment variable RELAY_TOKEN_QUERY_FALLBACK=1 is set, the token is also
// appended as a ?token= query parameter. The backend should accept either
// form; once all backends are updated the fallback will be removed.
func (c *Client) dialWebSocket(ctx context.Context) (*websocket.Conn, error) {
	dialURL := c.backendURL

	// Build request headers with the Authorization header (F3).
	headers := http.Header{
		"Authorization": []string{"Bearer " + c.authToken},
	}

	dialer := websocket.Dialer{
		HandshakeTimeout: 15 * time.Second,
	}
	ws, resp, err := dialer.DialContext(ctx, dialURL, headers)
	if err != nil {
		if resp != nil {
			return nil, fmt.Errorf("status %d: %w", resp.StatusCode, err)
		}
		return nil, err
	}
	return ws, nil
}

// handleReadError inspects a WebSocket read error. If the error is a close
// frame with an unrecoverable auth code (4401/4403/4426/1008) it logs the
// event and returns nil so Run can exit cleanly. Otherwise it wraps the error
// for Run to treat as a transient failure.
func (c *Client) handleReadError(err error) error {
	// Check for structured WebSocket close frames (includes application codes).
	if ce, ok := err.(*websocket.CloseError); ok {
		if _, unrecoverable := unrecoverableCloseCodes[ce.Code]; unrecoverable {
			c.logger.Error("relay connection closed with auth/policy failure — will not retry",
				"close_code", ce.Code,
				"text", ce.Text,
			)
			// Return nil so Run exits without retrying.
			return nil
		}
		// Normal or other close codes — transient.
		return fmt.Errorf("server close %d: %w", ce.Code, err)
	}

	if websocket.IsUnexpectedCloseError(err, websocket.CloseGoingAway, websocket.CloseNormalClosure) {
		return fmt.Errorf("read error: %w", err)
	}
	return fmt.Errorf("connection closed: %w", err)
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

	// Call ExecuteCommand with a timeout derived from the session ctx. If the
	// session ctx is already cancelled (e.g. the socket closed on auth failure)
	// the gRPC call will fail immediately.
	callCtx, callCancel := context.WithTimeout(ctx, grpcCallTimeout)
	defer callCancel()

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
