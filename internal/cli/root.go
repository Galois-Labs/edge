// Package cli implements the Cobra command tree for the galois-edge daemon.
// Each subcommand lives in its own file; this file defines the root command,
// version information, and the top-level Execute entry point.
package cli

import (
	"fmt"
	"os"

	"github.com/spf13/cobra"
)

// Version information — injected at build time via ldflags:
//
//	go build -ldflags "-X github.com/galois-labs/edge/internal/cli.Version=1.0.0
//	  -X github.com/galois-labs/edge/internal/cli.GitCommit=abc1234
//	  -X github.com/galois-labs/edge/internal/cli.BuildDate=2026-03-08"
var (
	Version   = "dev"
	GitCommit = "unknown"
	BuildDate = "unknown"
)

// rootCmd is the top-level Cobra command for galois-edge.
var rootCmd = &cobra.Command{
	Use:   "galois-edge",
	Short: "galois-edge — laboratory instrument gateway daemon",
	Long: `galois-edge is the edge daemon that bridges physical test equipment
to the Galois cloud control plane. It manages instrument discovery,
gRPC/WebSocket proxying, Tailscale networking, and backend registration.`,
}

func init() {
	rootCmd.AddCommand(versionCmd)
	rootCmd.AddCommand(startCmd)
	rootCmd.AddCommand(statusCmd)
	rootCmd.AddCommand(installCmd)
	rootCmd.AddCommand(uninstallCmd)
	rootCmd.AddCommand(configureCmd)
	rootCmd.AddCommand(setupCmd)
	rootCmd.AddCommand(doctorCmd)
	rootCmd.AddCommand(piSetupCmd)
	rootCmd.AddCommand(claudeCmd)
}

// Execute runs the root command. This is the sole entry point called from main.
func Execute() {
	if err := rootCmd.Execute(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

// --------------------------------------------------------------------------
// version subcommand
// --------------------------------------------------------------------------

var versionCmd = &cobra.Command{
	Use:   "version",
	Short: "Print version information",
	Run: func(cmd *cobra.Command, args []string) {
		fmt.Printf("galois-edge %s\n", Version)
		fmt.Printf("  commit:  %s\n", GitCommit)
		fmt.Printf("  built:   %s\n", BuildDate)
	},
}
