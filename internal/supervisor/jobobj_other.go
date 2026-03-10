//go:build !windows

package supervisor

// assignToJobObject is a no-op on non-Windows platforms.
// On Windows, this creates a Job Object that ensures the child process is
// terminated when the Go supervisor exits.
func assignToJobObject(pid int) error {
	return nil
}
