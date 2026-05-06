package cli

import (
	"context"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"time"

	"github.com/galois-labs/edge/internal/claudeingest"
	"github.com/galois-labs/edge/internal/config"
	"github.com/galois-labs/edge/internal/grpcclient"
	"github.com/galois-labs/edge/internal/network"
	"github.com/galois-labs/edge/internal/proxy"
	"github.com/galois-labs/edge/internal/registration"
	"github.com/galois-labs/edge/internal/relay"
	"github.com/galois-labs/edge/internal/supervisor"
	"github.com/spf13/cobra"
)

// startCmd implements "galois-edge start".
var startCmd = &cobra.Command{
	Use:   "start",
	Short: "Start the galois-edge daemon",
	Long: `Start loads configuration, spawns the Python instrument engine,
establishes Tailscale networking (if configured), creates TCP proxies for
gRPC and WebSocket traffic, and registers with the cloud backend.`,
	Run: runStart,
}

func init() {
	startCmd.Flags().String("config", "", "path to config.env file (auto-discovered if omitted)")
	startCmd.Flags().String("log-level", "", "log level override: debug, info, warn, error")
}

func runStart(cmd *cobra.Command, args []string) {
	// ----- resolve and load config -----
	cfgPath, _ := cmd.Flags().GetString("config")
	cfg, resolvedPath, err := loadConfig(cfgPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}

	// ----- apply CLI flag overrides -----
	if ll, _ := cmd.Flags().GetString("log-level"); ll != "" {
		cfg.LogLevel = ll
	}

	// ----- validate config -----
	if err := cfg.Validate(); err != nil {
		fmt.Fprintf(os.Stderr, "error: config validation failed: %v\n", err)
		os.Exit(1)
	}

	// ----- structured logging -----
	level := parseLogLevel(cfg.LogLevel)
	logger := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{
		Level: level,
	}))
	slog.SetDefault(logger)

	// ----- startup banner -----
	fmt.Println("=========================================")
	fmt.Printf(" galois-edge %s\n", Version)
	fmt.Printf(" commit:  %s\n", GitCommit)
	fmt.Printf(" config:  %s\n", resolvedPath)
	fmt.Printf(" log:     %s\n", cfg.LogLevel)
	fmt.Println("=========================================")

	slog.Info("daemon starting",
		"version", Version,
		"config", resolvedPath,
		"log_level", cfg.LogLevel,
		"edge_name", cfg.EdgeName,
	)

	// ----- top-level context tied to OS signals -----
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// ----- locate Python binary -----
	pythonBin := resolvePythonBinary(cfg.PythonBin)
	if pythonBin == "" {
		slog.Error("cannot find Python engine binary; set PYTHON_BIN in config")
		os.Exit(1)
	}
	slog.Info("python binary resolved", "path", pythonBin)

	// ----- build Python environment -----
	pythonEnv := buildPythonEnv(cfg)

	// ----- start supervisor (spawn Python, wait for healthy) -----
	healthAddr := fmt.Sprintf("127.0.0.1:%d", cfg.GRPCInternalPort)
	sup := supervisor.New(supervisor.Config{
		BinaryPath: pythonBin,
		Args:       []string{"start"},
		Env:        pythonEnv,
		HealthAddr: healthAddr,
	}, logger)

	slog.Info("starting Python engine...",
		"grpc_internal", cfg.GRPCInternalPort,
		"ws_internal", cfg.WSInternalPort,
	)

	if err := sup.Start(ctx); err != nil {
		slog.Error("failed to start Python engine", "error", err)
		os.Exit(1)
	}
	slog.Info("Python engine is healthy")

	// ----- initial registration (before tsnet) -----
	var regMgr *registration.Manager
	if cfg.BackendURL != "" {
		gc, err := grpcclient.New(healthAddr)
		if err != nil {
			slog.Warn("failed to create gRPC client for registration", "error", err)
		} else {
			getter := &instrumentGetterAdapter{client: gc}

			// IPFunc starts as empty — tsnet isn't up yet. It will be
			// replaced after tsnet starts so heartbeats include the tailnet IP.
			hostname, _ := os.Hostname()
			regMgr = registration.NewManager(registration.Config{
				BackendURL:        cfg.BackendURL,
				EdgeName:          cfg.EdgeName,
				Hostname:          hostname,
				Token:             cfg.RegistrationToken,
				GRPCPort:          cfg.GRPCPort,
				WSPort:            cfg.WSPort,
				Version:           Version,
				OSInfo:            fmt.Sprintf("%s/%s", runtime.GOOS, runtime.GOARCH),
				HeartbeatInterval: time.Duration(cfg.HeartbeatIntervalSec) * time.Second,
				InitialBackoff:    time.Duration(cfg.ConnectionInitialBackoff * float64(time.Second)),
				MaxBackoff:        time.Duration(cfg.ConnectionMaxBackoff * float64(time.Second)),
				FailureThreshold:  cfg.ConnectionFailureThreshold,
				IPFunc:            func() string { return "" }, // updated below after tsnet
			}, getter)

			result, err := regMgr.RegisterOnce(ctx)
			if err != nil {
				slog.Warn("initial registration failed, will retry in heartbeat loop", "error", err)
			} else {
				slog.Info("registered with backend", "edge_id", result.EdgeID)
				// If the backend returned tailnet credentials and config
				// doesn't already have them, update in-memory config and
				// persist to disk so subsequent restarts use the saved key.
				if result.PreAuthKey != "" && cfg.TailscaleAuthKey == "" {
					cfg.TailscaleAuthKey = result.PreAuthKey
					if result.HeadscaleURL != "" && cfg.HeadscaleURL == "" {
						cfg.HeadscaleURL = result.HeadscaleURL
					}
					persistTailnetCredentials(cfg, resolvedPath)
				}
			}
		}
	} else {
		slog.Info("no backend configured, running in standalone mode")
	}

	// ----- start tsnet (optional) -----
	var tsnetSrv *network.Server
	if cfg.TailscaleAuthKey != "" || cfg.HeadscaleURL != "" {
		stateDir := cfg.TsnetStateDir
		if stateDir == "" {
			stateDir = filepath.Join(config.SystemConfigDir(), "tsnet-state")
		}

		tsnetSrv = network.NewServer(network.Config{
			Hostname:   cfg.EdgeName,
			AuthKey:    cfg.TailscaleAuthKey,
			ControlURL: cfg.HeadscaleURL,
			StateDir:   stateDir,
		})

		if err := tsnetSrv.Start(ctx); err != nil {
			slog.Warn("tsnet failed to start, falling back to direct listeners only", "error", err)
			tsnetSrv = nil
		} else {
			slog.Info("tsnet connected", "ipv4", tsnetSrv.IPv4())
		}
	} else {
		slog.Info("tsnet not configured, using direct listeners only")
	}

	// ----- create listeners and start proxies -----
	var proxies []*proxy.TCPProxy

	// gRPC proxy: external port -> Python internal port.
	grpcTarget := fmt.Sprintf("127.0.0.1:%d", cfg.GRPCInternalPort)
	grpcProxies := createProxies("grpc", cfg.GRPCPort, grpcTarget, tsnetSrv)
	proxies = append(proxies, grpcProxies...)

	// WebSocket proxy: external port -> Python internal port.
	wsTarget := fmt.Sprintf("127.0.0.1:%d", cfg.WSInternalPort)
	wsProxies := createProxies("ws", cfg.WSPort, wsTarget, tsnetSrv)
	proxies = append(proxies, wsProxies...)

	for _, p := range proxies {
		p := p
		go func() {
			if err := p.Serve(ctx); err != nil {
				slog.Warn("proxy stopped with error", "error", err)
			}
		}()
	}

	// ----- start heartbeat loop (if registration manager was created) -----
	if regMgr != nil {
		// Now that tsnet is up, update the IPFunc so heartbeats include
		// the real tailnet IP.
		if tsnetSrv != nil {
			regMgr.SetIPFunc(tsnetSrv.IPv4)
		}
		regMgr.Start(ctx)
		slog.Info("registration heartbeat loop started", "backend", cfg.BackendURL)
	}

	// ----- start Claude Code ingestion control endpoint (if configured) -----
	var claudeControlStarted bool
	if cfg.BackendURL != "" && cfg.RegistrationToken != "" {
		cloudHTTPClient := http.DefaultClient
		if tsnetSrv != nil {
			if c, err := tsnetSrv.HTTPClient(); err == nil {
				cloudHTTPClient = c
			} else {
				slog.Warn("failed to create tsnet HTTP client for Claude ingestion", "error", err)
			}
		}
		claudeControl := claudeingest.NewControlServer(claudeingest.ControlConfig{
			BackendURL: cfg.BackendURL,
			AuthToken:  cfg.RegistrationToken,
			HTTPClient: cloudHTTPClient,
			Logger:     logger,
		})
		go func() {
			if err := claudeControl.Run(ctx); err != nil && ctx.Err() == nil {
				slog.Warn("claude ingest control endpoint stopped", "error", err)
			}
		}()
		claudeControlStarted = true
	} else {
		slog.Debug("claude ingest control endpoint disabled: backend URL or registration token missing")
	}

	// ----- start relay client (if configured) -----
	relayURL := resolveRelayURL(cfg)
	edgeID := ""
	if regMgr != nil {
		edgeID = regMgr.EdgeID()
	}
	if relayURL != "" && edgeID != "" && cfg.RegistrationToken != "" {
		relayClient := relay.NewClient(
			edgeID,
			cfg.EdgeName,
			Version,
			relayURL,
			cfg.RegistrationToken,
			healthAddr,
			logger,
		)
		go relayClient.Run(ctx)
		slog.Info("relay client started", "url", relayURL, "edge_id", edgeID)
	} else if relayURL != "" && edgeID == "" {
		slog.Warn("relay URL configured but no edge ID available (registration may have failed), skipping relay")
	}

	// ----- ready -----
	slog.Info("daemon ready", "claude_ingest_control", claudeControlStarted)
	<-ctx.Done()
	slog.Info("shutting down...")
	fmt.Println("Shutting down...")

	// ----- graceful shutdown (reverse startup order) -----

	// 1. Stop registration (best-effort unregister).
	if regMgr != nil {
		slog.Debug("stopping registration")
		regMgr.Stop()
	}

	// 2. Drain proxies.
	for _, p := range proxies {
		slog.Debug("stopping proxy")
		p.Stop()
	}

	// 3. Stop tsnet.
	if tsnetSrv != nil {
		slog.Debug("stopping tsnet")
		if err := tsnetSrv.Stop(); err != nil {
			slog.Warn("tsnet stop error", "error", err)
		}
	}

	// 4. Stop supervisor (stdin close -> wait -> kill).
	slog.Debug("stopping Python engine")
	if err := sup.Stop(); err != nil {
		slog.Warn("supervisor stop error", "error", err)
	}

	slog.Info("daemon stopped")
}

