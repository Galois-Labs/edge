package pisetup

import (
	"bufio"
	"bytes"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// CommandRunner executes a system command and returns combined stdout/stderr.
// It is wrapped so tests can stub `raspi-config`, `usermod`, etc.
type CommandRunner func(name string, args ...string) ([]byte, error)

// DefaultCommandRunner runs the command via os/exec and returns combined output.
func DefaultCommandRunner(name string, args ...string) ([]byte, error) {
	cmd := exec.Command(name, args...)
	return cmd.CombinedOutput()
}

// FixOptions configures the orchestrator and individual fix functions. Tests
// override Runner; production code uses DefaultCommandRunner.
type FixOptions struct {
	// User is the account that runs the daemon (passed to usermod).
	User string
	// DryRun disables all mutating side effects.
	DryRun bool
	// Runner is the command executor. Nil means DefaultCommandRunner.
	Runner CommandRunner
	// Out is where progress lines are written. Nil means os.Stdout.
	Out io.Writer
}

func (o FixOptions) runner() CommandRunner {
	if o.Runner != nil {
		return o.Runner
	}
	return DefaultCommandRunner
}

func (o FixOptions) out() io.Writer {
	if o.Out != nil {
		return o.Out
	}
	return os.Stdout
}

// FixConsoleOnSerial disables the login console on the GPIO UART. Two steps:
//
//  1. raspi-config nonint do_serial_cons 1 — flips the systemd unit and the
//     enable_uart=1 default. Best-effort; if raspi-config is absent we still
//     do step 2.
//  2. Strip console=serial0,... and console=ttyAMA0,... tokens from
//     cmdline.txt via an atomic write.
func FixConsoleOnSerial(opts FixOptions) error {
	out := opts.out()

	if _, err := LookupCommand("raspi-config"); err == nil {
		fmt.Fprintln(out, "  + raspi-config nonint do_serial_cons 1")
		if !opts.DryRun {
			if b, err := opts.runner()("raspi-config", "nonint", "do_serial_cons", "1"); err != nil {
				fmt.Fprintf(out, "    raspi-config failed: %v: %s\n", err, strings.TrimSpace(string(b)))
				// Don't return — cmdline.txt edit below is the real fix.
			}
		}
	} else {
		fmt.Fprintln(out, "  + raspi-config not found; skipping do_serial_cons 1")
	}

	cmdlinePath := ResolveCmdline()
	if cmdlinePath == "" {
		return fmt.Errorf("cmdline.txt not found at %s or %s", CmdlinePathPrimary, CmdlinePathLegacy)
	}

	original, err := os.ReadFile(cmdlinePath)
	if err != nil {
		return fmt.Errorf("read %s: %w", cmdlinePath, err)
	}
	stripped, changed := stripConsoleTokens(original)
	if !changed {
		fmt.Fprintf(out, "  + %s already has no console=serial0/ttyAMA0 token\n", cmdlinePath)
		return nil
	}

	fmt.Fprintf(out, "  + edit %s (strip console=serial0/ttyAMA0 tokens)\n", cmdlinePath)
	fmt.Fprintf(out, "    before: %s\n", strings.TrimRight(string(original), "\n"))
	fmt.Fprintf(out, "    after:  %s\n", strings.TrimRight(string(stripped), "\n"))
	if opts.DryRun {
		return nil
	}
	if err := atomicWrite(cmdlinePath, stripped, 0o644); err != nil {
		return fmt.Errorf("write %s: %w", cmdlinePath, err)
	}
	return nil
}

// stripConsoleTokens removes any whitespace-separated `console=serial0...` or
// `console=ttyAMA0...` token from a cmdline.txt body. cmdline.txt must be
// exactly one line (the kernel parses only the first line), so we preserve
// the surrounding whitespace as best we can.
func stripConsoleTokens(in []byte) ([]byte, bool) {
	// cmdline.txt is one line; collapse to first line, preserve trailing newline.
	hasNewline := bytes.HasSuffix(in, []byte("\n"))
	line := strings.TrimRight(string(in), "\r\n")
	tokens := strings.Fields(line)
	out := make([]string, 0, len(tokens))
	changed := false
	for _, tok := range tokens {
		if strings.HasPrefix(tok, "console=serial0") || strings.HasPrefix(tok, "console=ttyAMA0") {
			changed = true
			continue
		}
		out = append(out, tok)
	}
	if !changed {
		return in, false
	}
	rebuilt := strings.Join(out, " ")
	if hasNewline {
		rebuilt += "\n"
	}
	return []byte(rebuilt), true
}

// FixBluetoothOnPL011 ensures `dtoverlay=disable-bt` is present in config.txt
// and disables the hciuart helper. The append is idempotent: if the line is
// already present (commented or not) we don't double-add.
func FixBluetoothOnPL011(opts FixOptions) error {
	out := opts.out()
	path := ResolveConfigTxt()
	if path == "" {
		return fmt.Errorf("config.txt not found at %s or %s", ConfigTxtPathPrimary, ConfigTxtPathLegacy)
	}

	original, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("read %s: %w", path, err)
	}

	if hasDisableBT(original) {
		fmt.Fprintf(out, "  + %s already has dtoverlay=disable-bt\n", path)
	} else {
		appended := appendDisableBT(original)
		fmt.Fprintf(out, "  + append dtoverlay=disable-bt to %s\n", path)
		if !opts.DryRun {
			if err := atomicWrite(path, appended, 0o644); err != nil {
				return fmt.Errorf("write %s: %w", path, err)
			}
		}
	}

	if _, err := LookupCommand("systemctl"); err != nil {
		fmt.Fprintf(out, "  + systemctl not found; skipping disable hciuart\n")
		return nil
	}
	if opts.DryRun {
		fmt.Fprintln(out, "  + systemctl disable hciuart (if present)")
		return nil
	}
	// hciuart only exists on Pi 3/4 images; Pi 5 and Pi Zero W do not ship it.
	// Probe with list-unit-files so a missing unit is a clean skip, not a warning.
	if b, err := opts.runner()("systemctl", "list-unit-files", "--no-legend", "hciuart.service"); err != nil || !strings.Contains(string(b), "hciuart.service") {
		fmt.Fprintln(out, "  + hciuart.service not present on this image (Pi 5 / Zero W); skipping")
		return nil
	}
	fmt.Fprintln(out, "  + systemctl disable hciuart")
	if b, err := opts.runner()("systemctl", "disable", "hciuart"); err != nil {
		// Already-disabled is benign; only louder errors warrant a warning.
		fmt.Fprintf(out, "    systemctl disable hciuart returned: %v: %s\n", err, strings.TrimSpace(string(b)))
	}
	return nil
}

