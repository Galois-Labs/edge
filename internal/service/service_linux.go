//go:build linux

// Package service provides platform-specific service lifecycle management for
// the galois-edge daemon. On Linux, this means systemd unit file generation
// and systemctl commands.
package service

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"text/template"
)

// ServiceName is the systemd unit name (without the .service suffix).
const ServiceName = "galois-edge"

const unitFileName = ServiceName + ".service"
const unitFilePath = "/etc/systemd/system/" + unitFileName

// systemdUnitTmpl renders a hardened systemd unit. Security directives restrict
// the daemon to the minimum privileges needed for instrument access.
var systemdUnitTmpl = template.Must(template.New("unit").Parse(`[Unit]
Description=galois-edge daemon - laboratory instrument gateway
After=network-online.target
Wants=network-online.target
Documentation=https://docs.galois.dev/edge

[Service]
Type=simple
ExecStart={{.ExecPath}} start --config {{.ConfigPath}}
User={{.User}}
Group={{.User}}
Restart=on-failure
RestartSec=5
TimeoutStopSec=15

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=galois-edge

# Security hardening
ProtectSystem=strict
ProtectHome=yes
NoNewPrivileges=yes
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictNamespaces=yes
RestrictRealtime=yes
MemoryDenyWriteExecute=no
LockPersonality=yes

# Allow write access to config and tsnet state directories
ReadWritePaths={{.ConfigDir}} {{.TsnetStateDir}}

# Device access for USB/GPIB/serial instruments
SupplementaryGroups=plugdev dialout
DeviceAllow=/dev/ttyUSB* rw
DeviceAllow=/dev/ttyACM* rw
DeviceAllow=/dev/usbtmc* rw

[Install]
WantedBy=multi-user.target
`))

// unitData carries the template parameters for the systemd unit file.
type unitData struct {
	ExecPath      string
	ConfigPath    string
	User          string
	ConfigDir     string
	TsnetStateDir string
}

// InstallService creates a system user (if needed), installs udev rules for
// instrument access, writes the systemd unit file, reloads the daemon, and
// enables the service. Parameters:
//
//   - exePath:    absolute path to the galois-edge binary
//   - configPath: absolute path to the config file (e.g. /etc/galois-edge/config.env)
//   - user:       the system user to run as (typically "galois-edge")
func InstallService(exePath, configPath, user string) error {
	if err := ensureUser(user); err != nil {
		return fmt.Errorf("create service user: %w", err)
	}

	configDir := filepath.Dir(configPath)
	tsnetStateDir := filepath.Join(configDir, "tsnet-state")

	// Ensure the tsnet state directory exists and is owned by the service user.
	if err := os.MkdirAll(tsnetStateDir, 0o700); err != nil {
		return fmt.Errorf("create tsnet state dir: %w", err)
	}
	if err := chownPath(tsnetStateDir, user); err != nil {
		return fmt.Errorf("chown tsnet state dir: %w", err)
	}

	// Ensure the config file is readable by the service user.
	if err := chownPath(configPath, user); err != nil {
		return fmt.Errorf("chown config file: %w", err)
	}

	// Install udev rules for instrument device access.
	if err := installUdevRules(); err != nil {
		// Non-fatal: log the warning but continue. The daemon can still work
		// with LAN instruments or if permissions are already correct.
		fmt.Fprintf(os.Stderr, "warning: could not install udev rules: %v\n", err)
	}

	data := unitData{
		ExecPath:      exePath,
		ConfigPath:    configPath,
		User:          user,
		ConfigDir:     configDir,
		TsnetStateDir: tsnetStateDir,
	}

	var buf strings.Builder
	if err := systemdUnitTmpl.Execute(&buf, data); err != nil {
		return fmt.Errorf("render unit template: %w", err)
	}

	if err := os.WriteFile(unitFilePath, []byte(buf.String()), 0o644); err != nil {
		return fmt.Errorf("write unit file %s: %w", unitFilePath, err)
	}

	if err := systemctl("daemon-reload"); err != nil {
		return fmt.Errorf("daemon-reload: %w", err)
	}
	if err := systemctl("enable", unitFileName); err != nil {
		return fmt.Errorf("enable service: %w", err)
	}

	return nil
}

// UninstallService stops and disables the service, removes the unit file
// and udev rules, and reloads systemd.
func UninstallService() error {
	// Best-effort stop — the service may already be stopped.
	_ = systemctl("stop", unitFileName)

	if err := systemctl("disable", unitFileName); err != nil {
		return fmt.Errorf("disable service: %w", err)
	}

	if err := os.Remove(unitFilePath); err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("remove unit file: %w", err)
	}

	if err := systemctl("daemon-reload"); err != nil {
		return fmt.Errorf("daemon-reload: %w", err)
	}

	// Clean up udev rules.
	removeUdevRules()

	return nil
}

// StartService starts the galois-edge systemd service.
func StartService() error {
	return systemctl("start", unitFileName)
}

// StopService stops the galois-edge systemd service.
func StopService() error {
	return systemctl("stop", unitFileName)
}

