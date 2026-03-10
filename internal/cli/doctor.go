package cli

import (
	"fmt"
	"os"

	"github.com/galois-labs/edge/internal/doctor"
	"github.com/spf13/cobra"
)

// doctorCmd implements "galois-edge doctor".
var doctorCmd = &cobra.Command{
	Use:   "doctor",
	Short: "Run diagnostic checks on the system",
	Long: `Doctor runs a series of health checks to verify the system is properly
configured for running the galois-edge daemon. It checks disk space, Python
binary, gRPC connectivity, GPIB drivers, USB permissions, and network access.`,
	Run: runDoctor,
}

func init() {
	doctorCmd.Flags().String("config", "", "path to config.env file")
	doctorCmd.Flags().Bool("json", false, "output results as JSON")
}

func runDoctor(cmd *cobra.Command, args []string) {
	cfgPath, _ := cmd.Flags().GetString("config")
	jsonOutput, _ := cmd.Flags().GetBool("json")

	cfg, _, err := loadConfig(cfgPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "warning: could not load config: %v (using defaults)\n", err)
		cfg = nil // RunChecks handles nil cfg
	}

	results := doctor.RunChecks(cfg)

	if jsonOutput {
		out, err := doctor.FormatJSON(results)
		if err != nil {
			fmt.Fprintf(os.Stderr, "error: %v\n", err)
			os.Exit(1)
		}
		fmt.Println(out)
	} else {
		fmt.Println("galois-edge doctor")
		fmt.Println("==================")
		fmt.Print(doctor.FormatText(results))
		fmt.Println()

		if doctor.HasFailures(results) {
			fmt.Println("Some checks FAILED. Please resolve the issues above.")
			os.Exit(1)
		} else {
			fmt.Println("All checks passed (warnings are informational).")
		}
	}
}
