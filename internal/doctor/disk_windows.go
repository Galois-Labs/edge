//go:build windows

package doctor

import (
	"syscall"
	"unsafe"
)

// freeDiskBytes returns the number of free bytes available on the filesystem
// containing the given path. It calls the Win32 GetDiskFreeSpaceExW API.
func freeDiskBytes(path string) (uint64, error) {
	kernel32 := syscall.NewLazyDLL("kernel32.dll")
	proc := kernel32.NewProc("GetDiskFreeSpaceExW")

	p, err := syscall.UTF16PtrFromString(path)
	if err != nil {
		return 0, err
	}

	var freeBytesAvailable uint64
	ret, _, callErr := proc.Call(
		uintptr(unsafe.Pointer(p)),
		uintptr(unsafe.Pointer(&freeBytesAvailable)),
		0,
		0,
	)
	if ret == 0 {
		return 0, callErr
	}
	return freeBytesAvailable, nil
}
