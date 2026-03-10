package cli

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"text/tabwriter"

	"github.com/galois-labs/edge/internal/config"
	"github.com/spf13/cobra"
)

// configureCmd is the top-level "configure" command with get/set/list
// subcommands.
var configureCmd = &cobra.Command{
	Use:   "configure",
	Short: "Get, set, or list configuration values",
	Long: `Configure provides commands for inspecting and modifying the daemon
configuration file. Values are stored in KEY=VALUE format.

Use "configure list" to see all keys and their current values.
Use "configure get <KEY>" to read a single value.
Use "configure set <KEY> <VALUE>" to update a value.`,
}

func init() {
	configureCmd.PersistentFlags().String("config", "", "path to config.env file")
	configureCmd.AddCommand(configureGetCmd)
	configureCmd.AddCommand(configureSetCmd)
	configureCmd.AddCommand(configureListCmd)
	configureCmd.AddCommand(configureInitCmd)
}

// --------------------------------------------------------------------------
// configure get <KEY>
// --------------------------------------------------------------------------

var configureGetCmd = &cobra.Command{
	Use:   "get <KEY>",
	Short: "Print the value of a configuration key",
	Args:  cobra.ExactArgs(1),
	Run:   runConfigureGet,
}

func runConfigureGet(cmd *cobra.Command, args []string) {
	key := strings.ToUpper(args[0])

	cfg, _, err := loadConfigForConfigure(cmd)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}

	val, ok := config.GetValue(cfg, key)
	if !ok {
		fmt.Fprintf(os.Stderr, "error: unknown config key %q\n", key)
		fmt.Fprintf(os.Stderr, "Run 'galois-edge configure list' to see all valid keys.\n")
		os.Exit(1)
	}

	fmt.Println(val)
}

// --------------------------------------------------------------------------
// configure set <KEY> <VALUE>
// --------------------------------------------------------------------------

var configureSetCmd = &cobra.Command{
	Use:   "set <KEY> <VALUE>",
	Short: "Set a configuration value and write it to the config file",
	Args:  cobra.ExactArgs(2),
	Run:   runConfigureSet,
}

func runConfigureSet(cmd *cobra.Command, args []string) {
	key := strings.ToUpper(args[0])
	value := args[1]

	cfgPath := resolveConfigPath(cmd)

	// Validate the key is known.
	allKeys := config.EnvKeys()
	known := false
	for _, k := range allKeys {
		if k == key {
			known = true
			break
		}
	}
	if !known {
		fmt.Fprintf(os.Stderr, "error: unknown config key %q\n", key)
		fmt.Fprintf(os.Stderr, "Run 'galois-edge configure list' to see all valid keys.\n")
		os.Exit(1)
	}

	// Validate the value by applying it to a config struct.
	testCfg := config.New()
	if err := config.SetValue(testCfg, key, value); err != nil {
		fmt.Fprintf(os.Stderr, "error: invalid value for %s: %v\n", key, err)
		os.Exit(1)
	}

	// Read-modify-write the config file.
	kvs := make(map[string]string)
	if _, err := os.Stat(cfgPath); err == nil {
		existing, err := config.ParseFile(cfgPath)
		if err != nil {
			fmt.Fprintf(os.Stderr, "error: cannot read existing config %s: %v\n", cfgPath, err)
			os.Exit(1)
		}
		kvs = existing
	}

	kvs[key] = value

	if err := config.WriteFileMap(cfgPath, kvs); err != nil {
		fmt.Fprintf(os.Stderr, "error: cannot write config %s: %v\n", cfgPath, err)
		os.Exit(1)
	}

	fmt.Printf("%s=%s\n", key, value)
	fmt.Printf("Written to %s\n", cfgPath)
}

// --------------------------------------------------------------------------
// configure list
// --------------------------------------------------------------------------

var configureListCmd = &cobra.Command{
	Use:   "list",
	Short: "List all configuration keys and their current values",
	Run:   runConfigureList,
}

func runConfigureList(cmd *cobra.Command, args []string) {
	cfg, resolvedPath, err := loadConfigForConfigure(cmd)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}

	fmt.Printf("Configuration from: %s\n\n", resolvedPath)

	w := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintf(w, "KEY\tVALUE\n")
	fmt.Fprintf(w, "---\t-----\n")

	keys := config.EnvKeys()
	for _, key := range keys {
		val, _ := config.GetValue(cfg, key)
		// Mask sensitive values.
		if isSensitiveKey(key) && val != "" {
			val = maskValue(val)
		}
		fmt.Fprintf(w, "%s\t%s\n", key, val)
	}
	w.Flush()
}

// --------------------------------------------------------------------------
// configure init
// --------------------------------------------------------------------------

var configureInitCmd = &cobra.Command{
	Use:   "init",
	Short: "Create a default configuration file",
	Long: `Init creates a configuration file with default values at the
system or user config directory. This is useful for initial setup.`,
	Run: runConfigureInit,
}

func runConfigureInit(cmd *cobra.Command, args []string) {
	cfgPath := resolveConfigPath(cmd)

	if _, err := os.Stat(cfgPath); err == nil {
		fmt.Fprintf(os.Stderr, "error: config file already exists at %s\n", cfgPath)
		fmt.Fprintf(os.Stderr, "Delete it first or use 'configure set' to modify values.\n")
		os.Exit(1)
	}

	cfg := config.New()
	if err := cfg.Save(cfgPath); err != nil {
		fmt.Fprintf(os.Stderr, "error: cannot write config: %v\n", err)
		os.Exit(1)
	}

	fmt.Printf("Default config written to %s\n", cfgPath)
	fmt.Println("Edit this file or use 'galois-edge configure set <KEY> <VALUE>' to customize.")
}

// --------------------------------------------------------------------------
// Helpers
// --------------------------------------------------------------------------

// loadConfigForConfigure loads config using the --config flag or auto-discovery.
func loadConfigForConfigure(cmd *cobra.Command) (*config.Config, string, error) {
	cfgPath, _ := cmd.Flags().GetString("config")
	return loadConfig(cfgPath)
}

// resolveConfigPath determines the config file path for write operations.
// Priority: --config flag -> existing file -> user config dir.
func resolveConfigPath(cmd *cobra.Command) string {
	cfgPath, _ := cmd.Flags().GetString("config")
	if cfgPath != "" {
		return cfgPath
	}

	// Try to find an existing file.
	existing := config.FindConfigFile()
	if existing != "" {
		return existing
	}

	// Fall back to user config directory for new files (doesn't require root).
	return filepath.Join(config.UserConfigDir(), "config.env")
}

// sensitiveKeys is the set of config keys whose values should be masked
// in list output.
var sensitiveKeys = func() map[string]bool {
	keys := map[string]bool{
		"REGISTRATION_TOKEN":  true,
		"TAILSCALE_AUTH_KEY":  true,
	}
	return keys
}()

// isSensitiveKey reports whether the given config key contains sensitive
// data that should be masked in display.
func isSensitiveKey(key string) bool {
	return sensitiveKeys[key]
}

// maskValue replaces most of a string with asterisks, showing only the
// last 4 characters (or fewer if the string is short).
func maskValue(s string) string {
	if len(s) <= 4 {
		return strings.Repeat("*", len(s))
	}
	return strings.Repeat("*", len(s)-4) + s[len(s)-4:]
}

// sortedKeys returns the keys of a map in sorted order.
func sortedKeys(m map[string]string) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}
