//go:build windows

package claudeingest

import (
	"os"

	"golang.org/x/sys/windows"
)

// acquireFileLock takes an exclusive lock on lockPath via LockFileEx,
// blocking until granted. Returns a release function that the caller
// MUST invoke (deferred). On Windows the lock is mandatory and tied to
// the file handle; closing the handle releases automatically, but we
// also call UnlockFileEx for explicitness.
func acquireFileLock(lockPath string) (func(), error) {
	f, err := os.OpenFile(lockPath, os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		return nil, err
	}
	handle := windows.Handle(f.Fd())
	var ol windows.Overlapped
	// LOCKFILE_EXCLUSIVE_LOCK + 0 (blocking).
	if err := windows.LockFileEx(handle, windows.LOCKFILE_EXCLUSIVE_LOCK, 0, ^uint32(0), ^uint32(0), &ol); err != nil {
		_ = f.Close()
		return nil, err
	}
	return func() {
		var ol2 windows.Overlapped
		_ = windows.UnlockFileEx(handle, 0, ^uint32(0), ^uint32(0), &ol2)
		_ = f.Close()
	}, nil
}
