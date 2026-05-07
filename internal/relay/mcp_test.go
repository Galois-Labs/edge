package relay

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

// ---------------------------------------------------------------------------
// Wire-format tests
// ---------------------------------------------------------------------------

func TestMarshalMCPRequest(t *testing.T) {
	msg := &relayMessage{
		Type:         "mcp_request",
		McpRequestID: "mcp-req-1",
		CallerJwt:    "eyJhbGciOiJSUzI1NiJ9.test.sig",
		Payload:      json.RawMessage(`{"jsonrpc":"2.0","id":1,"method":"tools/list"}`),
	}
	data, err := MarshalMessage(msg)
	if err != nil {
		t.Fatalf("MarshalMessage: %v", err)
	}
	got, err := UnmarshalMessage(data)
	if err != nil {
		t.Fatalf("UnmarshalMessage: %v", err)
	}
	if got.Type != "mcp_request" {
		t.Errorf("Type = %q, want mcp_request", got.Type)
	}
	if got.McpRequestID != "mcp-req-1" {
		t.Errorf("McpRequestID = %q", got.McpRequestID)
	}
	if got.CallerJwt != "eyJhbGciOiJSUzI1NiJ9.test.sig" {
		t.Errorf("CallerJwt mismatch")
	}
	if !strings.Contains(string(got.Payload), `"method":"tools/list"`) {
		t.Errorf("Payload not preserved: %s", string(got.Payload))
	}
}

func TestMCPMessageFieldsOmitWhenEmpty(t *testing.T) {
	// A plain command_request shouldn't carry MCP fields in JSON.
	msg := &relayMessage{
		Type:        "command_request",
		RequestID:   "abc",
		CommandName: "x",
	}
	data, err := MarshalMessage(msg)
	if err != nil {
		t.Fatalf("MarshalMessage: %v", err)
	}
	if strings.Contains(string(data), "mcp_request_id") {
		t.Errorf("expected no mcp_request_id in command_request: %s", string(data))
	}
	if strings.Contains(string(data), "caller_jwt") {
		t.Errorf("expected no caller_jwt in command_request: %s", string(data))
	}
	if strings.Contains(string(data), "payload") {
		t.Errorf("expected no payload in command_request: %s", string(data))
	}
}

// ---------------------------------------------------------------------------
// jsonHasNonNullID
// ---------------------------------------------------------------------------

func TestJSONHasNonNullID(t *testing.T) {
	cases := []struct {
		name string
		body string
		want bool
	}{
		{"response with numeric id", `{"jsonrpc":"2.0","id":1,"result":{}}`, true},
		{"response with string id", `{"jsonrpc":"2.0","id":"abc","result":{}}`, true},
		{"notification, no id", `{"jsonrpc":"2.0","method":"notifications/progress"}`, false},
		{"explicit null id", `{"jsonrpc":"2.0","id":null,"method":"x"}`, false},
		{"malformed", `not json`, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := jsonHasNonNullID([]byte(tc.body))
			if got != tc.want {
				t.Errorf("jsonHasNonNullID(%s) = %v, want %v", tc.body, got, tc.want)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// handleMCP — unary path
// ---------------------------------------------------------------------------

// fakeMCP serves either an application/json response or a text/event-stream
// SSE stream depending on test wiring.
type fakeMCP struct {
	server   *httptest.Server
	hits     int
	mu       sync.Mutex
	jsonBody string
	sseLines []string // each is one full event ("data: …")
	jwtSeen  string
}

func newFakeMCPJSON(body string) *fakeMCP {
	f := &fakeMCP{jsonBody: body}
	f.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		f.hits++
		f.jwtSeen = r.Header.Get("Galois-Caller-JWT")
		f.mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, body)
	}))
	return f
}