// --------------------------------------------------------------------------
// Helpers
// --------------------------------------------------------------------------

// loadConfig resolves the config path and loads the configuration using
// the standard search order: CLI flag -> auto-discovery -> env-only.
func loadConfig(cliPath string) (*config.Config, string, error) {
	cfg, err := config.Load(cliPath)
	if err != nil {
		return nil, "", err
	}

	// Determine the resolved path for display purposes.
	resolvedPath := cliPath
	if resolvedPath == "" {
		resolvedPath = config.FindConfigFile()
	}
	if resolvedPath == "" {
		resolvedPath = "(defaults + env)"
	}

	return cfg, resolvedPath, nil
}

// resolvePythonBinary finds the frozen Python engine binary.
// Search order:
//  1. Explicit path from config (PYTHON_BIN).
//  2. Known binary names next to the Go binary.
//  3. Known binary names in PATH.
func resolvePythonBinary(configPath string) string {
	if configPath != "" {
		if _, err := os.Stat(configPath); err == nil {
			return configPath
		}
	}

	// Candidate names — try platform-specific (.exe) first on Windows.
	names := []string{
		"galois-edge-daemon",
		"galois-engine",
	}
	if runtime.GOOS == "windows" {
		names = []string{
			"galois-edge-daemon.exe",
			"galois-engine.exe",
			"galois-edge-daemon",
			"galois-engine",
		}
	}

	// Look next to the Go binary.
	if exe, err := os.Executable(); err == nil {
		dir := filepath.Dir(exe)
		for _, name := range names {
			candidate := filepath.Join(dir, name)
			if _, err := os.Stat(candidate); err == nil {
				return candidate
			}
		}
	}

	// Look in PATH.
	for _, name := range names {
		if p, err := exec.LookPath(name); err == nil {
			return p
		}
	}

	return ""
}

