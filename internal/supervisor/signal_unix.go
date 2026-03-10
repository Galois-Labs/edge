//go:build unix

package supervisor

import (
	"os"
	"syscall"
)

// terminateProcess sends SIGTERM to the process on Unix systems.
// This gives the child a chance to shut down gracefully before
// escalating to SIGKILL.
func terminateProcess(p *os.Process) error {
	return p.Signal(syscall.SIGTERM)
}
