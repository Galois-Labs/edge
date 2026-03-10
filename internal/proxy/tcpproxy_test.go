package proxy

import (
	"context"
	"io"
	"net"
	"sync"
	"testing"
	"time"
)

// ---------------------------------------------------------------------------
// Bidirectional copy with real TCP listener + dialer
// ---------------------------------------------------------------------------

func TestBidirectionalCopy(t *testing.T) {
	// Set up a "target" server that echoes data back.
	target, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("target listen: %v", err)
	}
	defer target.Close()

	go func() {
		for {
			conn, err := target.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				defer c.Close()
				io.Copy(c, c)
			}(conn)
		}
	}()

	// Set up the proxy listener.
	proxyLn, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("proxy listen: %v", err)
	}

	p := New("test-bidir", proxyLn, target.Addr().String())

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	var serveErr error
	serveDone := make(chan struct{})
	go func() {
		serveErr = p.Serve(ctx)
		close(serveDone)
	}()

	// Give the proxy a moment to start accepting.
	time.Sleep(50 * time.Millisecond)

	// Connect a client through the proxy.
	client, err := net.DialTimeout("tcp", proxyLn.Addr().String(), 2*time.Second)
	if err != nil {
		t.Fatalf("dial proxy: %v", err)
	}

	msg := []byte("hello proxy")
	if _, err := client.Write(msg); err != nil {
		t.Fatalf("write: %v", err)
	}

	buf := make([]byte, len(msg))
	if _, err := io.ReadFull(client, buf); err != nil {
		t.Fatalf("read: %v", err)
	}
	if string(buf) != "hello proxy" {
		t.Errorf("echo: got %q, want %q", string(buf), "hello proxy")
	}

	client.Close()

	// Stop the proxy.
	p.Stop()
	<-serveDone

	if serveErr != nil {
		t.Errorf("Serve returned error: %v", serveErr)
	}
}

// ---------------------------------------------------------------------------
// Graceful drain: stop while connection is active
// ---------------------------------------------------------------------------

func TestGracefulDrain(t *testing.T) {
	// Target: reads everything, then closes.
	target, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("target listen: %v", err)
	}
	defer target.Close()

	targetDone := make(chan struct{})
	go func() {
		defer close(targetDone)
		conn, err := target.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		io.Copy(io.Discard, conn)
	}()

	proxyLn, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("proxy listen: %v", err)
	}

	p := New("test-drain", proxyLn, target.Addr().String())

	ctx := context.Background()
	serveDone := make(chan struct{})
	go func() {
		p.Serve(ctx)
		close(serveDone)
	}()

	time.Sleep(50 * time.Millisecond)

	// Open a connection.
	client, err := net.DialTimeout("tcp", proxyLn.Addr().String(), 2*time.Second)
	if err != nil {
		t.Fatalf("dial proxy: %v", err)
	}

	// Write some data so the relay goroutines are active.
	client.Write([]byte("in-flight"))

	// Stop while the connection is still open — this should drain.
	stopDone := make(chan struct{})
	go func() {
		p.Stop()
		close(stopDone)
	}()

	// Close the client shortly after to let drain complete.
	time.Sleep(100 * time.Millisecond)
	client.Close()

	select {
	case <-stopDone:
		// Success — Stop returned after drain.
	case <-time.After(15 * time.Second):
		t.Fatal("Stop did not return within drain timeout")
	}
}

// ---------------------------------------------------------------------------
// Context cancellation
// ---------------------------------------------------------------------------

func TestContextCancellation(t *testing.T) {
	// Target listener.
	target, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("target listen: %v", err)
	}
	defer target.Close()

	proxyLn, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("proxy listen: %v", err)
	}

	p := New("test-ctx", proxyLn, target.Addr().String())

	ctx, cancel := context.WithCancel(context.Background())
	serveDone := make(chan struct{})
	go func() {
		p.Serve(ctx)
		close(serveDone)
	}()

	time.Sleep(50 * time.Millisecond)

	// Cancel the context — Serve should return.
	cancel()

	select {
	case <-serveDone:
		// Serve exited.
	case <-time.After(5 * time.Second):
		t.Fatal("Serve did not exit after context cancel")
	}
}

// ---------------------------------------------------------------------------
// Stop is idempotent
// ---------------------------------------------------------------------------

func TestStop_Idempotent(t *testing.T) {
	target, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("target listen: %v", err)
	}
	defer target.Close()

	proxyLn, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("proxy listen: %v", err)
	}

	p := New("test-idem", proxyLn, target.Addr().String())

	ctx := context.Background()
	serveDone := make(chan struct{})
	go func() {
		p.Serve(ctx)
		close(serveDone)
	}()

	time.Sleep(50 * time.Millisecond)

	// Call Stop concurrently from multiple goroutines — should not panic.
	var wg sync.WaitGroup
	for i := 0; i < 5; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			p.Stop()
		}()
	}
	wg.Wait()

	<-serveDone
}
