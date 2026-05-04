// Package pisetup implements detection and remediation of Raspberry Pi OS-level
// gotchas that prevent the galois-edge daemon from using the GPIO UART
// (/dev/serial0, /dev/ttyAMA0) for serial-instrument communication.
//
// The three issues handled here mirror src/galois_edge/pi_diagnostics.py on the
// Python side:
//
//  1. A login getty attached to /dev/ttyAMA0 / serial0 pollutes any read.
//  2. On Pi 3+/4/5/Zero 2 W, Bluetooth claims the high-quality PL011 UART so
//     /dev/serial0 aliases to the worse mini-UART.
//  3. The user that runs the daemon must be in the dialout group to open
//     /dev/serial0 without root privileges.
//
// Detection is pure (read-only); the fixes live in fix.go and the orchestrator
// is in orchestrator.go. ModelPath / CmdlinePath / ConfigTxtPath are package
// variables so tests can redirect them to a sandbox.
package pisetup

import (
	"bufio"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"os/user"
	"strings"
)

// File and command paths used by detection and fixes. Exposed as package
// variables so tests can stub them. Treat as read-only at runtime — the CLI
// does not flip them; only test code does.
var (
	// ModelPath is the device-tree node that identifies the Pi model.
	ModelPath = "/proc/device-tree/model"

	// CmdlinePathPrimary is the bootloader cmdline location on modern
	// Raspberry Pi OS (Bookworm and later).
	CmdlinePathPrimary = "/boot/firmware/cmdline.txt"

	// CmdlinePathLegacy is the fallback for pre-Bookworm Raspberry Pi OS.
	CmdlinePathLegacy = "/boot/cmdline.txt"

	// ConfigTxtPathPrimary is the bootloader config.txt location on modern
	// Raspberry Pi OS.
	ConfigTxtPathPrimary = "/boot/firmware/config.txt"

	// ConfigTxtPathLegacy is the fallback for pre-Bookworm Raspberry Pi OS.
	ConfigTxtPathLegacy = "/boot/config.txt"
)

// IssueKind identifies one of the three Pi UART issues.
type IssueKind int

const (
	// IssueConsoleOnSerial means a getty/console is attached to the GPIO UART.
	IssueConsoleOnSerial IssueKind = iota
	// IssueBluetoothOnPL011 means Bluetooth is bound to the PL011, forcing
	// /dev/serial0 to alias to the inferior mini-UART.
	IssueBluetoothOnPL011
	// IssueUserNotInDialout means the daemon user can't open /dev/serial0
	// without root because it isn't in the dialout group.
	IssueUserNotInDialout
)

// String returns a short, human-readable label for an IssueKind.
func (k IssueKind) String() string {
	switch k {
	case IssueConsoleOnSerial:
		return "console-on-serial"
	case IssueBluetoothOnPL011:
		return "bluetooth-on-pl011"
	case IssueUserNotInDialout:
		return "user-not-in-dialout"
	default:
		return "unknown"
	}
}

// Detection captures the result of a single detection probe. NeedsFix is true
// when the issue is present; OK is the inverse and is provided for clarity in
// summary tables. Detail is a short string suitable for printing.
type Detection struct {
	Kind     IssueKind
	NeedsFix bool
	Detail   string
}

// OK reports whether the system is already in the desired state for this
// issue.
func (d Detection) OK() bool { return !d.NeedsFix }

// IsRaspberryPi reports whether the host appears to be a Raspberry Pi by
// reading /proc/device-tree/model. Any read error or absence of "Raspberry Pi"
// in the model string returns false.
func IsRaspberryPi() (bool, string) {
	data, err := os.ReadFile(ModelPath)
	if err != nil {
		return false, ""
	}
	// device-tree strings are NUL-terminated; trim it for display.
	model := strings.TrimRight(string(data), "\x00\n\r\t ")
	if model == "" {
		return false, ""
	}
	return strings.Contains(strings.ToLower(model), "raspberry pi"), model
}

// ResolveCmdline returns the cmdline.txt path that exists, preferring the
// Bookworm location. Returns "" if neither file exists.
func ResolveCmdline() string {
	if _, err := os.Stat(CmdlinePathPrimary); err == nil {
		return CmdlinePathPrimary
	}
	if _, err := os.Stat(CmdlinePathLegacy); err == nil {
		return CmdlinePathLegacy
	}
	return ""
}

// ResolveConfigTxt returns the config.txt path that exists, preferring the
// Bookworm location. Returns "" if neither file exists.
func ResolveConfigTxt() string {
	if _, err := os.Stat(ConfigTxtPathPrimary); err == nil {
		return ConfigTxtPathPrimary
	}
	if _, err := os.Stat(ConfigTxtPathLegacy); err == nil {
		return ConfigTxtPathLegacy
	}
	return ""
}

