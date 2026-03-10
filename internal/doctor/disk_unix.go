//go:build unix

package doctor

import "syscall"

// freeDiskBytes returns the number of free bytes available to an unprivileged
// user on the filesystem containing the given path.
func freeDiskBytes(path string) (uint64, error) {
	var stat syscall.Statfs_t
	if err := syscall.Statfs(path, &stat); err != nil {
		return 0, err
	}
	return stat.Bavail * uint64(stat.Bsize), nil
}