// buildPythonEnv constructs environment variables to pass to the Python
// child process. The child inherits the parent's environment plus
// daemon-specific overrides.
func buildPythonEnv(cfg *config.Config) []string {
	env := os.Environ()
	env = append(env,
		fmt.Sprintf("GRPC_PORT=%d", cfg.GRPCInternalPort),
		fmt.Sprintf("WS_PORT=%d", cfg.WSInternalPort),
		fmt.Sprintf("EDGE_NAME=%s", cfg.EdgeName),
		fmt.Sprintf("GRPC_MAX_WORKERS=%d", cfg.GRPCMaxWorkers),
		fmt.Sprintf("PROFILES_ENABLED=%t", cfg.ProfilesEnabled),
		fmt.Sprintf("GPIB_ENABLED=%s", cfg.GPIBEnabled),
		fmt.Sprintf("LAN_ENABLED=%t", cfg.LANEnabled),
		fmt.Sprintf("LAN_MDNS_ENABLED=%t", cfg.LANMdnsEnabled),
		fmt.Sprintf("USB_RAW_ENABLED=%t", cfg.USBRawEnabled),
		fmt.Sprintf("WS_ENABLED=%t", cfg.WSEnabled),
		fmt.Sprintf("ZMQ_ENABLED=%t", cfg.ZMQEnabled),
		fmt.Sprintf("ZMQ_PUB_PORT=%d", cfg.ZMQPubPort),
		fmt.Sprintf("RESCAN_INTERVAL_SEC=%d", cfg.RescanIntervalSec),
		fmt.Sprintf("LOG_LEVEL=%s", cfg.LogLevel),
	)

	if cfg.ProfileDir != "" {
		env = append(env, fmt.Sprintf("PROFILE_DIR=%s", cfg.ProfileDir))
	}
	if cfg.VisaBackend != "" {
		env = append(env, fmt.Sprintf("VISA_BACKEND=%s", cfg.VisaBackend))
	}
	if len(cfg.LANInstruments) > 0 {
		env = append(env, fmt.Sprintf("LAN_INSTRUMENTS=%s", strings.Join(cfg.LANInstruments, ",")))
	}

	// Pass through any unrecognized config.env keys (e.g. DEMO_MODE,
	// MODBUS_INSTRUMENTS) so the Python child can read them.
	for k, v := range cfg.Extra {
		env = append(env, fmt.Sprintf("%s=%s", k, v))
	}

	return env
}

