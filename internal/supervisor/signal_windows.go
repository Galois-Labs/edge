//go:build windows

package supervisor

import (
	"fmt"
	"os"

	"golang.org/x/sys/windows"
)

// terminateProcess calls TerminateProcess on Windows. Unlike Unix SIGTERM,
// this does not give the child an opportunity to run cleanup handlers.
// The stdin-pipe closure (step 1 of shutdown) serves as the graceful signal
// on Windows; TerminateProcess is the escalation path.
func terminateProcess(p *os.Process) error {
	handle, err := windows.OpenProcess(windows.PROCESS_TERMINATE, false, uint32(p.Pid))
	if err != nil {
		return fmt.Errorf("OpenProcess(%d): %w", p.Pid, err)
	}
	defer windows.CloseHandle(handle)

	if err := windows.TerminateProcess(handle, 1); err != nil {
		return fmt.Errorf("TerminateProcess(%d): %w", p.Pid, err)
	}
	return nil
}
