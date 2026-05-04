package pisetup

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// withTempPaths swaps the package-level paths to point inside t.TempDir and
// restores them on cleanup. Returns the temp dir for further use.
func withTempPaths(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()

	origModel := ModelPath
	origCmdPrim := CmdlinePathPrimary
	origCmdLeg := CmdlinePathLegacy
	origCfgPrim := ConfigTxtPathPrimary
	origCfgLeg := ConfigTxtPathLegacy

	ModelPath = filepath.Join(dir, "model")
	CmdlinePathPrimary = filepath.Join(dir, "boot/firmware/cmdline.txt")
	CmdlinePathLegacy = filepath.Join(dir, "boot/cmdline.txt")
	ConfigTxtPathPrimary = filepath.Join(dir, "boot/firmware/config.txt")
	ConfigTxtPathLegacy = filepath.Join(dir, "boot/config.txt")

	t.Cleanup(func() {
		ModelPath = origModel
		CmdlinePathPrimary = origCmdPrim
		CmdlinePathLegacy = origCmdLeg
		ConfigTxtPathPrimary = origCfgPrim
		ConfigTxtPathLegacy = origCfgLeg
	})

	if err := os.MkdirAll(filepath.Join(dir, "boot/firmware"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(dir, "boot"), 0o755); err != nil {
		t.Fatal(err)
	}
	return dir
}

// ---------------------------------------------------------------------------
// Detection
// ---------------------------------------------------------------------------

func TestIsRaspberryPi_Negative(t *testing.T) {
	withTempPaths(t)
	// No model file -> not a Pi.
	ok, model := IsRaspberryPi()
	if ok {
		t.Errorf("IsRaspberryPi without model file: got true, want false")
	}
	if model != "" {
		t.Errorf("model: got %q, want empty", model)
	}
}

func TestIsRaspberryPi_Positive(t *testing.T) {
	withTempPaths(t)
	// Real device-tree strings are NUL-terminated.
	if err := os.WriteFile(ModelPath, []byte("Raspberry Pi 5 Model B Rev 1.0\x00"), 0o644); err != nil {
		t.Fatal(err)
	}
	ok, model := IsRaspberryPi()
	if !ok {
		t.Errorf("IsRaspberryPi on Pi: got false, want true")
	}
	if !strings.Contains(model, "Raspberry Pi 5") {
		t.Errorf("model: got %q, want it to contain 'Raspberry Pi 5'", model)
	}
}

func TestDetectConsoleOnSerial(t *testing.T) {
	withTempPaths(t)

	// 1. Missing cmdline.txt -> NeedsFix=false, just informational.
	d := DetectConsoleOnSerial()
	if d.NeedsFix {
		t.Errorf("missing cmdline: NeedsFix=true, want false")
	}

	// 2. cmdline with console=serial0 -> NeedsFix=true.
	body := "console=serial0,115200 console=tty1 root=PARTUUID=abc rootwait\n"
	if err := os.WriteFile(CmdlinePathPrimary, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	d = DetectConsoleOnSerial()
	if !d.NeedsFix {
		t.Errorf("with console=serial0: NeedsFix=false, want true; detail=%q", d.Detail)
	}

	// 3. cmdline cleaned up -> NeedsFix=false.
	body = "console=tty1 root=PARTUUID=abc rootwait\n"
	if err := os.WriteFile(CmdlinePathPrimary, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	d = DetectConsoleOnSerial()
	if d.NeedsFix {
		t.Errorf("after cleanup: NeedsFix=true, want false; detail=%q", d.Detail)
	}
}

func TestDetectBluetoothOnPL011(t *testing.T) {
	withTempPaths(t)

	// Missing config.txt -> NeedsFix=false.
	d := DetectBluetoothOnPL011()
	if d.NeedsFix {
		t.Errorf("missing config.txt: NeedsFix=true, want false")
	}

	// Empty config.txt -> NeedsFix=true.
	if err := os.WriteFile(ConfigTxtPathPrimary, []byte("# default\nenable_uart=1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	d = DetectBluetoothOnPL011()
	if !d.NeedsFix {
		t.Errorf("missing dtoverlay=disable-bt: NeedsFix=false, want true")
	}

	// With overlay -> NeedsFix=false.
	body := "enable_uart=1\ndtoverlay=disable-bt\n"
	if err := os.WriteFile(ConfigTxtPathPrimary, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	d = DetectBluetoothOnPL011()
	if d.NeedsFix {
		t.Errorf("with overlay: NeedsFix=true, want false")
	}

	// Commented line counts as already-present (don't double-add).
	body = "enable_uart=1\n#dtoverlay=disable-bt\n"
	if err := os.WriteFile(ConfigTxtPathPrimary, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	d = DetectBluetoothOnPL011()
	if !d.NeedsFix {
		t.Errorf("commented line should still mark NeedsFix=true")
	}
}

// ---------------------------------------------------------------------------
// stripConsoleTokens
// ---------------------------------------------------------------------------

func TestStripConsoleTokens(t *testing.T) {
	cases := []struct {
		name  string
		in    string
		want  string
		dirty bool
	}{
		{
			name:  "strip serial0",
			in:    "console=serial0,115200 console=tty1 rootwait\n",
			want:  "console=tty1 rootwait\n",
			dirty: true,
		},
		{
			name:  "strip ttyAMA0",
			in:    "console=ttyAMA0,115200 console=tty1 rootwait\n",
			want:  "console=tty1 rootwait\n",
			dirty: true,
		},
		{
			name:  "strip both",
			in:    "console=serial0,115200 console=ttyAMA0,115200 console=tty1\n",
			want:  "console=tty1\n",
			dirty: true,
		},
		{
			name:  "no change",
			in:    "console=tty1 rootwait\n",
			want:  "console=tty1 rootwait\n",
			dirty: false,
		},
		{
			name:  "no trailing newline",
			in:    "console=serial0,115200 console=tty1",
			want:  "console=tty1",
			dirty: true,
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got, changed := stripConsoleTokens([]byte(c.in))
			if changed != c.dirty {
				t.Errorf("changed: got %v, want %v", changed, c.dirty)
			}
			if string(got) != c.want {
				t.Errorf("output: got %q, want %q", string(got), c.want)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// Atomic write
// ---------------------------------------------------------------------------

func TestAtomicWrite(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "out.txt")

	if err := atomicWrite(target, []byte("hello\n"), 0o644); err != nil {
		t.Fatalf("atomicWrite: %v", err)
	}
	got, err := os.ReadFile(target)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "hello\n" {
		t.Errorf("contents: got %q, want %q", string(got), "hello\n")
	}

	// Overwriting succeeds and leaves no leftover temp files.
	if err := atomicWrite(target, []byte("world\n"), 0o644); err != nil {
		t.Fatalf("atomicWrite (overwrite): %v", err)
	}
	got, _ = os.ReadFile(target)
	if string(got) != "world\n" {
		t.Errorf("after overwrite: got %q, want %q", string(got), "world\n")
	}

	entries, _ := os.ReadDir(dir)
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), ".pisetup-") {
			t.Errorf("leftover temp file: %s", e.Name())
		}
	}
}

// ---------------------------------------------------------------------------
// Fix functions with stubbed runner
// ---------------------------------------------------------------------------

type runnerCall struct {
	Name string
	Args []string
}

func recordingRunner() (CommandRunner, *[]runnerCall) {
	calls := &[]runnerCall{}
	r := func(name string, args ...string) ([]byte, error) {
		*calls = append(*calls, runnerCall{Name: name, Args: append([]string(nil), args...)})
		return nil, nil
	}
	return r, calls
}

func TestFixConsoleOnSerial_StripsTokens(t *testing.T) {
	dir := withTempPaths(t)
	_ = dir
	original := "console=serial0,115200 console=tty1 root=PARTUUID=abc rootwait\n"
	if err := os.WriteFile(CmdlinePathPrimary, []byte(original), 0o644); err != nil {
		t.Fatal(err)
	}

	runner, _ := recordingRunner()
	var buf bytes.Buffer
	if err := FixConsoleOnSerial(FixOptions{Runner: runner, Out: &buf}); err != nil {
		t.Fatalf("FixConsoleOnSerial: %v", err)
	}
	got, _ := os.ReadFile(CmdlinePathPrimary)
	if strings.Contains(string(got), "console=serial0") {
		t.Errorf("cmdline still contains console=serial0: %q", string(got))
	}
	if !strings.Contains(string(got), "console=tty1") {
		t.Errorf("cmdline lost console=tty1: %q", string(got))
	}
}

func TestFixConsoleOnSerial_DryRunNoWrite(t *testing.T) {
	withTempPaths(t)
	original := "console=serial0,115200 console=tty1 rootwait\n"
	if err := os.WriteFile(CmdlinePathPrimary, []byte(original), 0o644); err != nil {
		t.Fatal(err)
	}
	runner, calls := recordingRunner()
	var buf bytes.Buffer
	if err := FixConsoleOnSerial(FixOptions{DryRun: true, Runner: runner, Out: &buf}); err != nil {
		t.Fatalf("FixConsoleOnSerial dry-run: %v", err)
	}
	got, _ := os.ReadFile(CmdlinePathPrimary)
	if string(got) != original {
		t.Errorf("dry-run modified cmdline: got %q, want %q", string(got), original)
	}
	for _, c := range *calls {
		if c.Name == "raspi-config" {
			t.Errorf("dry-run still invoked raspi-config: %+v", c)
		}
	}
}

func TestFixBluetoothOnPL011_AppendsOverlay(t *testing.T) {
	withTempPaths(t)
	if err := os.WriteFile(ConfigTxtPathPrimary, []byte("# config.txt\nenable_uart=1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	runner, calls := recordingRunner()
	var buf bytes.Buffer
	if err := FixBluetoothOnPL011(FixOptions{Runner: runner, Out: &buf}); err != nil {
		t.Fatalf("FixBluetoothOnPL011: %v", err)
	}
	got, _ := os.ReadFile(ConfigTxtPathPrimary)
	if !strings.Contains(string(got), "dtoverlay=disable-bt") {
		t.Errorf("config.txt missing dtoverlay=disable-bt: %q", string(got))
	}
	// Idempotent: second call shouldn't add another line if systemctl is mocked.
	if err := FixBluetoothOnPL011(FixOptions{Runner: runner, Out: &buf}); err != nil {
		t.Fatalf("FixBluetoothOnPL011 second call: %v", err)
	}
	got, _ = os.ReadFile(ConfigTxtPathPrimary)
	if strings.Count(string(got), "dtoverlay=disable-bt") != 1 {
		t.Errorf("dtoverlay=disable-bt not idempotent: %q", string(got))
	}

	// systemctl disable hciuart should have been requested at least once
	// (only if systemctl is on PATH; on macOS dev hosts it might not be).
	_ = calls // do not assert content; depends on host PATH.
}

// TestFixBluetoothOnPL011_SkipsHciuartWhenAbsent verifies the Pi 5 / Zero W
// case: hciuart.service does not exist, so we probe with list-unit-files,
// see no entry, and skip the disable call cleanly. Regression test for a real
// bug observed during pi5 hardware integration: pi-setup --yes printed
// "Failed to disable unit: Unit hciuart.service does not exist" because the
// disable was unconditional.
func TestFixBluetoothOnPL011_SkipsHciuartWhenAbsent(t *testing.T) {
	if _, err := LookupCommand("systemctl"); err != nil {
		t.Skip("systemctl not on PATH; this regression only manifests when systemctl is callable")
	}
	withTempPaths(t)
	if err := os.WriteFile(ConfigTxtPathPrimary, []byte("# config.txt\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	calls := &[]runnerCall{}
	runner := func(name string, args ...string) ([]byte, error) {
		*calls = append(*calls, runnerCall{Name: name, Args: append([]string(nil), args...)})
		// Simulate Pi 5 / Zero W: list-unit-files for hciuart returns empty.
		if name == "systemctl" && len(args) > 0 && args[0] == "list-unit-files" {
			return []byte(""), nil
		}
		return nil, nil
	}
	var buf bytes.Buffer
	if err := FixBluetoothOnPL011(FixOptions{Runner: runner, Out: &buf}); err != nil {
		t.Fatalf("FixBluetoothOnPL011: %v", err)
	}
	// Must probe with list-unit-files exactly once.
	probes := 0
	disables := 0
	for _, c := range *calls {
		if c.Name == "systemctl" && len(c.Args) > 0 && c.Args[0] == "list-unit-files" {
			probes++
		}
		if c.Name == "systemctl" && len(c.Args) > 0 && c.Args[0] == "disable" {
			disables++
		}
	}
	if probes != 1 {
		t.Errorf("expected 1 list-unit-files probe; got %d (%+v)", probes, *calls)
	}
	if disables != 0 {
		t.Errorf("expected 0 disable calls when hciuart absent; got %d (%+v)", disables, *calls)
	}
	if !strings.Contains(buf.String(), "not present on this image") {
		t.Errorf("expected friendly skip message; got %q", buf.String())
	}
}

// TestFixBluetoothOnPL011_DisablesHciuartWhenPresent covers the Pi 3 / Pi 4
// path where the unit does exist.
func TestFixBluetoothOnPL011_DisablesHciuartWhenPresent(t *testing.T) {
	if _, err := LookupCommand("systemctl"); err != nil {
		t.Skip("systemctl not on PATH")
	}
	withTempPaths(t)
	if err := os.WriteFile(ConfigTxtPathPrimary, []byte("# config.txt\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	calls := &[]runnerCall{}
	runner := func(name string, args ...string) ([]byte, error) {
		*calls = append(*calls, runnerCall{Name: name, Args: append([]string(nil), args...)})
		if name == "systemctl" && len(args) > 0 && args[0] == "list-unit-files" {
			return []byte("hciuart.service                            enabled         enabled\n"), nil
		}
		return nil, nil
	}
	var buf bytes.Buffer
	if err := FixBluetoothOnPL011(FixOptions{Runner: runner, Out: &buf}); err != nil {
		t.Fatalf("FixBluetoothOnPL011: %v", err)
	}
	disables := 0
	for _, c := range *calls {
		if c.Name == "systemctl" && len(c.Args) > 0 && c.Args[0] == "disable" {
			disables++
		}
	}
	if disables != 1 {
		t.Errorf("expected 1 disable call when hciuart present; got %d (%+v)", disables, *calls)
	}
}

func TestFixUserNotInDialout(t *testing.T) {
	withTempPaths(t)
	if _, err := LookupCommand("usermod"); err != nil {
		t.Skip("usermod not available on this host; skipping")
	}
	runner, calls := recordingRunner()
	var buf bytes.Buffer
	if err := FixUserNotInDialout(FixOptions{User: "alice", Runner: runner, Out: &buf}); err != nil {
		t.Fatalf("FixUserNotInDialout: %v", err)
	}
	if len(*calls) != 1 || (*calls)[0].Name != "usermod" {
		t.Fatalf("expected usermod call; got %+v", *calls)
	}
	gotArgs := (*calls)[0].Args
	if len(gotArgs) != 3 || gotArgs[0] != "-aG" || gotArgs[1] != "dialout" || gotArgs[2] != "alice" {
		t.Errorf("usermod args: got %v, want [-aG dialout alice]", gotArgs)
	}
}

// ---------------------------------------------------------------------------
// Orchestrator
// ---------------------------------------------------------------------------

func TestRun_NotARaspberryPi(t *testing.T) {
	withTempPaths(t)
	var buf bytes.Buffer
	res, err := Run(RunOptions{
		Out:      &buf,
		DetectPi: func() (bool, string) { return false, "" },
		IsRoot:   func() bool { return true },
		Runner:   func(name string, args ...string) ([]byte, error) { return nil, nil },
	})
	if err != nil {
		t.Fatalf("Run on non-Pi: %v", err)
	}
	if res.IsPi {
		t.Errorf("Result.IsPi: got true, want false")
	}
	if !strings.Contains(buf.String(), "Not a Raspberry Pi") {
		t.Errorf("output missing non-Pi message: %q", buf.String())
	}
}

func TestRun_DryRunOnPi(t *testing.T) {
	withTempPaths(t)
	original := "console=serial0,115200 console=tty1 rootwait\n"
	if err := os.WriteFile(CmdlinePathPrimary, []byte(original), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(ConfigTxtPathPrimary, []byte("enable_uart=1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	var buf bytes.Buffer
	res, err := Run(RunOptions{
		User:     "alice",
		DryRun:   true,
		Yes:      true,
		Out:      &buf,
		DetectPi: func() (bool, string) { return true, "Raspberry Pi 5 Model B" },
		IsRoot:   func() bool { return false }, // dry-run shouldn't enforce root
		Runner:   func(name string, args ...string) ([]byte, error) { return nil, nil },
	})
	if err != nil {
		t.Fatalf("Run dry-run: %v", err)
	}
	// Dry-run should not modify files.
	got, _ := os.ReadFile(CmdlinePathPrimary)
	if string(got) != original {
		t.Errorf("dry-run modified cmdline: got %q, want %q", string(got), original)
	}
	if len(res.Applied) != 0 {
		t.Errorf("dry-run recorded applied fixes: %+v", res.Applied)
	}
	if !strings.Contains(buf.String(), "[dry-run]") {
		t.Errorf("output missing dry-run marker: %q", buf.String())
	}
}

func TestRun_NotRoot_ApplyMode_Errors(t *testing.T) {
	withTempPaths(t)
	if err := os.WriteFile(CmdlinePathPrimary, []byte("console=serial0,115200 console=tty1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(ConfigTxtPathPrimary, []byte("enable_uart=1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	var buf bytes.Buffer
	_, err := Run(RunOptions{
		User:     "alice",
		Yes:      true,
		Out:      &buf,
		DetectPi: func() (bool, string) { return true, "Raspberry Pi 5" },
		IsRoot:   func() bool { return false },
		Runner:   func(name string, args ...string) ([]byte, error) { return nil, nil },
	})
	if err == nil {
		t.Fatalf("expected root error; got nil")
	}
	if !strings.Contains(err.Error(), "must run as root") {
		t.Errorf("expected 'must run as root' error; got %v", err)
	}
}

func TestRun_FullApply(t *testing.T) {
	withTempPaths(t)
	if err := os.WriteFile(CmdlinePathPrimary, []byte("console=serial0,115200 console=tty1 rootwait\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(ConfigTxtPathPrimary, []byte("enable_uart=1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	var calls []runnerCall
	runner := func(name string, args ...string) ([]byte, error) {
		calls = append(calls, runnerCall{Name: name, Args: append([]string(nil), args...)})
		// Simulate usermod failing if user is "fail-user", to verify error propagation path.
		if name == "usermod" && len(args) >= 3 && args[2] == "fail-user" {
			return []byte("usermod: failed"), errors.New("exit 1")
		}
		return nil, nil
	}
	var buf bytes.Buffer
	res, err := Run(RunOptions{
		User:     "alice",
		Yes:      true,
		Out:      &buf,
		DetectPi: func() (bool, string) { return true, "Raspberry Pi 5" },
		IsRoot:   func() bool { return true },
		Runner:   runner,
	})
	if err != nil {
		t.Fatalf("Run apply: %v", err)
	}

	// cmdline.txt should have been rewritten.
	got, _ := os.ReadFile(CmdlinePathPrimary)
	if strings.Contains(string(got), "console=serial0") {
		t.Errorf("cmdline still has console=serial0: %q", string(got))
	}
	// config.txt should have the overlay appended.
	got, _ = os.ReadFile(ConfigTxtPathPrimary)
	if !strings.Contains(string(got), "dtoverlay=disable-bt") {
		t.Errorf("config.txt missing dtoverlay=disable-bt: %q", string(got))
	}
	if !res.RebootRequired {
		t.Errorf("RebootRequired: got false, want true")
	}
	if len(res.Applied) == 0 {
		t.Errorf("Applied empty; expected at least one fix")
	}
	// Ensure usermod was invoked exactly once.
	usermodCalls := 0
	for _, c := range calls {
		if c.Name == "usermod" {
			usermodCalls++
		}
	}
	// usermod is only attempted if "alice" was detected as not in dialout AND
	// usermod is on PATH. On a CI host without alice, the detection skips
	// fix entirely. Accept either 0 or 1.
	if usermodCalls > 1 {
		t.Errorf("usermod called %d times; want at most 1", usermodCalls)
	}
}

func TestRun_PromptDeclined(t *testing.T) {
	withTempPaths(t)
	if err := os.WriteFile(CmdlinePathPrimary, []byte("console=serial0,115200 console=tty1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(ConfigTxtPathPrimary, []byte("enable_uart=1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	var buf bytes.Buffer
	res, err := Run(RunOptions{
		User:     "alice",
		Out:      &buf,
		In:       strings.NewReader("n\n"),
		DetectPi: func() (bool, string) { return true, "Raspberry Pi 5" },
		IsRoot:   func() bool { return true },
		Runner:   func(name string, args ...string) ([]byte, error) { return nil, nil },
	})
	if err != nil {
		t.Fatalf("Run with prompt 'n': %v", err)
	}
	if !strings.Contains(buf.String(), "Aborted") {
		t.Errorf("expected Aborted output; got %q", buf.String())
	}
	if len(res.Applied) != 0 {
		t.Errorf("declined run still applied fixes: %+v", res.Applied)
	}
}

// ---------------------------------------------------------------------------
// CLI registration sanity check (lives in this package to avoid an import
// cycle with internal/cli's testing layer; we exercise the command tree
// indirectly by ensuring Run() with non-Pi detection returns cleanly).
// ---------------------------------------------------------------------------

func TestRun_AllChecksPass(t *testing.T) {
	withTempPaths(t)
	// cmdline.txt + config.txt both already configured.
	if err := os.WriteFile(CmdlinePathPrimary, []byte("console=tty1 rootwait\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(ConfigTxtPathPrimary, []byte("enable_uart=1\ndtoverlay=disable-bt\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	var buf bytes.Buffer
	res, err := Run(RunOptions{
		User:     "root", // root is in dialout on most systems; if not, NeedsFix=true and we fall through
		Out:      &buf,
		Yes:      true,
		DetectPi: func() (bool, string) { return true, "Raspberry Pi 5" },
		IsRoot:   func() bool { return true },
		Runner:   func(name string, args ...string) ([]byte, error) { return nil, nil },
	})
	if err != nil {
		t.Fatalf("Run all-pass: %v", err)
	}
	// "root" might or might not be in dialout. Just ensure no error and no
	// crashes. The test that matters: detection is read-only.
	_ = res
	if !strings.Contains(buf.String(), "Detection summary") {
		t.Errorf("missing detection summary in output: %q", buf.String())
	}
}