// createProxies creates TCP proxies for both the tsnet listener (if
// available) and a fallback listener on all interfaces.
func createProxies(name string, externalPort int, targetAddr string, tsnetSrv *network.Server) []*proxy.TCPProxy {
	var proxies []*proxy.TCPProxy

	// tsnet listener (tailnet-only traffic).
	if tsnetSrv != nil {
		tsLn, err := tsnetSrv.Listen("tcp", fmt.Sprintf(":%d", externalPort))
		if err != nil {
			slog.Warn("tsnet listener failed", "name", name, "port", externalPort, "error", err)
		} else {
			p := proxy.New(name+"-tsnet", tsLn, targetAddr)
			proxies = append(proxies, p)
			slog.Info("tsnet proxy created", "name", name, "port", externalPort)
		}
	}

	// Fallback listener on all interfaces.
	addr := fmt.Sprintf("0.0.0.0:%d", externalPort)
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		slog.Warn("fallback listener failed", "name", name, "addr", addr, "error", err)
	} else {
		p := proxy.New(name+"-fallback", ln, targetAddr)
		proxies = append(proxies, p)
		slog.Info("fallback proxy created", "name", name, "addr", addr)
	}

	return proxies
}

// parseLogLevel converts a string log level name to an slog.Level value.
func parseLogLevel(s string) slog.Level {
	switch strings.ToLower(strings.TrimSpace(s)) {
	case "debug":
		return slog.LevelDebug
	case "warn", "warning":
		return slog.LevelWarn
	case "error":
		return slog.LevelError
	default:
		return slog.LevelInfo
	}
}

