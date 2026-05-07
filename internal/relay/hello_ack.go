//go:build relay_hello_ack

// This file is compiled only when the relay_hello_ack build tag is set.
// It provides the waitForHelloAck implementation that reads one frame after
// sending hello and verifies its type is "hello_ack".
//
// Requires a matching backend change: the backend must send
//
//	{"type":"hello_ack","session_id":"<uuid>"}
//
// after validating the hello frame. Until that backend coordination is
// complete, build without this tag (the no-op stub in hello_ack_stub.go is
// used instead).
//
// Build with hello_ack support:
//
//	go build -tags relay_hello_ack ./...
//	go test  -tags relay_hello_ack ./internal/relay/...
package relay

import (
	"context"
	"fmt"
	"time"

	"github.com/gorilla/websocket"
)

// waitForHelloAck reads one frame from ws after sending hello, expecting a
// hello_ack. If the frame does not arrive within helloAckTimeout, or if the
// first frame is not hello_ack, a non-nil error is returned so connectAndServe
// can retry or exit cleanly.
//
// Close errors (including auth failure codes) are returned as-is so
// connectAndServe's normal handleReadError path can process them exactly once.
func (c *Client) waitForHelloAck(ctx context.Context, ws *websocket.Conn) error {
	// Apply a deadline for the hello_ack window.
	deadline := time.Now().Add(helloAckTimeout)
	ws.SetReadDeadline(deadline)
	defer ws.SetReadDeadline(time.Time{}) // clear the deadline after this function

	type readResult struct {
		msg relayMessage
		err error
	}

	ch := make(chan readResult, 1)
	go func() {
		var msg relayMessage
		err := ws.ReadJSON(&msg)
		ch <- readResult{msg, err}
	}()

	select {
	case <-ctx.Done():
		return ctx.Err()
	case result := <-ch:
		if result.err != nil {
			// Propagate the raw error to connectAndServe so handleReadError
			// is called exactly once (avoids duplicate log messages).
			return result.err
		}
		if result.msg.Type != "hello_ack" {
			c.logger.Warn("expected hello_ack, got unexpected frame type — closing and retrying",
				"got_type", result.msg.Type,
			)
			return fmt.Errorf("expected hello_ack, got %q", result.msg.Type)
		}
		c.logger.Info("received hello_ack from relay",
			"session_id", result.msg.SessionID,
		)
		return nil
	}
}
