//go:build windows

package supervisor

import (
	"fmt"
	"unsafe"

	"golang.org/x/sys/windows"
)

// assignToJobObject creates a Windows Job Object configured with
// JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE and assigns the child process to it.
//
// When the Go supervisor process exits (or crashes), Windows automatically
// closes all handles, including the job handle. The KILL_ON_JOB_CLOSE flag
// ensures that the Python child process is terminated, preventing orphaned
// processes.
//
// The job handle is intentionally NOT closed by this function. It must remain
// open for the kill-on-close behavior to work. It will be closed automatically
// when the Go process exits.
func assignToJobObject(pid int) error {
	// Create the job object.
	job, err := windows.CreateJobObject(nil, nil)
	if err != nil {
		return fmt.Errorf("CreateJobObject: %w", err)
	}

	// Configure kill-on-close behavior.
	info := windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION{
		BasicLimitInformation: windows.JOBOBJECT_BASIC_LIMIT_INFORMATION{
			LimitFlags: windows.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
		},
	}
	_, err = windows.SetInformationJobObject(
		job,
		windows.JobObjectExtendedLimitInformation,
		uintptr(unsafe.Pointer(&info)),
		uint32(unsafe.Sizeof(info)),
	)
	if err != nil {
		windows.CloseHandle(job)
		return fmt.Errorf("SetInformationJobObject: %w", err)
	}

	// Open the child process by PID.
	handle, err := windows.OpenProcess(
		windows.PROCESS_SET_QUOTA|windows.PROCESS_TERMINATE,
		false,
		uint32(pid),
	)
	if err != nil {
		windows.CloseHandle(job)
		return fmt.Errorf("OpenProcess(%d): %w", pid, err)
	}
	defer windows.CloseHandle(handle)

	// Assign the child to the job.
	if err := windows.AssignProcessToJobObject(job, handle); err != nil {
		windows.CloseHandle(job)
		return fmt.Errorf("AssignProcessToJobObject(%d): %w", pid, err)
	}

	// Do NOT close job — handle must stay open for kill-on-close to work.
	return nil
}
