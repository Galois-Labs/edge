// Package proxy provides a bidirectional TCP proxy used by the galois-edge
// Go supervisor to forward connections from the Tailscale interface to the
// Python engine's internal ports (e.g. gRPC 50051->50052, WS 8765->8766).
package proxy

import (
	"context"
	"fmt"
	"io"
	"log"
	"net"
	"sync"
	"sync/atomic"
	"time"
)

// drainTimeout is the maximum time Stop waits for active connections to
// finish before giving up.
const drainTimeout = 10 * time.Second

// dialTimeout is how long the proxy waits when connecting to the target.
const dialTimeout = 5 * time.Second

// TCPProxy accepts connections on a listener and relays each one to a
// fixed target address using bidirectional io.Copy. It supports graceful
// shutdown: Stop closes the listener, then waits up to drainTimeout for
// in-flight connections to complete.
type TCPProxy struct {
	name       string
	listener   net.Listener
	targetAddr string

	ctx    context.Context
	cancel context.CancelFunc

	active    sync.WaitGroup
	connCount atomic.Int64
	stopped   atomic.Bool

	closeOnce sync.Once
	done      chan struct{}
}

// New creates a TCPProxy that will forward connections accepted on
// listener to targetAddr. The name is used in log messages to
// distinguish multiple proxies (e.g. "grpc", "ws"). Call Serve to
// begin accepting connections.
func New(name string, listener net.Listener, targetAddr string) *TCPProxy {
	ctx, cancel := context.WithCancel(context.Background())
	return &TCPProxy{
		name:       name,
		listener:   listener,
		targetAddr: targetAddr,
		ctx:        ctx,
		cancel:     cancel,
		done:       make(chan struct{}),
	}
}

// Serve runs the accept loop. It blocks until Stop is called, the
// provided context is cancelled, or the listener returns an
// unrecoverable error.
func (p *TCPProxy) Serve(ctx context.Context) error {
	// If the caller's context ends, trigger a stop.
	go func() {
		select {
		case <-ctx.Done():
			p.Stop()
		case <-p.ctx.Done():
		}
	}()

	log.Printf("[proxy:%s] forwarding %s -> %s", p.name, p.listener.Addr(), p.targetAddr)

	defer func() {
		p.closeOnce.Do(func() { close(p.done) })
	}()

	for {
		conn, err := p.listener.Accept()
		if err != nil {
			if p.stopped.Load() {
				return nil // listener closed by Stop — normal shutdown
			}
			return fmt.Errorf("proxy %s: accept: %w", p.name, err)
		}

		p.active.Add(1)
		id := p.connCount.Add(1)
		go p.relay(id, conn)
	}
}

// relay handles a single proxied connection: dial the target, then copy
// bytes in both directions until one side closes or errors.
func (p *TCPProxy) relay(id int64, src net.Conn) {
	defer p.active.Done()
	defer src.Close()

	log.Printf("[proxy:%s] #%d from %s", p.name, id, src.RemoteAddr())
	defer log.Printf("[proxy:%s] #%d closed", p.name, id)

	dst, err := net.DialTimeout("tcp", p.targetAddr, dialTimeout)
	if err != nil {
		log.Printf("[proxy:%s] #%d dial %s: %v", p.name, id, p.targetAddr, err)
		return
	}
	defer dst.Close()

	var wg sync.WaitGroup
	wg.Add(2)

	pipe := func(dir string, to, from net.Conn) {
		defer wg.Done()
		if _, cerr := io.Copy(to, from); cerr != nil && !isClosedConn(cerr) {
			log.Printf("[proxy:%s] #%d %s: %v", p.name, id, dir, cerr)
		}
		// Half-close the write side so the peer sees EOF.
		if tc, ok := to.(*net.TCPConn); ok {
			_ = tc.CloseWrite()
		}
	}

	go pipe("src->dst", dst, src)
	go pipe("dst->src", src, dst)

	wg.Wait()
}

// Stop stops accepting new connections and waits up to drainTimeout for
// active connections to complete. It is safe to call from multiple
// goroutines and more than once.
func (p *TCPProxy) Stop() {
	if !p.stopped.CompareAndSwap(false, true) {
		<-p.done // already stopping — wait for completion
		return
	}

	log.Printf("[proxy:%s] stopping, draining active connections...", p.name)
	p.cancel()
	_ = p.listener.Close()

	// Wait for in-flight connections, up to drainTimeout.
	drained := make(chan struct{})
	go func() {
		p.active.Wait()
		close(drained)
	}()

	select {
	case <-drained:
		log.Printf("[proxy:%s] all connections drained", p.name)
	case <-time.After(drainTimeout):
		log.Printf("[proxy:%s] drain timeout (%s), abandoning remaining connections", p.name, drainTimeout)
	}

	p.closeOnce.Do(func() { close(p.done) })
}

// isClosedConn reports whether err indicates a write/read on a closed
// network connection, which is expected during shutdown.
func isClosedConn(err error) bool {
	if err == nil {
		return false
	}
	return err == net.ErrClosed
}
