// Capabilities-over-relay support for the daemon-side relay client.
//
// handleCapabilities is the dispatch sibling of handleCommand: it forwards a
// capabilities_request to the local Python daemon's GetCapabilities gRPC and
// emits the marshalled response back as a single capabilities_response frame.
//
// Wire correspondence (one logical request):
//
//	capabilities_request             cloud → daemon
//	capabilities_response            daemon → cloud  (Payload = protojson caps)
//
// The Payload is the protojson encoding of edgepb.GetCapabilitiesResponse so
// the cloud can decode it with the same generated types.

package relay

import (
	"context"
	"encoding/json"
	"fmt"
	"sync"

	edgepb "github.com/galois-labs/edge/proto/gen/go/edge/v1"
	"github.com/gorilla/websocket"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/encoding/protojson"
)

// handleCapabilities processes a single capabilities_request: forwards it to
// the local Python gRPC server and sends the result back as a
// capabilities_response.
func (c *Client) handleCapabilities(ctx context.Context, mu *sync.Mutex, ws *websocket.Conn, req relayMessage) {
	// Dial the local Python gRPC server (fresh connection per request to
	// avoid stale connections if Python restarts).
	conn, err := grpc.NewClient(
		c.localGRPC,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		c.sendCapabilitiesError(mu, ws, req.RequestID,
			fmt.Sprintf("failed to connect to local gRPC: %v", err))
		return
	}
	defer conn.Close()

	svc := edgepb.NewEdgeDaemonServiceClient(conn)

	// Call GetCapabilities with a timeout derived from the session ctx. If the
	// session ctx is already cancelled (e.g. the socket closed on auth failure)
	// the gRPC call will fail immediately.
	callCtx, callCancel := context.WithTimeout(ctx, grpcCallTimeout)
	defer callCancel()

	grpcResp, err := svc.GetCapabilities(callCtx, &edgepb.GetCapabilitiesRequest{
		InstrumentId: req.InstrumentID,
	})
	if err != nil {
		c.logger.Warn("gRPC GetCapabilities failed",
			"request_id", req.RequestID,
			"error", err,
		)
		c.sendCapabilitiesError(mu, ws, req.RequestID,
			fmt.Sprintf("gRPC error: %v", err))
		return
	}

	payload, err := protojson.Marshal(grpcResp)
	if err != nil {
		c.sendCapabilitiesError(mu, ws, req.RequestID,
			fmt.Sprintf("marshal capabilities: %v", err))
		return
	}

	resp := relayMessage{
		Type:      "capabilities_response",
		RequestID: req.RequestID,
		Success:   true,
		Payload:   json.RawMessage(payload),
	}
	if err := c.writeJSON(mu, ws, &resp); err != nil {
		c.logger.Warn("failed to send capabilities_response",
			"request_id", req.RequestID,
			"error", err,
		)
		return
	}

	c.logger.Info("sent capabilities response",
		"request_id", req.RequestID,
		"instrument_id", req.InstrumentID,
	)
}

// sendCapabilitiesError sends a capabilities_response with success=false.
func (c *Client) sendCapabilitiesError(mu *sync.Mutex, ws *websocket.Conn, requestID, errMsg string) {
	resp := relayMessage{
		Type:         "capabilities_response",
		RequestID:    requestID,
		Success:      false,
		ErrorMessage: errMsg,
	}
	if err := c.writeJSON(mu, ws, &resp); err != nil {
		c.logger.Warn("failed to send capabilities error response",
			"request_id", requestID,
			"error", err,
		)
	}
}
