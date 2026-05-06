//go:build linux

package claudeingest

import (
	"net"
	"syscall"

	"golang.org/x/sys/unix"
)

// readPeerUID returns the UID of the process on the other end of a
// Unix domain socket. Linux exposes this via SO_PEERCRED.
func readPeerUID(c *net.UnixConn) (int, error) {
	raw, err := c.SyscallConn()
	if err != nil {
		return 0, err
	}
	var uid int
	var sysErr error
	err = raw.Control(func(fd uintptr) {
		var ucred *unix.Ucred
		ucred, sysErr = unix.GetsockoptUcred(int(fd), unix.SOL_SOCKET, unix.SO_PEERCRED)
		if sysErr != nil {
			return
		}
		uid = int(ucred.Uid)
	})
	if err != nil {
		return 0, err
	}
	if sysErr != nil {
		// The expected unix.Errno path on misconfigured kernels.
		if errno, ok := sysErr.(syscall.Errno); ok {
			return 0, errno
		}
		return 0, sysErr
	}
	return uid, nil
}
