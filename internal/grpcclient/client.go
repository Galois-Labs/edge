// Package grpcclient provides a lightweight gRPC client for the local Python
// instrument engine. It is used by the registration manager to fetch the
// current instrument list before reporting to the cloud backend.
package grpcclient

import (
	"context"
	"fmt"
	"time"

	edgepb "github.com/galois-labs/edge/proto/gen/go/edge/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

// DefaultTimeout is applied to every gRPC call if the caller's context does
// not already carry a deadline.
const DefaultTimeout = 5 * time.Second

// Client wraps a gRPC connection to the Python daemon running on localhost.
type Client struct {
	conn   *grpc.ClientConn
	svc    edgepb.EdgeDaemonServiceClient
	target string
}

// New dials the given target (e.g. "127.0.0.1:50052") over a plaintext
// connection. The caller must call Close when the client is no longer needed.
func New(target string) (*Client, error) {
	conn, err := grpc.NewClient(
		target,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		return nil, fmt.Errorf("grpc dial %s: %w", target, err)
	}
	return &Client{
		conn:   conn,
		svc:    edgepb.NewEdgeDaemonServiceClient(conn),
		target: target,
	}, nil
}

// GetInstruments calls ListInstruments on the Python daemon and returns the
// instrument list. A per-call timeout of DefaultTimeout is applied.
func (c *Client) GetInstruments(ctx context.Context) ([]*edgepb.Instrument, error) {
	ctx, cancel := context.WithTimeout(ctx, DefaultTimeout)
	defer cancel()

	resp, err := c.svc.ListInstruments(ctx, &edgepb.ListInstrumentsRequest{})
	if err != nil {
		return nil, fmt.Errorf("ListInstruments: %w", err)
	}
	return resp.GetInstruments(), nil
}

// Close releases the underlying gRPC connection.
func (c *Client) Close() error {
	if c.conn != nil {
		return c.conn.Close()
	}
	return nil
}
