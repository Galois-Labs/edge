package doctor

import (
	"encoding/json"
	"os"
	"os/exec"
)

// newMockCommand returns a replacement for execCommand that produces a real
// *exec.Cmd backed by "go run" of an inline helper. For test simplicity we
// use a simpler approach: override execCommand to call a Go binary that prints
// the desired output and exits with the desired code.
//
// We encode the desired stdout as a JSON env var and use "sh -c 'printf ...'",
// which is available on macOS and Linux (our dev/CI platforms).
// On Windows the test would be skipped or use a different helper.
func newMockCommand(stdout []byte, cmdErr error) func(string, ...string) *exec.Cmd {
	if cmdErr != nil {
		// Return a command that exits non-zero.
		return func(name string, args ...string) *exec.Cmd {
			// "false" exits 1 on unix.
			return exec.Command("sh", "-c", "exit 1")
		}
	}

	// Encode stdout as base64 so we can embed it safely in a shell snippet.
	// Use python3 or printf — both available on macOS / Linux CI.
	// Simplest approach: write the bytes to a temp file and cat it.
	tmp, err := os.CreateTemp("", "mock-tailscale-*.json")
	if err != nil {
		panic("newMockCommand: cannot create temp file: " + err.Error())
	}
	tmp.Write(stdout)
	tmp.Close()

	return func(name string, args ...string) *exec.Cmd {
		return exec.Command("cat", tmp.Name())
	}
}

// buildTailscaleStatusJSON is a helper that produces the JSON bytes for a
// tailscale status response with the given IPs.
func buildTailscaleStatusJSON(ips []string) []byte {
	status := tailscaleStatusJSON{}
	status.Self.TailscaleIPs = ips
	b, _ := json.Marshal(status)
	return b
}
