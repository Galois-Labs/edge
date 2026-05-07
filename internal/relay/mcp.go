// MCP-over-relay support for the daemon-side relay client.
//
// Phase 2 of docs/mcp-integration.md generalizes the relay envelope to carry
// streamable-HTTP MCP traffic. handleMCP is the dispatch sibling of
// handleCommand: it forwards an mcp_request body to the local FastMCP HTTP
// endpoint and streams the response back as one or more mcp_notification
// frames followed by a final mcp_response.
//
// Wire correspondence (one logical agent call):
//
//	mcp_request                      cloud → daemon  (JSON-RPC body)
//	mcp_notification (0..N)          daemon → cloud  (one per SSE event)
//	mcp_response                     daemon → cloud  (final body, terminates stream)
//
// The local FastMCP returns either application/json (unary) or
// text/event-stream (streaming). We branch on Content-Type and never buffer
// the whole SSE stream: each event is parsed and forwarded as it arrives.

package relay

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

// mcpHTTPTimeout caps the total local HTTP call. Streamable-HTTP responses
// can legitimately last for many seconds (long-running tools, batched
// notifications) so this is generous compared to grpcCallTimeout.
const mcpHTTPTimeout = 5 * time.Minute

// mcpHTTPClient is shared across handleMCP invocations to amortize TCP/TLS
// setup. Localhost-only so no TLS, but reusing connections still helps.
var mcpHTTPClient = &http.Client{
	Timeout: mcpHTTPTimeout,
	Transport: &http.Transport{
		// Bound the connection pool — FastMCP is single-process, no point
		// in keeping more than a handful of idle conns alive.
		MaxIdleConns:        4,
		MaxIdleConnsPerHost: 4,
		IdleConnTimeout:     30 * time.Second,
		// Disable HTTP/2 here: streamable-HTTP responses are SSE, and
		// HTTP/1.1 chunked transfer is the documented path. HTTP/2 would
		// also introduce client-side multiplexing complexity we don't need
		// for a localhost target.
		ForceAttemptHTTP2: false,
	},
}

// handleMCP forwards a single mcp_request to the local FastMCP HTTP endpoint
// and emits the corresponding mcp_notification + mcp_response frames on the
// relay WebSocket.
//
// Errors at any stage are surfaced as a synthetic mcp_response carrying a
// JSON-RPC error envelope. We never silently drop a request — the cloud's
// pendingMCP map is keyed on McpRequestID and callers will block forever if
// no response arrives.
func (c *Client) handleMCP(ctx context.Context, mu *sync.Mutex, ws *websocket.Conn, req relayMessage) {
	if c.mcpPort == 0 {
		c.sendMCPError(mu, ws, req.McpRequestID, req.Payload, -32603,
			"MCP forwarding not configured on this daemon")
		return
	}

	url := fmt.Sprintf("http://%s:%d%s", c.mcpHost, c.mcpPort, c.mcpPath)

	body := io.NopCloser(bytes.NewReader([]byte(req.Payload)))
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, url, body)
	if err != nil {
		c.sendMCPError(mu, ws, req.McpRequestID, req.Payload, -32603,
			fmt.Sprintf("build local MCP request: %v", err))
		return
	}
	httpReq.Header.Set("Content-Type", "application/json")
	// Streamable-HTTP clients announce both response shapes; the server
	// chooses one. FastMCP keys on this header.
	httpReq.Header.Set("Accept", "application/json, text/event-stream")
	if req.CallerJwt != "" {
		// The Python middleware reads this header and places the validated
		// claims onto the FastMCP call context. See
		// src/galois_edge/mcp/auth.py.
		httpReq.Header.Set("Galois-Caller-JWT", req.CallerJwt)
	}

	resp, err := mcpHTTPClient.Do(httpReq)
	if err != nil {
		c.sendMCPError(mu, ws, req.McpRequestID, req.Payload, -32603,
			fmt.Sprintf("local MCP call failed: %v", err))
		return
	}
	defer resp.Body.Close()

	contentType := resp.Header.Get("Content-Type")
	if strings.HasPrefix(contentType, "text/event-stream") {
		c.streamMCPSSE(mu, ws, req.McpRequestID, resp.Body)
		return
	}

	// Unary JSON path.
	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		c.sendMCPError(mu, ws, req.McpRequestID, req.Payload, -32603,
			fmt.Sprintf("read local MCP response: %v", err))
		return
	}
	if resp.StatusCode >= 400 {
		// FastMCP returns a JSON-RPC error body on most failure paths, so
		// passing it through verbatim is correct. If the body is empty,
		// synthesize a generic one.
		if len(bytes.TrimSpace(respBody)) == 0 {
			c.sendMCPError(mu, ws, req.McpRequestID, req.Payload, -32603,
				fmt.Sprintf("local MCP returned status %d", resp.StatusCode))
			return
		}
	}

	out := relayMessage{
		Type:         "mcp_response",
		McpRequestID: req.McpRequestID,
		Payload:      json.RawMessage(respBody),
	}
	if err := c.writeJSON(mu, ws, &out); err != nil {
		c.logger.Warn("failed to send mcp_response",
			"mcp_request_id", req.McpRequestID,
			"error", err,
		)
	}
}