// ServiceStatus queries systemctl and returns a human-readable state:
// "running", "stopped", "failed", or "unknown".
func ServiceStatus() (string, error) {
	out, err := exec.Command("systemctl", "is-active", unitFileName).CombinedOutput()
	state := strings.TrimSpace(string(out))

	switch state {
	case "active":
		return "running", nil
	case "inactive":
		return "stopped", nil
	case "failed":
		return "failed", nil
	default:
		if err != nil {
			return "unknown", fmt.Errorf("systemctl is-active: %s (%w)", state, err)
		}
		return "unknown", nil
	}
}

// RunAsService is not applicable on Linux where systemd manages the process
// lifecycle directly.
func RunAsService(_ func() error, _ func()) error {
	return fmt.Errorf("RunAsService not supported on Linux (use systemd)")
}

// IsWindowsService always returns false on Linux.
func IsWindowsService() bool { return false }

// ---------------------------------------------------------------------------
// udev rules
// ---------------------------------------------------------------------------

const udevRulesPath = "/etc/udev/rules.d/99-galois-edge.rules"

// udevRules grants the plugdev and dialout groups access to common laboratory
// instrument USB devices (USBTMC, serial adapters) without requiring manual
// configuration. Covers major T&M vendors: Keysight/Agilent, Tektronix,
// Rohde & Schwarz, National Instruments, Rigol, and Siglent.
const udevRules = `# galois-edge — instrument device permissions
# Managed by galois-edge install. Reload: udevadm control --reload-rules

# USBTMC (USB Test & Measurement Class)
SUBSYSTEM=="usb", ATTR{bInterfaceClass}=="fe", ATTR{bInterfaceSubClass}=="03", MODE="0660", GROUP="plugdev"

# Keysight/Agilent (0x0957)
SUBSYSTEM=="usb", ATTR{idVendor}=="0957", MODE="0660", GROUP="plugdev"

# Tektronix (0x0699)
SUBSYSTEM=="usb", ATTR{idVendor}=="0699", MODE="0660", GROUP="plugdev"

# Rohde & Schwarz (0x0aad)
SUBSYSTEM=="usb", ATTR{idVendor}=="0aad", MODE="0660", GROUP="plugdev"

# National Instruments (0x3923)
SUBSYSTEM=="usb", ATTR{idVendor}=="3923", MODE="0660", GROUP="plugdev"

# Rigol (0x1ab1)
SUBSYSTEM=="usb", ATTR{idVendor}=="1ab1", MODE="0660", GROUP="plugdev"

# Siglent (0xf4ec)
SUBSYSTEM=="usb", ATTR{idVendor}=="f4ec", MODE="0660", GROUP="plugdev"

# USBTMC character devices
KERNEL=="usbtmc[0-9]*", MODE="0660", GROUP="plugdev"

# Serial adapters (FTDI, Prolific, CH340, CP210x)
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", MODE="0660", GROUP="dialout"
SUBSYSTEM=="tty", ATTRS{idVendor}=="067b", MODE="0660", GROUP="dialout"
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", MODE="0660", GROUP="dialout"
SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", MODE="0660", GROUP="dialout"
`

// installUdevRules writes the udev rules file and reloads the udev daemon.
func installUdevRules() error {
	if err := os.WriteFile(udevRulesPath, []byte(udevRules), 0o644); err != nil {
		return fmt.Errorf("write %s: %w", udevRulesPath, err)
	}

	// Best-effort reload — may fail on systems without udevadm.
	_ = exec.Command("udevadm", "control", "--reload-rules").Run()
	_ = exec.Command("udevadm", "trigger").Run()

	return nil
}

// removeUdevRules removes the galois-edge udev rules file.
func removeUdevRules() {
	_ = os.Remove(udevRulesPath)
	_ = exec.Command("udevadm", "control", "--reload-rules").Run()
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// ensureUser creates a system user with no home directory and nologin shell,
// and adds it to the plugdev and dialout groups for instrument access.
func ensureUser(user string) error {
	// Check if user already exists.
	if err := exec.Command("id", user).Run(); err == nil {
		return nil
	}

	cmd := exec.Command("useradd",
		"--system",
		"--shell", "/usr/sbin/nologin",
		"--groups", "plugdev,dialout",
		"--no-create-home",
		user,
	)
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("%s: %s", err, strings.TrimSpace(string(out)))
	}
	return nil
}

// chownPath sets ownership of a path to the named user using the chown binary.
func chownPath(path, user string) error {
	cmd := exec.Command("chown", "-R", user+":"+user, path)
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("chown %s: %s (%w)", path, strings.TrimSpace(string(out)), err)
	}
	return nil
}

// systemctl runs a systemctl subcommand with the given arguments.
func systemctl(args ...string) error {
	cmd := exec.Command("systemctl", args...)
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("systemctl %s: %s (%w)",
			strings.Join(args, " "),
			strings.TrimSpace(string(out)),
			err,
		)
	}
	return nil
}
