package cli

import (
	"testing"
)

// TestPiSetupRegistered confirms that the root command knows about pi-setup.
// This guards against accidental regressions in init() ordering or stripped
// imports.
func TestPiSetupRegistered(t *testing.T) {
	cmd, _, err := rootCmd.Find([]string{"pi-setup"})
	if err != nil {
		t.Fatalf("rootCmd.Find(pi-setup): %v", err)
	}
	if cmd == nil {
		t.Fatalf("pi-setup command is nil after Find")
	}
	if cmd.Name() != "pi-setup" {
		t.Errorf("resolved command name: got %q, want %q", cmd.Name(), "pi-setup")
	}

	// Confirm the documented flags exist.
	for _, f := range []string{"dry-run", "yes", "user", "reboot"} {
		if cmd.Flags().Lookup(f) == nil {
			t.Errorf("flag %q not registered on pi-setup", f)
		}
	}
}