// streamMCPSSE reads SSE events from body and emits each as one
// mcp_notification frame on the WS. The terminating event (per the MCP
// streamable-HTTP transport spec, the final JSON-RPC response is sent on the
// stream as a regular event with the JSON-RPC `id` set; everything before is
// notifications without an id) is forwarded as an mcp_response.
//
// We follow the MCP wire convention rather than guessing from event names:
// any SSE data payload that parses as JSON-RPC and contains a non-null `id`
// is the response; anything else (notifications/progress, etc.) becomes
// mcp_notification. This matches FastMCP's emission order.
func (c *Client) streamMCPSSE(mu *sync.Mutex, ws *websocket.Conn, mcpRequestID string, body io.Reader) {
	scanner := bufio.NewScanner(body)
	// SSE events can be larger than the default 64KB scanner buffer (e.g.
	// a tools/list response with many dynamic tools). Bump the cap.
	scanner.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)

	var dataLines []string
	flush := func() {
		if len(dataLines) == 0 {
			return
		}
		raw := strings.Join(dataLines, "\n")
		dataLines = dataLines[:0]
		// Decide notification vs response by inspecting the JSON-RPC id.
		isResponse := jsonHasNonNullID([]byte(raw))
		frameType := "mcp_notification"
		if isResponse {
			frameType = "mcp_response"
		}
		out := relayMessage{
			Type:         frameType,
			McpRequestID: mcpRequestID,
			Payload:      json.RawMessage(raw),
		}
		if err := c.writeJSON(mu, ws, &out); err != nil {
			c.logger.Warn("failed to forward SSE frame",
				"mcp_request_id", mcpRequestID,
				"frame_type", frameType,
				"error", err,
			)
		}
	}

	for scanner.Scan() {
		line := scanner.Text()
		if line == "" {
			// SSE event boundary.
			flush()
			continue
		}
		// SSE field syntax: "field: value". We only care about `data:`.
		// `event:`, `id:`, and `retry:` are ignored — the JSON-RPC body is
		// the source of truth for routing.
		if strings.HasPrefix(line, ":") {
			continue
		}
		if strings.HasPrefix(line, "data:") {
			value := strings.TrimPrefix(line, "data:")
			value = strings.TrimPrefix(value, " ")
			dataLines = append(dataLines, value)
		}
	}
	// Final flush in case the stream ended without a trailing blank line.
	flush()

	if err := scanner.Err(); err != nil {
		c.logger.Warn("SSE scanner error", "mcp_request_id", mcpRequestID, "error", err)
	}
}

// jsonHasNonNullID returns true if data is a JSON object containing an `id`
// field whose value is not null. This matches the JSON-RPC convention for
// distinguishing responses (have id) from notifications (no id, or null id).
//
// We do a lightweight unmarshal into a sentinel struct rather than full
// parsing; the SSE event payloads are small (≤ a few KB typically) and this
// is on the hot path.
func jsonHasNonNullID(data []byte) bool {
	type idCheck struct {
		ID json.RawMessage `json:"id"`
	}
	var ic idCheck
	if err := json.Unmarshal(data, &ic); err != nil {
		return false
	}
	if len(ic.ID) == 0 {
		return false
	}
	trimmed := bytes.TrimSpace(ic.ID)
	if bytes.Equal(trimmed, []byte("null")) {
		return false
	}
	return true
}

// sendMCPError builds a JSON-RPC error response and writes it as an
// mcp_response frame. The `id` is preserved from the inbound request when
// possible so the cloud can correlate the failure to the original call.
func (c *Client) sendMCPError(mu *sync.Mutex, ws *websocket.Conn, mcpRequestID string, requestPayload json.RawMessage, code int, message string) {
	id := extractJSONRPCID(requestPayload)
	errBody := map[string]any{
		"jsonrpc": "2.0",
		"id":      id,
		"error": map[string]any{
			"code":    code,
			"message": message,
		},
	}
	encoded, err := json.Marshal(errBody)
	if err != nil {
		// Fall back to a hand-rolled body — should never fail.
		encoded = []byte(`{"jsonrpc":"2.0","id":null,"error":{"code":-32603,"message":"internal error"}}`)
	}
	out := relayMessage{
		Type:         "mcp_response",
		McpRequestID: mcpRequestID,
		Payload:      json.RawMessage(encoded),
	}
	if werr := c.writeJSON(mu, ws, &out); werr != nil {
		c.logger.Warn("failed to send mcp error",
			"mcp_request_id", mcpRequestID,
			"error", werr,
		)
	}
}

// extractJSONRPCID returns the JSON-RPC `id` field as a raw JSON value, or
// json.RawMessage("null") if the payload doesn't have one. The cloud's
// pendingMCP map is keyed on mcp_request_id (separate from the JSON-RPC id),
// so this is purely a courtesy for the upstream client.
func extractJSONRPCID(payload json.RawMessage) any {
	type idCheck struct {
		ID json.RawMessage `json:"id"`
	}
	var ic idCheck
	if len(payload) == 0 {
		return nil
	}
	if err := json.Unmarshal(payload, &ic); err != nil || len(ic.ID) == 0 {
		return nil
	}
	// Decode into an interface{} so json.Marshal round-trips the original
	// shape (number / string / null).
	var out any
	if err := json.Unmarshal(ic.ID, &out); err != nil {
		return nil
	}
	return out
}
