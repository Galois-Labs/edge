package cli

import (
	"fmt"
	"os"
	"path/filepath"

	"github.com/galois-labs/edge/internal/config"
	"github.com/galois-labs/edge/internal/service"
	"github.com/spf13/cobra"
)

// --------------------------------------------------------------------------
// install subcommand
// --------------------------------------------------------------------------

// installCmd implements "galois-edge install".
var installCmd = &cobra.Command{
	Use:   "install",
	Short: "Install galois-edge as a system service",
	Long: `Install creates a systemd unit (Linux) or registers a Windows Service
(Windows) so that galois-edge starts automatically on boot.

The command must be run with elevated privileges (root / Administrator).`,
	Run: runInstall,
}

func init() {
	installCmd.Flags().String("config", "", "path to config.env (default: system config dir)")
	installCmd.Flags().String("user", "galois-edge", "service user (Linux only)")
}

func runInstall(cmd *cobra.Command, args []string) {
	cfgPath, _ := cmd.Flags().GetString("config")
	if cfgPath == "" {
		cfgPath = filepath.Join(config.SystemConfigDir(), "config.env")
	}

	user, _ := cmd.Flags().GetString("user")

	exePath, err := os.Executable()
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: cannot determine executable path: %v\n", err)
		os.Exit(1)
	}
	// Resolve symlinks to get the real binary path.
	exePath, err = filepath.EvalSymlinks(exePath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: cannot resolve executable path: %v\n", err)
		os.Exit(1)
	}

	fmt.Printf("Installing %s service...\n", service.ServiceName)
	fmt.Printf("  binary:  %s\n", exePath)
	fmt.Printf("  config:  %s\n", cfgPath)
	fmt.Printf("  user:    %s\n", user)

	if err := service.InstallService(exePath, cfgPath, user); err != nil {
		fmt.Fprintf(os.Stderr, "error: install failed: %v\n", err)
		os.Exit(1)
	}

	fmt.Printf("Service %s installed successfully.\n", service.ServiceName)
	fmt.Println("Start the service with: galois-edge start (or systemctl start galois-edge)")
}

// --------------------------------------------------------------------------
// uninstall subcommand
// --------------------------------------------------------------------------

// uninstallCmd implements "galois-edge uninstall".
var uninstallCmd = &cobra.Command{
	Use:   "uninstall",
	Short: "Remove the galois-edge system service",
	Long: `Uninstall stops the service (if running), removes the systemd unit
(Linux) or deregisters the Windows Service, and cleans up.

The command must be run with elevated privileges (root / Administrator).`,
	Run: runUninstall,
}

func runUninstall(cmd *cobra.Command, args []string) {
	fmt.Printf("Uninstalling %s service...\n", service.ServiceName)

	if err := service.UninstallService(); err != nil {
		fmt.Fprintf(os.Stderr, "error: uninstall failed: %v\n", err)
		os.Exit(1)
	}

	fmt.Printf("Service %s uninstalled successfully.\n", service.ServiceName)
}
