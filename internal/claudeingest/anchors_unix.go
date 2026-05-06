//go:build !windows

package claudeingest

import (
	"os"

	"golang.org/x/sys/unix"
)

// acquireFileLock takes an exclusive flock on lockPath, creating the
// file if needed. It returns a release function that the caller MUST
// invoke (deferred) to drop the lock and close the file. Blocks until
// the lock is granted; flock waits are typically sub-millisecond on
// uncontended files.
func acquireFileLock(lockPath string) (func(), error) {
	f, err := os.OpenFile(lockPath, os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		return nil, err
	}
	if err := unix.Flock(int(f.Fd()), unix.LOCK_EX); err != nil {
		_ = f.Close()
		return nil, err
	}
	return func() {
		_ = unix.Flock(int(f.Fd()), unix.LOCK_UN)
		_ = f.Close()
	}, nil
}
