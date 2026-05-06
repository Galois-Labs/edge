//go:build darwin

package claudeingest

import (
	"net"

	"golang.org/x/sys/unix"
)

// readPeerUID returns the UID of the process on the other end of a
// Unix domain socket. macOS exposes this via SOL_LOCAL/LOCAL_PEERCRED
// returning a struct xucred whose first uid field is the effective
// UID. golang.org/x/sys/unix wraps the getsockopt as GetsockoptXucred.
func readPeerUID(c *net.UnixConn) (int, error) {
	raw, err := c.SyscallConn()
	if err != nil {
		return 0, err
	}
	var uid int
	var callErr error
	err = raw.Control(func(fd uintptr) {
		var cred *unix.Xucred
		cred, callErr = unix.GetsockoptXucred(int(fd), unix.SOL_LOCAL, unix.LOCAL_PEERCRED)
		if callErr != nil {
			return
		}
		uid = int(cred.Uid)
	})
	if err != nil {
		return 0, err
	}
	if callErr != nil {
		return 0, callErr
	}
	return uid, nil
}
