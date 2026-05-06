//go:build !windows

package claudeingest

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"log/slog"
	"net"
	"os"
	"os/user"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"

	"github.com/galois-labs/edge/internal/installid"
)

// controlSocketPath is the on-disk path of the daemon's control socket.
// On Linux the daemon's systemd unit typically owns /run/galois-edge;
// on macOS we use /var/run/galois-edge with the same idea. Both are
// system locations so multiple users can connect to one daemon-owned
// socket.
func controlSocketPath() string {
	switch runtime.GOOS {
	case "darwin":
		return "/var/run/galois-edge/claude-ingest.sock"
	default:
		return "/run/galois-edge/claude-ingest.sock"
	}
}

// fallbackUserSocketPath is used when the daemon cannot create the
// system socket (e.g., dev runs as a regular user). The fallback lives
// under the running user's runtime dir — connections from other users
// won't be possible, which is fine for dev.
func fallbackUserSocketPath() string {
	if dir := os.Getenv("XDG_RUNTIME_DIR"); dir != "" {
		return filepath.Join(dir, "galois-claude-ingest.sock")
	}
	if dir := os.TempDir(); dir != "" {
		return filepath.Join(dir, fmt.Sprintf("galois-claude-ingest-%d.sock", os.Getuid()))
	}
	return "/tmp/galois-claude-ingest.sock"
}

// newControlListener creates the Unix domain socket listener. It also
// chmods the socket so any local user can connect (peer authentication
// happens at connect time, not via filesystem perms).
func newControlListener(logger *slog.Logger) (net.Listener, error) {
	path := controlSocketPath()
	listener, err := tryListen(path)
	if err != nil {
		// Fall back to a per-user socket. Common in dev.
		fallback := fallbackUserSocketPath()
		l2, err2 := tryListen(fallback)
		if err2 != nil {
			return nil, fmt.Errorf("listen on %s: %w (fallback %s: %v)", path, err, fallback, err2)
		}
		logger.Warn("falling back to per-user control socket; multi-user connect not supported here",
			"system_path", path, "system_err", err, "fallback", fallback)
		return &peerCredListener{Listener: l2}, nil
	}
	// Make the socket world-connectable; peer-cred check is the actual
	// guard.
	if err := os.Chmod(path, 0o666); err != nil {
		_ = listener.Close()
		return nil, fmt.Errorf("chmod %s: %w", path, err)
	}
	return &peerCredListener{Listener: listener}, nil
}

func tryListen(path string) (net.Listener, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, err
	}
	// Remove a stale socket file from a prior run; bind() fails on
	// EADDRINUSE otherwise. Doing this race-free in the presence of a
	// peer daemon is impossible without locking — for v1 we accept
	// the small race and assume the daemon process is the only writer.
	if info, err := os.Stat(path); err == nil && info.Mode()&os.ModeSocket != 0 {
		_ = os.Remove(path)
	}
	return net.Listen("unix", path)
}

// peerCredListener wraps a net.Listener so each accepted Conn carries
// the peer's UID. The HTTP server's ConnContext later derives a subject
// key from this UID via getpwuid + installid.
type peerCredListener struct {
	net.Listener
}

func (l *peerCredListener) Accept() (net.Conn, error) {
	c, err := l.Listener.Accept()
	if err != nil {
		return nil, err
	}
	uc, ok := c.(*net.UnixConn)
	if !ok {
		// AF_UNIX listener should always produce *UnixConn; if not,
		// fail closed.
		_ = c.Close()
		return nil, fmt.Errorf("accepted non-unix conn: %T", c)
	}
	uid, err := readPeerUID(uc)
	if err != nil {
		_ = c.Close()
		return nil, fmt.Errorf("read peer cred: %w", err)
	}
	return &peerConn{Conn: c, peerUID: uid}, nil
}

// peerConn carries the peer's UID alongside the connection so
// ConnContext can read it.
type peerConn struct {
	net.Conn
	peerUID int
}

// peerSubjectKeyFromConn computes the expected subject_key for the
// process on the other end of the connection. Used by the http.Server
// ConnContext to attach a key to every request before any handler
// runs. Returns "unauthorized" on any failure so handlers can reject
// distinctly from "no peer info" (test path).
func peerSubjectKeyFromConn(conn net.Conn, logger *slog.Logger) string {
	pc, ok := conn.(*peerConn)
	if !ok {
		// Could be an unwrapped test connection; treat as unauthorized
		// so production listeners always require peer creds.
		return "unauthorized"
	}
	uid := pc.peerUID
	username, err := lookupUsername(uid)
	if err != nil {
		logger.Warn("peer username lookup failed", "uid", uid, "error", err)
		return "unauthorized"
	}
	id, err := installid.Read()
	if err != nil {
		logger.Warn("install id read failed; treating peer as unauthorized", "error", err)
		return "unauthorized"
	}
	keyInput := strings.Join([]string{id, username}, "\x00")
	sum := sha256.Sum256([]byte(keyInput))
	return hex.EncodeToString(sum[:])
}

// lookupUsername wraps user.LookupId. Cached because it's called per
// request and getpwuid can be slow on misconfigured NSS.
var (
	usernameCacheMu sync.Mutex
	usernameCache   = map[int]string{}
)

func lookupUsername(uid int) (string, error) {
	usernameCacheMu.Lock()
	if name, ok := usernameCache[uid]; ok {
		usernameCacheMu.Unlock()
		return name, nil
	}
	usernameCacheMu.Unlock()

	u, err := user.LookupId(strconv.Itoa(uid))
	if err != nil {
		return "", err
	}
	usernameCacheMu.Lock()
	usernameCache[uid] = u.Username
	usernameCacheMu.Unlock()
	return u.Username, nil
}

// dialControl connects to the daemon's control socket via Unix socket.
func dialControl(ctx context.Context, _, _ string) (net.Conn, error) {
	path := controlSocketPath()
	if _, err := os.Stat(path); err == nil {
		return (&net.Dialer{}).DialContext(ctx, "unix", path)
	}
	// Try fallback path.
	fallback := fallbackUserSocketPath()
	return (&net.Dialer{}).DialContext(ctx, "unix", fallback)
}