func newFakeMCPSSE(events []string) *fakeMCP {
	f := &fakeMCP{sseLines: events}
	f.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		f.hits++
		f.mu.Unlock()
		w.Header().Set("Content-Type", "text/event-stream")
		w.Header().Set("Cache-Control", "no-cache")
		w.WriteHeader(http.StatusOK)
		flusher, _ := w.(http.Flusher)
		for _, ev := range events {
			_, _ = io.WriteString(w, ev)
			_, _ = io.WriteString(w, "\n\n")
			if flusher != nil {
				flusher.Flush()
			}
		}
	}))
	return f
}

func (f *fakeMCP) Close() { f.server.Close() }

// hostPort splits the httptest.Server URL into host + port for the relay
// client constructor.
func (f *fakeMCP) hostPort(t *testing.T) (string, int) {
	t.Helper()
	u := f.server.URL
	u = strings.TrimPrefix(u, "http://")
	host, portStr, err := net.SplitHostPort(u)
	if err != nil {
		t.Fatalf("split %q: %v", u, err)
	}
	var port int
	if _, err := fmt.Sscanf(portStr, "%d", &port); err != nil {
		t.Fatalf("parse port %q: %v", portStr, err)
	}
	return host, port
}

// wsTestPair builds a WebSocket server + connected client pair so we can
// invoke handleMCP and observe what it writes.
type wsTestPair struct {
	srv      *httptest.Server
	server   *websocket.Conn // server side (where handleMCP writes via the client copy)
	client   *websocket.Conn // client side (where we read the emitted frames)
	upgrader websocket.Upgrader
}

func newWSTestPair(t *testing.T) *wsTestPair {
	t.Helper()
	wp := &wsTestPair{
		upgrader: websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }},
	}
	ready := make(chan struct{})
	wp.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := wp.upgrader.Upgrade(w, r, nil)
		if err != nil {
			t.Errorf("upgrade: %v", err)
			return
		}
		wp.server = conn
		close(ready)
		// Hold the connection open until the client closes it.
		for {
			if _, _, err := conn.ReadMessage(); err != nil {
				return
			}
		}
	}))
	wsURL := "ws" + strings.TrimPrefix(wp.srv.URL, "http")
	c, _, err := websocket.DefaultDialer.Dial(wsURL, nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	wp.client = c
	<-ready
	return wp
}

func (p *wsTestPair) Close() {
	if p.client != nil {
		p.client.Close()
	}
	if p.server != nil {
		p.server.Close()
	}
	p.srv.Close()
}

func TestHandleMCPUnaryJSON(t *testing.T) {
	mcp := newFakeMCPJSON(`{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}`)
	defer mcp.Close()
	host, port := mcp.hostPort(t)

	wp := newWSTestPair(t)
	defer wp.Close()

	c := NewClient("edge-x", "edge", "1.0", "ws://unused", "tok", "127.0.0.1:50052",
		slog.New(slog.NewTextHandler(io.Discard, nil)))
	c.WithMCPTarget(host, port, "/mcp")

	var mu sync.Mutex
	req := relayMessage{
		Type:         "mcp_request",
		McpRequestID: "mcp-1",
		CallerJwt:    "test-jwt",
		Payload:      json.RawMessage(`{"jsonrpc":"2.0","id":1,"method":"tools/list"}`),
	}

	// Run handleMCP synchronously; it should write exactly one mcp_response.
	done := make(chan struct{})
	go func() {
		c.handleMCP(context.Background(), &mu, wp.server, req)
		close(done)
	}()

	wp.client.SetReadDeadline(time.Now().Add(5 * time.Second))
	_, raw, err := wp.client.ReadMessage()
	if err != nil {
		t.Fatalf("read first frame: %v", err)
	}
	got, err := UnmarshalMessage(raw)
	if err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got.Type != "mcp_response" {
		t.Errorf("Type = %q, want mcp_response", got.Type)
	}
	if got.McpRequestID != "mcp-1" {
		t.Errorf("McpRequestID = %q", got.McpRequestID)
	}
	if !strings.Contains(string(got.Payload), `"result":{"tools":[]}`) {
		t.Errorf("payload: %s", string(got.Payload))
	}

	<-done

	if mcp.hits != 1 {
		t.Errorf("fake MCP hits = %d, want 1", mcp.hits)
	}
	if mcp.jwtSeen != "test-jwt" {
		t.Errorf("Galois-Caller-JWT = %q, want test-jwt", mcp.jwtSeen)
	}
}

