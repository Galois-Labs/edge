//go:build !relay_hello_ack

// hello_ack_stub.go provides a no-op waitForHelloAck when the relay_hello_ack
// build tag is not set. The daemon proceeds directly to the heartbeat + read
// loop without waiting for a hello_ack from the backend, which preserves
// backward compatibility with backends that do not yet send hello_ack.
//
// See hello_ack.go for the feature implementation and build instructions.
package relay

import (
	"context"

	"github.com/gorilla/websocket"
)

// waitForHelloAck is a no-op stub. The hello_ack feature is not enabled.
// Build with -tags relay_hello_ack to enable it.
func (c *Client) waitForHelloAck(_ context.Context, _ *websocket.Conn) error {
	return nil
}
