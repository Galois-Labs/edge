//go:build windows

package service

import (
	"fmt"
	"time"

	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/mgr"
)

// ServiceName is the Windows SCM service name.
const ServiceName = "galois-edge"

// serviceDisplayName is shown in the Windows Services management console.
const serviceDisplayName = "galois-edge daemon"

// serviceDescription is stored in the SCM registry entry.
const serviceDescription = "galois-edge daemon - laboratory instrument gateway"

// ---------------------------------------------------------------------------
// svc.Handler implementation
// ---------------------------------------------------------------------------

// handler implements svc.Handler for the Windows Service Control Manager.
type handler struct {
	startFunc func() error
	stopFunc  func()
}

// Execute is called by the Windows SCM to manage the service lifecycle.
// It blocks until the service receives a stop or shutdown command.
func (h *handler) Execute(args []string, r <-chan svc.ChangeRequest, s chan<- svc.Status) (bool, uint32) {
	const acceptedCmds = svc.AcceptStop | svc.AcceptShutdown

	s <- svc.Status{State: svc.StartPending}

	if err := h.startFunc(); err != nil {
		return true, 1
	}

	s <- svc.Status{State: svc.Running, Accepts: acceptedCmds}

	for c := range r {
		switch c.Cmd {
		case svc.Interrogate:
			s <- c.CurrentStatus
		case svc.Stop, svc.Shutdown:
			s <- svc.Status{State: svc.StopPending}
			h.stopFunc()
			return false, 0
		}
	}

	return false, 0
}

// ---------------------------------------------------------------------------
// Public API — SCM lifecycle
// ---------------------------------------------------------------------------

// RunAsService runs the daemon under the Windows Service Control Manager.
// startFunc is invoked to initialise the daemon; stopFunc is called when a
// stop or shutdown command arrives from the SCM.
func RunAsService(startFunc func() error, stopFunc func()) error {
	return svc.Run(ServiceName, &handler{
		startFunc: startFunc,
		stopFunc:  stopFunc,
	})
}

// IsWindowsService reports whether this process was launched by the Windows
// Service Control Manager.
func IsWindowsService() bool {
	is, err := svc.IsWindowsService()
	if err != nil {
		return false
	}
	return is
}

// ---------------------------------------------------------------------------
// Public API — install / uninstall / start / stop / status
// ---------------------------------------------------------------------------

// InstallService registers the service with the Windows SCM. The binary at
// exePath will be invoked with "start --config <configPath>" as arguments.
// The user parameter is ignored on Windows (services run as LocalSystem by
// default).
func InstallService(exePath, configPath, _ string) error {
	m, err := mgr.Connect()
	if err != nil {
		return fmt.Errorf("connect to SCM: %w", err)
	}
	defer m.Disconnect()

	s, err := m.CreateService(ServiceName, exePath, mgr.Config{
		DisplayName: serviceDisplayName,
		Description: serviceDescription,
		StartType:   mgr.StartAutomatic,
	}, "start", "--config", configPath)
	if err != nil {
		return fmt.Errorf("create service: %w", err)
	}
	defer s.Close()

	// Configure recovery actions so the SCM auto-restarts on failure.
	recoveryActions := []mgr.RecoveryAction{
		{Type: mgr.ServiceRestart, Delay: 5 * time.Second},
		{Type: mgr.ServiceRestart, Delay: 15 * time.Second},
		{Type: mgr.ServiceRestart, Delay: 60 * time.Second},
	}
	resetPeriod := uint32((24 * time.Hour).Seconds())
	if err := s.SetRecoveryActions(recoveryActions, resetPeriod); err != nil {
		return fmt.Errorf("set recovery actions: %w", err)
	}

	return nil
}

// UninstallService stops the service (if running) and removes it from the SCM.
func UninstallService() error {
	m, err := mgr.Connect()
	if err != nil {
		return fmt.Errorf("connect to SCM: %w", err)
	}
	defer m.Disconnect()

	s, err := m.OpenService(ServiceName)
	if err != nil {
		return fmt.Errorf("open service: %w", err)
	}
	defer s.Close()

	// Best-effort stop before deleting.
	status, err := s.Query()
	if err == nil && status.State != svc.Stopped {
		_, _ = s.Control(svc.Stop)
		deadline := time.Now().Add(10 * time.Second)
		for time.Now().Before(deadline) {
			status, err = s.Query()
			if err != nil || status.State == svc.Stopped {
				break
			}
			time.Sleep(500 * time.Millisecond)
		}
	}

	if err := s.Delete(); err != nil {
		return fmt.Errorf("delete service: %w", err)
	}
	return nil
}

// StartService sends a start command to the SCM.
func StartService() error {
	m, err := mgr.Connect()
	if err != nil {
		return fmt.Errorf("connect to SCM: %w", err)
	}
	defer m.Disconnect()

	s, err := m.OpenService(ServiceName)
	if err != nil {
		return fmt.Errorf("open service: %w", err)
	}
	defer s.Close()

	if err := s.Start(); err != nil {
		return fmt.Errorf("start service: %w", err)
	}
	return nil
}

// StopService sends a stop command to the SCM.
func StopService() error {
	m, err := mgr.Connect()
	if err != nil {
		return fmt.Errorf("connect to SCM: %w", err)
	}
	defer m.Disconnect()

	s, err := m.OpenService(ServiceName)
	if err != nil {
		return fmt.Errorf("open service: %w", err)
	}
	defer s.Close()

	_, err = s.Control(svc.Stop)
	if err != nil {
		return fmt.Errorf("stop service: %w", err)
	}
	return nil
}

// ServiceStatus queries the SCM and returns a human-readable state string:
// "running", "stopped", "start_pending", "stop_pending", or "unknown".
func ServiceStatus() (string, error) {
	m, err := mgr.Connect()
	if err != nil {
		return "", fmt.Errorf("connect to SCM: %w", err)
	}
	defer m.Disconnect()

	s, err := m.OpenService(ServiceName)
	if err != nil {
		return "", fmt.Errorf("open service: %w", err)
	}
	defer s.Close()

	status, err := s.Query()
	if err != nil {
		return "", fmt.Errorf("query service: %w", err)
	}

	switch status.State {
	case svc.Running:
		return "running", nil
	case svc.Stopped:
		return "stopped", nil
	case svc.StartPending:
		return "start_pending", nil
	case svc.StopPending:
		return "stop_pending", nil
	default:
		return "unknown", nil
	}
}
