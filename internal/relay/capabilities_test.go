package relay

import (
	"context"
	"io"
	"log/slog"
	"net"
	"sync"
	"testing"
	"time"

	edgepb "github.com/galois-labs/edge/proto/gen/go/edge/v1"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/encoding/protojson"
)

// fakeEdgeDaemon is a minimal in-process EdgeDaemonService that returns a
// canned GetCapabilitiesResponse. It mirrors fakeMCP in mcp_test.go but for the
// local gRPC surface handleCapabilities dials.
type fakeEdgeDaemon struct {
	edgepb.UnimplementedEdgeDaemonServiceServer
	resp *edgepb.GetCapabilitiesResponse

	mu              sync.Mutex
	gotInstrumentID string
}

func (f *fakeEdgeDaemon) GetCapabilities(ctx context.Context, req *edgepb.GetCapabilitiesRequest) (*edgepb.GetCapabilitiesResponse, error) {
	f.mu.Lock()
	f.gotInstrumentID = req.GetInstrumentId()
	f.mu.Unlock()
	return f.resp, nil
}

// startFakeDaemon launches the fake gRPC server on a local port and returns its
// address. The server is stopped on test cleanup.
func startFakeDaemon(t *testing.T, svc edgepb.EdgeDaemonServiceServer) string {
	t.Helper()
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	srv := grpc.NewServer()
	edgepb.RegisterEdgeDaemonServiceServer(srv, svc)
	go func() { _ = srv.Serve(lis) }()
	t.Cleanup(srv.Stop)
	return lis.Addr().String()
}

func TestHandleCapabilities(t *testing.T) {
	fake := &fakeEdgeDaemon{
		resp: &edgepb.GetCapabilitiesResponse{
			EdgeId: "edge-cap",
			Capabilities: []*edgepb.InstrumentCapabilities{
				{
					InstrumentId: "instr-1",
					HasProfile:   true,
					Manufacturer: "Keysight",
					Model:        "EDU36311A",
				},
			},
		},
	}
	addr := startFakeDaemon(t, fake)

	wp := newWSTestPair(t)
	defer wp.Close()

	c := NewClient("edge-cap", "edge", "1.0", "ws://unused", "tok", addr,
		slog.New(slog.NewTextHandler(io.Discard, nil)))

	var mu sync.Mutex
	req := relayMessage{
		Type:         "capabilities_request",
		RequestID:    "cap-1",
		InstrumentID: "instr-1",
	}

	// Run handleCapabilities; it should write exactly one capabilities_response.
	done := make(chan struct{})
	go func() {
		c.handleCapabilities(context.Background(), &mu, wp.server, req)
		close(done)
	}()

	wp.client.SetReadDeadline(time.Now().Add(5 * time.Second))
	_, raw, err := wp.client.ReadMessage()
	if err != nil {
		t.Fatalf("read frame: %v", err)
	}
	got, err := UnmarshalMessage(raw)
	if err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got.Type != "capabilities_response" {
		t.Errorf("Type = %q, want capabilities_response", got.Type)
	}
	if got.RequestID != "cap-1" {
		t.Errorf("RequestID = %q", got.RequestID)
	}
	if !got.Success {
		t.Errorf("Success = false, want true (error=%q)", got.ErrorMessage)
	}

	// Payload must protojson-decode back into the canned response.
	var decoded edgepb.GetCapabilitiesResponse
	if err := protojson.Unmarshal(got.Payload, &decoded); err != nil {
		t.Fatalf("protojson decode payload: %v (payload=%s)", err, string(got.Payload))
	}
	if decoded.GetEdgeId() != "edge-cap" {
		t.Errorf("EdgeId = %q, want edge-cap", decoded.GetEdgeId())
	}
	if len(decoded.GetCapabilities()) != 1 {
		t.Fatalf("capabilities len = %d, want 1", len(decoded.GetCapabilities()))
	}
	if model := decoded.GetCapabilities()[0].GetModel(); model != "EDU36311A" {
		t.Errorf("Model = %q, want EDU36311A", model)
	}

	<-done

	fake.mu.Lock()
	gotInstr := fake.gotInstrumentID
	fake.mu.Unlock()
	if gotInstr != "instr-1" {
		t.Errorf("daemon saw InstrumentId = %q, want instr-1", gotInstr)
	}
}
