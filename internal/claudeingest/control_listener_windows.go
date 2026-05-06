//go:build windows

package claudeingest

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"log/slog"
	"net"
	"os/user"
	"strings"
	"sync"

	"github.com/Microsoft/go-winio"
	"github.com/galois-labs/edge/internal/installid"
	"golang.org/x/sys/windows"
)

const (
	// pipeName is the Windows named pipe path. Standard form
	// \\.\pipe\<name>; clients use the same path.
	pipeName = `\\.\pipe\galois-edge-claude-ingest`

	// pipeSDDL grants:
	//   - SYSTEM (SY) full control
	//   - Builtin Administrators (BA) full control
	//   - Authenticated Users (AU) connect+read+write
	// Authenticated Users is the loose grant that lets any logged-in
	// user connect; peer-cred verification on accept is the actual
	// auth boundary, so the DACL only needs to keep network/anonymous
	// users out.
	pipeSDDL = "D:(A;;FA;;;SY)(A;;FA;;;BA)(A;;0x12019b;;;AU)"
)

// newControlListener creates a named-pipe listener for the daemon's
// control endpoint.
func newControlListener(logger *slog.Logger) (net.Listener, error) {
	cfg := &winio.PipeConfig{
		SecurityDescriptor: pipeSDDL,
		MessageMode:        false,
		InputBufferSize:    65536,
		OutputBufferSize:   65536,
	}
	listener, err := winio.ListenPipe(pipeName, cfg)
	if err != nil {
		return nil, fmt.Errorf("listen pipe %s: %w", pipeName, err)
	}
	return &peerCredPipeListener{Listener: listener, logger: logger}, nil
}

// peerCredPipeListener wraps the named-pipe listener so each accepted
// conn carries the peer's SID + Windows username.
type peerCredPipeListener struct {
	net.Listener
	logger *slog.Logger
}

func (l *peerCredPipeListener) Accept() (net.Conn, error) {
	c, err := l.Listener.Accept()
	if err != nil {
		return nil, err
	}
	username, sysErr := readPeerUsername(c)
	if sysErr != nil {
		l.logger.Warn("read peer username failed", "error", sysErr)
		_ = c.Close()
		return nil, sysErr
	}
	return &peerConn{Conn: c, peerUsername: username}, nil
}

// peerConn carries the peer's username on Windows. (UID has no meaning
// here; Windows uses SIDs and we pre-resolve to a username for parity
// with the POSIX path.)
type peerConn struct {
	net.Conn
	peerUsername string
}

// peerSubjectKeyFromConn computes the expected subject_key for the
// peer at the other end of the pipe.
func peerSubjectKeyFromConn(conn net.Conn, logger *slog.Logger) string {
	pc, ok := conn.(*peerConn)
	if !ok {
		return "unauthorized"
	}
	if pc.peerUsername == "" {
		return "unauthorized"
	}
	id, err := installid.Read()
	if err != nil {
		logger.Warn("install id read failed; treating peer as unauthorized", "error", err)
		return "unauthorized"
	}
	keyInput := strings.Join([]string{id, pc.peerUsername}, "\x00")
	sum := sha256.Sum256([]byte(keyInput))
	return hex.EncodeToString(sum[:])
}

// readPeerUsername resolves the SID of the connected client to its
// account name. go-winio exposes the underlying handle; from there we
// open the impersonation token and look up the user SID.
func readPeerUsername(c net.Conn) (string, error) {
	type pidGetter interface {
		ClientProcessID() (uint32, error)
	}
	pg, ok := c.(pidGetter)
	if !ok {
		return "", fmt.Errorf("connection does not expose ClientProcessID")
	}
	pid, err := pg.ClientProcessID()
	if err != nil {
		return "", err
	}

	handle, err := windows.OpenProcess(
		windows.PROCESS_QUERY_LIMITED_INFORMATION,
		false,
		pid,
	)
	if err != nil {
		return "", err
	}
	defer windows.CloseHandle(handle)

	var token windows.Token
	if err := windows.OpenProcessToken(handle, windows.TOKEN_QUERY, &token); err != nil {
		return "", err
	}
	defer token.Close()

	tu, err := token.GetTokenUser()
	if err != nil {
		return "", err
	}
	sid := tu.User.Sid
	u, err := user.LookupId(sid.String())
	if err != nil {
		return cachedSIDLookup(sid.String()), nil
	}
	return u.Username, nil
}

// cachedSIDLookup is a fallback used when user.LookupId can't resolve
// the SID (rare but observed on heavily customized AD setups). We
// return the SID string itself as the "username" — it's stable
// per-account and combined with install_id still yields a unique
// subject_key, just one that doesn't match the human-typed username.
var (
	sidCacheMu sync.Mutex
	sidCache   = map[string]string{}
)

func cachedSIDLookup(sid string) string {
	sidCacheMu.Lock()
	defer sidCacheMu.Unlock()
	if name, ok := sidCache[sid]; ok {
		return name
	}
	sidCache[sid] = sid
	return sid
}

// dialControl connects to the daemon's named pipe.
func dialControl(ctx context.Context, _, _ string) (net.Conn, error) {
	return winio.DialPipeContext(ctx, pipeName)
}
