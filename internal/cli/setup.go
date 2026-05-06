package cli

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"github.com/galois-labs/edge/internal/config"
	"github.com/galois-labs/edge/internal/installid"
	"github.com/spf13/cobra"
)

// DefaultBackendURL is the production cloud backend URL used when neither
// the --backend flag nor the BACKEND_URL environment variable is set.
const DefaultBackendURL = "https://cloud.galoislabs.ai"

// setupCmd implements "galois-edge setup <TOKEN>".
var setupCmd = &cobra.Command{
	Use:   "setup <TOKEN>",
	Short: "Register this edge with the cloud backend and write config",
	Long: `Setup takes a single API key token (obtained from the Galois dashboard),
registers the edge daemon with the cloud backend, and persists all
configuration needed for "galois-edge start" to work.

The token is typically prefixed with "glc_". After a successful setup,
start the daemon with:

  galois-edge start`,
	Args: cobra.ExactArgs(1),
	Run:  runSetup,
}

func init() {
	setupCmd.Flags().String("backend", "", "backend URL (default: https://cloud.galoislabs.ai, env: BACKEND_URL)")
	setupCmd.Flags().String("name", "", "edge name (default: system hostname)")
	setupCmd.Flags().String("config", "", "config file path (default: auto-detect or ~/.config/galois-edge/config.env)")
}

// setupPayload mirrors registerPayload from the registration package but
// is defined locally to avoid importing the full registration Manager.
type setupPayload struct {
	Name     string `json:"name"`
	Hostname string `json:"hostname"`
	GRPCPort int    `json:"grpc_port"`
	WSPort   int    `json:"ws_port"`
	Version  string `json:"version,omitempty"`
	OSInfo   string `json:"os_info,omitempty"`
}

// setupResponse captures the fields returned by POST /api/v1/edges/register.
type setupResponse struct {
	ID           string `json:"id"`
	PreAuthKey   string `json:"pre_auth_key,omitempty"`
	HeadscaleURL string `json:"headscale_url,omitempty"`
}

func runSetup(cmd *cobra.Command, args []string) {
	token := args[0]

	// Soft validation: warn if token doesn't match the expected prefix.
	if !strings.HasPrefix(token, "glc_") {
		fmt.Fprintf(os.Stderr, "warning: token does not start with \"glc_\" — this may not be a valid Galois API key\n")
	}

	// Resolve backend URL: --backend flag -> BACKEND_URL env -> default.
	backendURL, _ := cmd.Flags().GetString("backend")
	if backendURL == "" {
		backendURL = os.Getenv("BACKEND_URL")
	}
	if backendURL == "" {
		backendURL = DefaultBackendURL
	}
	backendURL = strings.TrimRight(backendURL, "/")

	// Resolve edge name: --name flag -> os.Hostname().
	edgeName, _ := cmd.Flags().GetString("name")
	hostname, _ := os.Hostname()
	if edgeName == "" {
		edgeName = hostname
	}

	// Build registration payload.
	payload := setupPayload{
		Name:     edgeName,
		Hostname: hostname,
		GRPCPort: 50051,
		WSPort:   8765,
		Version:  Version,
		OSInfo:   runtime.GOOS + "/" + runtime.GOARCH,
	}

	body, err := json.Marshal(payload)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: failed to build registration payload: %v\n", err)
		os.Exit(1)
	}

	// POST to backend.
	url := backendURL + "/api/v1/edges/register"

	client := &http.Client{Timeout: 30 * time.Second}
	req, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: failed to create request: %v\n", err)
		os.Exit(1)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-API-Key", token)

	resp, err := client.Do(req)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: cannot reach backend at %s: %v\n", backendURL, err)
		os.Exit(1)
	}
	defer resp.Body.Close()

	respBody, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))

	if resp.StatusCode == http.StatusUnauthorized {
		fmt.Fprintf(os.Stderr, "error: authentication failed (401) — check that your token is valid\n")
		os.Exit(1)
	}
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusCreated {
		fmt.Fprintf(os.Stderr, "error: registration failed (HTTP %d): %s\n", resp.StatusCode, string(respBody))
		os.Exit(1)
	}

	// Parse response.
	var regResp setupResponse
	if err := json.Unmarshal(respBody, &regResp); err != nil {
		fmt.Fprintf(os.Stderr, "error: cannot parse registration response: %v\n", err)
		os.Exit(1)
	}

	// Resolve config path: --config flag -> existing file -> user config dir.
	cfgPath, _ := cmd.Flags().GetString("config")
	if cfgPath == "" {
		existing := config.FindConfigFile()
		if existing != "" {
			cfgPath = existing
		} else {
			cfgPath = filepath.Join(config.UserConfigDir(), "config.env")
		}
	}

	// Read-modify-write: preserve existing config values, overlay new ones.
	kvs := make(map[string]string)
	if _, err := os.Stat(cfgPath); err == nil {
		existing, err := config.ParseFile(cfgPath)
		if err != nil {
			fmt.Fprintf(os.Stderr, "error: cannot read existing config %s: %v\n", cfgPath, err)
			os.Exit(1)
		}
		kvs = existing
	}

	kvs["BACKEND_URL"] = backendURL
	kvs["REGISTRATION_TOKEN"] = token
	kvs["EDGE_NAME"] = edgeName

	if regResp.PreAuthKey != "" {
		kvs["TAILSCALE_AUTH_KEY"] = regResp.PreAuthKey
	}
	if regResp.HeadscaleURL != "" {
		kvs["HEADSCALE_URL"] = regResp.HeadscaleURL
	}

	if err := config.WriteFileMap(cfgPath, kvs); err != nil {
		fmt.Fprintf(os.Stderr, "error: cannot write config %s: %v\n", cfgPath, err)
		os.Exit(1)
	}

	// Ensure a per-machine install ID exists. This is used to derive
	// stable subject keys for Claude Code ingestion (and any future
	// per-edge identity needs that should outlive hostname changes).
	// Failure is non-fatal: the ID will be lazily created the next
	// time anything that needs it runs (Claude hook, claude enable),
	// possibly as a per-user fallback.
	if id, err := installid.Ensure(); err != nil {
		fmt.Fprintf(os.Stderr, "warning: could not persist install id: %v\n", err)
	} else {
		_ = id // available; not echoed to user (it's machine-internal)
	}

	// Success output.
	fmt.Printf("Registered as %q (%s)\n", edgeName, regResp.ID)
	fmt.Printf("Config written to %s\n", cfgPath)
	fmt.Println()
	fmt.Println("Start the daemon:")
	fmt.Println("  galois-edge start")
}
