package pisetup

import (
	"bufio"
	"fmt"
	"io"
	"os"
	"strings"
)

// RunOptions controls a full pi-setup run.
type RunOptions struct {
	// User is the daemon account to add to the dialout group. Empty means
	// "current user", with SUDO_USER preferred over the effective uid when
	// running under sudo.
	User string
	// DryRun prints the plan and exits without modifying anything.
	DryRun bool
	// Yes skips the interactive confirmation prompt.
	Yes bool
	// Reboot calls `systemctl reboot` after applying fixes.
	Reboot bool
	// In supplies the prompt's stdin source; defaults to os.Stdin.
	In io.Reader
	// Out captures progress; defaults to os.Stdout.
	Out io.Writer
	// Runner stubs system commands; defaults to DefaultCommandRunner.
	Runner CommandRunner
	// IsRoot is the privilege check used for the early exit. Defaults to
	// `os.Geteuid() == 0`. Tests override this to bypass the gate.
	IsRoot func() bool
	// DetectPi is the platform check. Defaults to IsRaspberryPi. Tests
	// override this to simulate Pi vs non-Pi.
	DetectPi func() (bool, string)
}

// Result summarizes a Run() invocation.
type Result struct {
	// IsPi is true if the host appears to be a Raspberry Pi.
	IsPi bool
	// Model is the device-tree model string (empty on non-Pi).
	Model string
	// Detections is the full pre-fix detection sweep.
	Detections []Detection
	// Applied is the set of fixes attempted (in order).
	Applied []AppliedFix
	// RebootRequired is true if any cmdline.txt / config.txt edit landed.
	RebootRequired bool
}

// AppliedFix records the outcome of one fix step.
type AppliedFix struct {
	Kind IssueKind
	Err  error
}

// Run executes the full pi-setup flow. It returns a Result and a non-nil
// error only for unrecoverable problems (e.g., not running as root). A
// per-fix failure is recorded in Result.Applied[i].Err and contributes to a
// non-zero process exit handled by the caller.
func Run(opts RunOptions) (Result, error) {
	if opts.Out == nil {
		opts.Out = os.Stdout
	}
	if opts.In == nil {
		opts.In = os.Stdin
	}
	if opts.Runner == nil {
		opts.Runner = DefaultCommandRunner
	}
	if opts.IsRoot == nil {
		opts.IsRoot = func() bool { return os.Geteuid() == 0 }
	}
	if opts.DetectPi == nil {
		opts.DetectPi = IsRaspberryPi
	}

	out := opts.Out
	res := Result{}

	// 1. Pi detection.
	isPi, model := opts.DetectPi()
	res.IsPi = isPi
	res.Model = model
	if !isPi {
		fmt.Fprintln(out, "Not a Raspberry Pi — nothing to do.")
		return res, nil
	}
	fmt.Fprintf(out, "Detected: %s\n", model)
	fmt.Fprintln(out)

	// 2. Resolve user. Prefer SUDO_USER when running under sudo so we add
	// the human's account to dialout, not root.
	user := opts.User
	if user == "" {
		if sudoUser := os.Getenv("SUDO_USER"); sudoUser != "" && sudoUser != "root" {
			user = sudoUser
		}
	}
	if user == "" {
		if u := currentUsername(); u != "" {
			user = u
		}
	}

	// 3. Detection sweep.
	detections := DetectAll(user)
	res.Detections = detections
	printSummary(out, detections)

	needsAny := false
	for _, d := range detections {
		if d.NeedsFix {
			needsAny = true
			break
		}
	}
	if !needsAny {
		fmt.Fprintln(out)
		fmt.Fprintln(out, "All checks passed. Nothing to do.")
		return res, nil
	}

	// 4. Print plan.
	fmt.Fprintln(out)
	fmt.Fprintln(out, "Proposed fixes:")
	for _, d := range detections {
		if !d.NeedsFix {
			continue
		}
		printPlan(out, d, user)
	}

	if opts.DryRun {
		fmt.Fprintln(out)
		fmt.Fprintln(out, "[dry-run] no changes applied.")
		return res, nil
	}

	// 5. Privilege gate (only when actually applying).
	if !opts.IsRoot() {
		return res, fmt.Errorf("pi-setup must run as root; re-run with sudo")
	}

	// 6. Confirmation.
	if !opts.Yes {
		fmt.Fprintln(out)
		fmt.Fprint(out, "Apply all fixes? [y/N] ")
		if !confirm(opts.In) {
			fmt.Fprintln(out, "Aborted.")
			return res, nil
		}
	}

	fmt.Fprintln(out)
	fmt.Fprintln(out, "Applying fixes:")

	fixOpts := FixOptions{
		User:   user,
		DryRun: false,
		Runner: opts.Runner,
		Out:    out,
	}

	for _, d := range detections {
		if !d.NeedsFix {
			continue
		}
		fmt.Fprintf(out, " * %s\n", d.Kind)
		var err error
		switch d.Kind {
		case IssueConsoleOnSerial:
			err = FixConsoleOnSerial(fixOpts)
			if err == nil {
				res.RebootRequired = true
			}
		case IssueBluetoothOnPL011:
			err = FixBluetoothOnPL011(fixOpts)
			if err == nil {
				res.RebootRequired = true
			}
		case IssueUserNotInDialout:
			err = FixUserNotInDialout(fixOpts)
		}
		if err != nil {
			fmt.Fprintf(out, "    FAILED: %v\n", err)
		}
		res.Applied = append(res.Applied, AppliedFix{Kind: d.Kind, Err: err})
	}

	fmt.Fprintln(out)
	if res.RebootRequired {
		fmt.Fprintln(out, "Done. A reboot is required for cmdline + config.txt changes to take effect.")
	} else {
		fmt.Fprintln(out, "Done.")
	}

	if opts.Reboot {
		fmt.Fprintln(out, "Triggering systemctl reboot...")
		if _, err := opts.Runner("systemctl", "reboot"); err != nil {
			return res, fmt.Errorf("systemctl reboot: %w", err)
		}
	} else if res.RebootRequired {
		fmt.Fprintln(out, "Reboot now with: sudo systemctl reboot")
	}

	return res, nil
}