func TestHandleMCPStreamSSE(t *testing.T) {
	// Two notifications (no id) followed by a response (id=2).
	events := []string{
		`data: {"jsonrpc":"2.0","method":"notifications/progress","params":{"progress":1}}`,
		`data: {"jsonrpc":"2.0","method":"notifications/progress","params":{"progress":2}}`,
		`data: {"jsonrpc":"2.0","id":2,"result":{"count":2}}`,
	}
	mcp := newFakeMCPSSE(events)
	defer mcp.Close()
	host, port := mcp.hostPort(t)

	wp := newWSTestPair(t)
	defer wp.Close()

	c := NewClient("edge-y", "edge", "1.0", "ws://unused", "tok", "127.0.0.1:50052",
		slog.New(slog.NewTextHandler(io.Discard, nil)))
	c.WithMCPTarget(host, port, "/mcp")

	var mu sync.Mutex
	req := relayMessage{
		Type:         "mcp_request",
		McpRequestID: "mcp-stream-1",
		Payload:      json.RawMessage(`{"jsonrpc":"2.0","id":2,"method":"start_stream"}`),
	}

	done := make(chan struct{})
	go func() {
		c.handleMCP(context.Background(), &mu, wp.server, req)
		close(done)
	}()

	// Expect: 2 notifications + 1 response, in order.
	wp.client.SetReadDeadline(time.Now().Add(5 * time.Second))

	wantTypes := []string{"mcp_notification", "mcp_notification", "mcp_response"}
	for i, want := range wantTypes {
		_, raw, err := wp.client.ReadMessage()
		if err != nil {
			t.Fatalf("read frame %d: %v", i, err)
		}
		got, err := UnmarshalMessage(raw)
		if err != nil {
			t.Fatalf("unmarshal %d: %v", i, err)
		}
		if got.Type != want {
			t.Errorf("frame %d Type = %q, want %q (payload=%s)", i, got.Type, want, string(got.Payload))
		}
		if got.McpRequestID != "mcp-stream-1" {
			t.Errorf("frame %d McpRequestID = %q", i, got.McpRequestID)
		}
	}
	<-done
}

func TestHandleMCPRejectsWhenPortUnconfigured(t *testing.T) {
	wp := newWSTestPair(t)
	defer wp.Close()

	c := NewClient("edge-z", "edge", "1.0", "ws://unused", "tok", "127.0.0.1:50052",
		slog.New(slog.NewTextHandler(io.Discard, nil)))
	// Intentionally do NOT call WithMCPTarget — mcpPort stays 0.

	var mu sync.Mutex
	req := relayMessage{
		Type:         "mcp_request",
		McpRequestID: "mcp-nope",
		Payload:      json.RawMessage(`{"jsonrpc":"2.0","id":1,"method":"tools/list"}`),
	}

	done := make(chan struct{})
	go func() {
		c.handleMCP(context.Background(), &mu, wp.server, req)
		close(done)
	}()

	wp.client.SetReadDeadline(time.Now().Add(2 * time.Second))
	_, raw, err := wp.client.ReadMessage()
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	got, err := UnmarshalMessage(raw)
	if err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got.Type != "mcp_response" {
		t.Errorf("Type = %q", got.Type)
	}
	if !strings.Contains(string(got.Payload), "not configured") {
		t.Errorf("expected 'not configured' in error: %s", string(got.Payload))
	}
	<-done
}