// hasDisableBT reports whether config.txt already contains an active or
// commented dtoverlay=disable-bt directive. We treat commented lines as
// "already present" to avoid duplicates the user toggled off.
func hasDisableBT(data []byte) bool {
	scanner := bufio.NewScanner(bytes.NewReader(data))
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if strings.HasPrefix(line, "#") {
			continue
		}
		if strings.Contains(line, "dtoverlay=disable-bt") || strings.Contains(line, "dtoverlay=pi3-disable-bt") {
			return true
		}
	}
	return false
}

// appendDisableBT appends `dtoverlay=disable-bt` and a galois-edge marker
// comment, ensuring a leading newline if the file doesn't already end in one.
func appendDisableBT(data []byte) []byte {
	var b bytes.Buffer
	b.Write(data)
	if len(data) > 0 && !bytes.HasSuffix(data, []byte("\n")) {
		b.WriteByte('\n')
	}
	b.WriteString("\n# Added by galois-edge pi-setup: free PL011 UART for /dev/serial0.\n")
	b.WriteString("dtoverlay=disable-bt\n")
	return b.Bytes()
}

// FixUserNotInDialout adds the named user to the dialout group via usermod.
// Caller must log out and back in for the new group to take effect; we print
// a reminder.
func FixUserNotInDialout(opts FixOptions) error {
	out := opts.out()
	if opts.User == "" {
		return fmt.Errorf("user is empty; cannot add to dialout")
	}
	if _, err := LookupCommand("usermod"); err != nil {
		return fmt.Errorf("usermod not found in PATH: %w", err)
	}
	fmt.Fprintf(out, "  + usermod -aG dialout %s\n", opts.User)
	if opts.DryRun {
		fmt.Fprintf(out, "  + reminder: %s must log out and back in for the new group to take effect\n", opts.User)
		return nil
	}
	if b, err := opts.runner()("usermod", "-aG", "dialout", opts.User); err != nil {
		return fmt.Errorf("usermod -aG dialout %s: %w: %s", opts.User, err, strings.TrimSpace(string(b)))
	}
	fmt.Fprintf(out, "  + reminder: %s must log out and back in for the new group to take effect\n", opts.User)
	return nil
}

// atomicWrite writes data to path via a sibling tempfile + rename. The temp
// file is created in the same directory so rename is atomic on the same
// filesystem.
func atomicWrite(path string, data []byte, perm os.FileMode) error {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".pisetup-*")
	if err != nil {
		return fmt.Errorf("create temp in %s: %w", dir, err)
	}
	tmpPath := tmp.Name()
	cleanup := func() { _ = os.Remove(tmpPath) }

	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		cleanup()
		return fmt.Errorf("write temp: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		cleanup()
		return fmt.Errorf("fsync temp: %w", err)
	}
	if err := tmp.Close(); err != nil {
		cleanup()
		return fmt.Errorf("close temp: %w", err)
	}
	if err := os.Chmod(tmpPath, perm); err != nil {
		cleanup()
		return fmt.Errorf("chmod temp: %w", err)
	}
	if err := os.Rename(tmpPath, path); err != nil {
		cleanup()
		return fmt.Errorf("rename %s -> %s: %w", tmpPath, path, err)
	}
	return nil
}