// persistTailnetCredentials saves the tailnet credentials (TAILSCALE_AUTH_KEY
// and optionally HEADSCALE_URL) to the config file using a read-modify-write
// pattern. If the config file doesn't exist yet, a minimal file is created.
func persistTailnetCredentials(cfg *config.Config, resolvedPath string) {
	cfgPath := resolvedPath
	if cfgPath == "" || cfgPath == "(defaults + env)" {
		// No config file on disk yet — fall back to user config dir.
		cfgPath = filepath.Join(config.UserConfigDir(), "config.env")
	}

	// Read existing file (may not exist).
	kvs := make(map[string]string)
	if _, err := os.Stat(cfgPath); err == nil {
		existing, err := config.ParseFile(cfgPath)
		if err != nil {
			slog.Warn("cannot read existing config for tailnet credential persistence", "path", cfgPath, "error", err)
			return
		}
		kvs = existing
	}

	kvs["TAILSCALE_AUTH_KEY"] = cfg.TailscaleAuthKey
	if cfg.HeadscaleURL != "" {
		kvs["HEADSCALE_URL"] = cfg.HeadscaleURL
	}

	if err := config.WriteFileMap(cfgPath, kvs); err != nil {
		slog.Warn("failed to persist tailnet credentials to config", "path", cfgPath, "error", err)
		return
	}
	slog.Info("received tailnet credentials from backend, saved to config", "path", cfgPath)
}

// --------------------------------------------------------------------------
// instrumentGetterAdapter bridges grpcclient.Client to registration.InstrumentGetter.
// --------------------------------------------------------------------------

type instrumentGetterAdapter struct {
	client *grpcclient.Client
}

func (a *instrumentGetterAdapter) GetInstruments(ctx context.Context) ([]registration.InstrumentInfo, error) {
	instruments, err := a.client.GetInstruments(ctx)
	if err != nil {
		return nil, err
	}

	out := make([]registration.InstrumentInfo, 0, len(instruments))
	for _, inst := range instruments {
		status := "connected"
		if !inst.GetIsConnected() {
			status = "disconnected"
		}
		out = append(out, registration.InstrumentInfo{
			ID:           inst.GetId(),
			VisaAddress:  inst.GetAddress(),
			Name:         inst.GetIdnString(),
			Manufacturer: inst.GetManufacturer(),
			Model:        inst.GetModel(),
			Status:       status,
		})
	}
	return out, nil
}

// resolveRelayURL determines the WebSocket relay URL. If RELAY_URL is set
// explicitly in config, use it. Otherwise derive it from BACKEND_URL by
// replacing http(s):// with ws(s):// and appending /api/v1/relay/ws.
// Returns empty string if no relay should be used.
func resolveRelayURL(cfg *config.Config) string {
	if cfg.RelayURL != "" {
		return cfg.RelayURL
	}
	if cfg.BackendURL == "" {
		return ""
	}

	u := cfg.BackendURL
	if strings.HasPrefix(u, "https://") {
		u = "wss://" + strings.TrimPrefix(u, "https://")
	} else if strings.HasPrefix(u, "http://") {
		u = "ws://" + strings.TrimPrefix(u, "http://")
	} else {
		// Already a ws:// or wss:// URL, or unknown scheme.
		// Just use it as-is with the relay path appended.
	}

	u = strings.TrimRight(u, "/")
	return u + "/api/v1/relay/ws"
}
