package cli

import (
	"fmt"
	"os"

	"github.com/galois-labs/edge/internal/pisetup"
	"github.com/spf13/cobra"
)

// piSetupCmd implements "galois-edge pi-setup". The subcommand applies the
// three Raspberry Pi UART fixes that the daemon's pi_diagnostics.py only
// warns about. See internal/pisetup for detection + fix logic.
var piSetupCmd = &cobra.Command{
	Use:   "pi-setup",
	Short: "Configure Raspberry Pi UART for serial-instrument access",
	Long: `pi-setup detects and remediates the three OS-level gotchas that prevent
the galois-edge daemon from using the Pi's GPIO UART (/dev/serial0,
/dev/ttyAMA0):

  1. A login getty/console attached to the UART pollutes any read.
  2. Bluetooth claims the high-quality PL011 (Pi 3+/4/5/Zero 2 W) so
     /dev/serial0 falls back to the inferior mini-UART.
  3. The daemon user is not in the dialout group and cannot open the
     UART without root.

Detection is non-destructive. Use --dry-run to preview the changes that
would be made. To actually apply, re-run as root (or under sudo) with
--yes to skip the confirmation prompt.`,
	Run: runPiSetup,
}

func init() {
	piSetupCmd.Flags().Bool("dry-run", false, "print what would be done; do not modify the system")
	piSetupCmd.Flags().Bool("yes", false, "apply fixes without prompting")
	piSetupCmd.Flags().String("user", "", "user to add to the dialout group (default: SUDO_USER, then current user)")
	piSetupCmd.Flags().Bool("reboot", false, "run `systemctl reboot` after applying fixes")
}

func runPiSetup(cmd *cobra.Command, args []string) {
	dryRun, _ := cmd.Flags().GetBool("dry-run")
	yes, _ := cmd.Flags().GetBool("yes")
	user, _ := cmd.Flags().GetString("user")
	reboot, _ := cmd.Flags().GetBool("reboot")

	res, err := pisetup.Run(pisetup.RunOptions{
		User:   user,
		DryRun: dryRun,
		Yes:    yes,
		Reboot: reboot,
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}
	if res.HasFailures() {
		os.Exit(2)
	}
}