// HasFailures reports whether any applied fix returned an error.
func (r Result) HasFailures() bool {
	for _, a := range r.Applied {
		if a.Err != nil {
			return true
		}
	}
	return false
}

func printSummary(w io.Writer, detections []Detection) {
	fmt.Fprintln(w, "Detection summary:")
	for _, d := range detections {
		marker := "[ OK ]"
		if d.NeedsFix {
			marker = "[FIX!]"
		}
		fmt.Fprintf(w, "  %s %-22s %s\n", marker, d.Kind, d.Detail)
	}
}

func printPlan(w io.Writer, d Detection, user string) {
	switch d.Kind {
	case IssueConsoleOnSerial:
		fmt.Fprintln(w, "  - disable login console on /dev/ttyAMA0:")
		fmt.Fprintln(w, "      raspi-config nonint do_serial_cons 1")
		fmt.Fprintln(w, "      strip console=serial0,*  console=ttyAMA0,* tokens from cmdline.txt")
	case IssueBluetoothOnPL011:
		fmt.Fprintln(w, "  - free the PL011 UART from Bluetooth:")
		fmt.Fprintln(w, "      append dtoverlay=disable-bt to config.txt")
		fmt.Fprintln(w, "      systemctl disable hciuart")
	case IssueUserNotInDialout:
		fmt.Fprintf(w, "  - add %s to dialout group:\n", user)
		fmt.Fprintf(w, "      usermod -aG dialout %s\n", user)
	}
}

func confirm(r io.Reader) bool {
	scanner := bufio.NewScanner(r)
	if !scanner.Scan() {
		return false
	}
	resp := strings.TrimSpace(strings.ToLower(scanner.Text()))
	return resp == "y" || resp == "yes"
}

// currentUsername returns the running user's name; "" on lookup failure.
// Defined as a hook so tests on weird CI envs can override.
var currentUsername = func() string {
	// os/user.Current() requires cgo on some platforms; fallback to env vars.
	if u := os.Getenv("USER"); u != "" {
		return u
	}
	if u := os.Getenv("LOGNAME"); u != "" {
		return u
	}
	return ""
}