// DetectConsoleOnSerial inspects cmdline.txt for `console=serial0,...` or
// `console=ttyAMA0,...` tokens. Either of those keeps a getty attached.
func DetectConsoleOnSerial() Detection {
	path := ResolveCmdline()
	if path == "" {
		return Detection{
			Kind:     IssueConsoleOnSerial,
			NeedsFix: false,
			Detail:   "cmdline.txt not found (not a Pi boot layout) — skipping",
		}
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return Detection{
			Kind:     IssueConsoleOnSerial,
			NeedsFix: false,
			Detail:   fmt.Sprintf("could not read %s: %v", path, err),
		}
	}
	tokens := strings.Fields(string(data))
	for _, tok := range tokens {
		if strings.HasPrefix(tok, "console=serial0") || strings.HasPrefix(tok, "console=ttyAMA0") {
			return Detection{
				Kind:     IssueConsoleOnSerial,
				NeedsFix: true,
				Detail:   fmt.Sprintf("%s contains %q (login console attached to UART)", path, tok),
			}
		}
	}
	return Detection{
		Kind:     IssueConsoleOnSerial,
		NeedsFix: false,
		Detail:   fmt.Sprintf("%s has no console=serial0/ttyAMA0 token", path),
	}
}

// DetectBluetoothOnPL011 inspects config.txt for `dtoverlay=disable-bt`. If
// the overlay is absent, Bluetooth claims the PL011 on Pi 3+/4/5/Zero 2 W and
// /dev/serial0 falls back to the mini-UART.
func DetectBluetoothOnPL011() Detection {
	path := ResolveConfigTxt()
	if path == "" {
		return Detection{
			Kind:     IssueBluetoothOnPL011,
			NeedsFix: false,
			Detail:   "config.txt not found — skipping",
		}
	}
	f, err := os.Open(path)
	if err != nil {
		return Detection{
			Kind:     IssueBluetoothOnPL011,
			NeedsFix: false,
			Detail:   fmt.Sprintf("could not read %s: %v", path, err),
		}
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if strings.HasPrefix(line, "#") {
			continue
		}
		// Accept either disable-bt or pi3-disable-bt (legacy alias).
		if strings.Contains(line, "dtoverlay=disable-bt") || strings.Contains(line, "dtoverlay=pi3-disable-bt") {
			return Detection{
				Kind:     IssueBluetoothOnPL011,
				NeedsFix: false,
				Detail:   fmt.Sprintf("%s already has dtoverlay=disable-bt", path),
			}
		}
	}
	return Detection{
		Kind:     IssueBluetoothOnPL011,
		NeedsFix: true,
		Detail:   fmt.Sprintf("%s missing dtoverlay=disable-bt (Bluetooth claims PL011)", path),
	}
}

// DetectUserNotInDialout reports whether the named user is in the dialout
// group. If username is empty, the current real user is used. On systems with
// no dialout group at all, NeedsFix is false (nothing actionable).
func DetectUserNotInDialout(username string) Detection {
	if username == "" {
		if u, err := user.Current(); err == nil {
			username = u.Username
		}
	}
	if username == "" {
		return Detection{
			Kind:     IssueUserNotInDialout,
			NeedsFix: false,
			Detail:   "could not determine user; skipping dialout check",
		}
	}

	u, err := user.Lookup(username)
	if err != nil {
		return Detection{
			Kind:     IssueUserNotInDialout,
			NeedsFix: false,
			Detail:   fmt.Sprintf("user %q not found: %v", username, err),
		}
	}

	dialout, err := user.LookupGroup("dialout")
	if err != nil {
		return Detection{
			Kind:     IssueUserNotInDialout,
			NeedsFix: false,
			Detail:   "dialout group not present on this system",
		}
	}

	gids, err := u.GroupIds()
	if err != nil {
		return Detection{
			Kind:     IssueUserNotInDialout,
			NeedsFix: false,
			Detail:   fmt.Sprintf("could not enumerate groups for %s: %v", username, err),
		}
	}
	for _, gid := range gids {
		if gid == dialout.Gid {
			return Detection{
				Kind:     IssueUserNotInDialout,
				NeedsFix: false,
				Detail:   fmt.Sprintf("user %s is in dialout", username),
			}
		}
	}
	return Detection{
		Kind:     IssueUserNotInDialout,
		NeedsFix: true,
		Detail:   fmt.Sprintf("user %s is NOT in dialout (cannot open /dev/serial0)", username),
	}
}

// DetectAll runs the full detection sweep against the named user and returns
// the results in a stable order.
func DetectAll(username string) []Detection {
	return []Detection{
		DetectConsoleOnSerial(),
		DetectBluetoothOnPL011(),
		DetectUserNotInDialout(username),
	}
}

// LookupCommand wraps exec.LookPath but unwraps the *exec.Error to a sentinel
// when the binary is missing. Callers typically only care about presence.
func LookupCommand(name string) (string, error) {
	path, err := exec.LookPath(name)
	if err != nil {
		var lpErr *exec.Error
		if errors.As(err, &lpErr) {
			return "", lpErr
		}
		return "", err
	}
	return path, nil
}
