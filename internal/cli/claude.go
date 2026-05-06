package cli

import (
	"bufio"
	"context"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/galois-labs/edge/internal/claudeingest"
	"github.com/spf13/cobra"
)

var claudeConfigPath string

// claudeCmd groups Claude Code ingestion commands.
var claudeCmd = &cobra.Command{
	Use:   "claude",
	Short: "Manage Claude Code knowledge ingestion",
	Long: `Manage optional Claude Code transcript ingestion. Setup remains unchanged:
register with "galois-edge setup <TOKEN>", then opt in once with
"galois-edge claude enable <folder...>".`,
}

var claudeEnableCmd = &cobra.Command{
	Use:   "enable <folder> [folder...]",
	Short: "Enable Claude Code ingestion for selected folders",
	Args:  cobra.MinimumNArgs(1),
	Run:   runClaudeEnable,
}

var claudeBackfillCmd = &cobra.Command{
	Use:   "backfill",
	Short: "Ingest previous Claude Code chats for consented folders",
	Run:   runClaudeBackfill,
}

var claudeDisableCmd = &cobra.Command{
	Use:   "disable",
	Short: "Disable Claude Code ingestion and remove managed hooks",
	Run:   runClaudeDisable,
}

var claudeStatusCmd = &cobra.Command{
	Use:   "status",
	Short: "Show Claude Code ingestion status",
	Run:   runClaudeStatus,
}

var claudeHookCmd = &cobra.Command{
	Use:    "hook",
	Short:  "Internal Claude Code hook entry point",
	Hidden: true,
	Run:    runClaudeHook,
}

func init() {
	claudeCmd.PersistentFlags().StringVar(&claudeConfigPath, "config", "", "path to config.env file")
	claudeEnableCmd.Flags().Bool("yes", false, "confirm consent non-interactively")
	claudeEnableCmd.Flags().Bool("no-backfill", false, "skip historical transcript backfill after enabling")
	claudeBackfillCmd.Flags().Bool("dry-run", false, "scan and report without uploading or advancing offsets")
	claudeHookCmd.Flags().String("managed-hook", "", "managed hook marker")
	claudeCmd.AddCommand(claudeEnableCmd)
	claudeCmd.AddCommand(claudeBackfillCmd)
	claudeCmd.AddCommand(claudeDisableCmd)
	claudeCmd.AddCommand(claudeStatusCmd)
	claudeCmd.AddCommand(claudeHookCmd)
}

func runClaudeEnable(cmd *cobra.Command, args []string) {
	folders, err := claudeingest.NormalizeFolders(args)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}

	subject, err := claudeingest.LocalSubject()
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: cannot determine local user: %v\n", err)
		os.Exit(1)
	}
	now := time.Now().UTC()
	consent := claudeingest.NewConsent(subject, folders, now)
	alreadyEnabled := false
	if existing, err := claudeingest.LoadConsent(); err == nil && existing != nil && existing.Enabled {
		alreadyEnabled = sameStringSlice(existing.AllowedFolders, folders)
		if alreadyEnabled {
			consent = *existing
			consent.UpdatedAt = now
		}
	}

	yes, _ := cmd.Flags().GetBool("yes")
	if !alreadyEnabled && !yes && !confirmClaudeConsent(folders) {
		fmt.Println("Claude Code ingestion not enabled.")
		return
	}

	if err := claudeingest.SaveConsent(consent); err != nil {
		fmt.Fprintf(os.Stderr, "error: cannot save local consent: %v\n", err)
		os.Exit(1)
	}

	settingsPath, err := claudeingest.ClaudeSettingsPath()
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: cannot find Claude settings path: %v\n", err)
		os.Exit(1)
	}
	exePath := managedExecutablePath()
	if err := claudeingest.InstallManagedHooks(settingsPath, claudeingest.ManagedHookCommand(exePath)); err != nil {
		fmt.Fprintf(os.Stderr, "error: cannot install Claude Code hook: %v\n", err)
		os.Exit(1)
	}

	synced, syncErr := syncClaudeConsent(cmd.Context(), consent)
	if synced {
		syncTime := time.Now().UTC()
		consent.CloudSyncedAt = &syncTime
		_ = claudeingest.SaveConsent(consent)
	}

	if alreadyEnabled {
		fmt.Println("Claude Code ingestion already enabled; repaired local hook and cloud sync.")
	} else {
		fmt.Println("Claude Code ingestion enabled.")
	}
	fmt.Printf("  Folders: %d\n", len(folders))
	for _, folder := range folders {
		fmt.Printf("    - %s\n", folder)
	}
	fmt.Printf("  Hook:    %s\n", settingsPath)
	if synced {
		fmt.Println("  Cloud:   consent synced")
	} else {
		fmt.Printf("  Cloud:   sync pending (%v)\n", syncErr)
	}

	noBackfill, _ := cmd.Flags().GetBool("no-backfill")
	if !noBackfill {
		runBackfillAndPrint(cmd.Context(), false)
	}
}

func runClaudeBackfill(cmd *cobra.Command, args []string) {
	dryRun, _ := cmd.Flags().GetBool("dry-run")
	runBackfillAndPrint(cmd.Context(), dryRun)
}

func runClaudeDisable(cmd *cobra.Command, args []string) {
	subject, err := claudeingest.LocalSubject()
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: cannot determine local user: %v\n", err)
		os.Exit(1)
	}
	consent := claudeingest.DisabledConsent(subject, time.Now().UTC())
	if existing, err := claudeingest.LoadConsent(); err == nil && existing != nil {
		consent.AllowedFolders = existing.AllowedFolders
		consent.ConsentedAt = existing.ConsentedAt
	}

	if err := claudeingest.SaveConsent(consent); err != nil {
		fmt.Fprintf(os.Stderr, "error: cannot save local consent: %v\n", err)
		os.Exit(1)
	}
	if settingsPath, err := claudeingest.ClaudeSettingsPath(); err == nil {
		if err := claudeingest.RemoveManagedHooks(settingsPath); err != nil {
			fmt.Fprintf(os.Stderr, "warning: cannot remove Claude Code hook: %v\n", err)
		}
	}

	synced, syncErr := syncClaudeConsent(cmd.Context(), consent)
	if synced {
		syncTime := time.Now().UTC()
		consent.CloudSyncedAt = &syncTime
		_ = claudeingest.SaveConsent(consent)
	}

	fmt.Println("Claude Code ingestion disabled.")
	if synced {
		fmt.Println("  Cloud: consent synced")
	} else {
		fmt.Printf("  Cloud: sync pending (%v)\n", syncErr)
	}
}

func runClaudeStatus(cmd *cobra.Command, args []string) {
	consent, err := claudeingest.LoadConsent()
	if err != nil {
		if os.IsNotExist(err) {
			fmt.Println("Claude Code ingestion: not configured")
		} else {
			fmt.Printf("Claude Code ingestion: local consent unreadable (%v)\n", err)
		}
	} else if consent != nil && consent.Enabled {
		fmt.Println("Claude Code ingestion: enabled")
		for _, folder := range consent.AllowedFolders {
			fmt.Printf("  - %s\n", folder)
		}
		if consent.CloudSyncedAt != nil {
			fmt.Printf("  Cloud consent synced: %s\n", consent.CloudSyncedAt.Format(time.RFC3339))
		} else {
			fmt.Println("  Cloud consent synced: pending/unknown")
		}
	} else {
		fmt.Println("Claude Code ingestion: disabled")
	}

	if settingsPath, err := claudeingest.ClaudeSettingsPath(); err == nil {
		if claudeingest.ManagedHookInstalled(settingsPath) {
			fmt.Printf("Claude Code hook: installed (%s)\n", settingsPath)
		} else {
			fmt.Printf("Claude Code hook: not installed (%s)\n", settingsPath)
		}
	}

	ctx, cancel := context.WithTimeout(cmd.Context(), 2*time.Second)
	defer cancel()
	if localControlHealthy(ctx) {
		fmt.Println("Daemon control endpoint: reachable")
	} else {
		fmt.Println("Daemon control endpoint: not reachable")
	}
}

func runClaudeHook(cmd *cobra.Command, args []string) {
	marker, _ := cmd.Flags().GetString("managed-hook")
	if marker != claudeingest.ManagedHookMarker {
		return
	}
	ctx, cancel := context.WithTimeout(cmd.Context(), 55*time.Second)
	defer cancel()
	runner := claudeingest.NewHookRunner()
	_ = runner.Run(ctx, os.Stdin)
}

func confirmClaudeConsent(folders []string) bool {
	fmt.Println("Galois can ingest Claude Code transcripts from these folders only:")
	fmt.Println()
	for _, folder := range folders {
		fmt.Printf("  %s\n", folder)
	}
	fmt.Println()
	fmt.Println("Transcripts may contain prompts, assistant responses, tool results, file paths,")
	fmt.Println("and code snippets that appear in Claude Code chats.")
	fmt.Println()
	fmt.Print("Enable Claude Code ingestion? [y/N] ")

	reader := bufio.NewReader(os.Stdin)
	answer, _ := reader.ReadString('\n')
	answer = strings.TrimSpace(strings.ToLower(answer))
	return answer == "y" || answer == "yes"
}

func syncClaudeConsent(parent context.Context, consent claudeingest.Consent) (bool, error) {
	ctx, cancel := context.WithTimeout(parent, 20*time.Second)
	defer cancel()

	local := claudeingest.NewLocalControlClient("")
	if err := local.PostConsent(ctx, consent); err == nil {
		return true, nil
	}

	cfg, _, err := loadConfig(claudeConfigPath)
	if err != nil {
		return false, err
	}
	if cfg.BackendURL == "" || cfg.RegistrationToken == "" {
		return false, fmt.Errorf("backend URL or registration token not configured")
	}
	cloud := claudeingest.NewCloudClient(cfg.BackendURL, cfg.RegistrationToken, nil)
	if err := cloud.PutConsent(ctx, consent); err != nil {
		return false, err
	}
	return true, nil
}

func localControlHealthy(ctx context.Context) bool {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://"+claudeingest.DefaultControlAddr+"/health", nil)
	if err != nil {
		return false
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode >= 200 && resp.StatusCode < 300
}

func runBackfillAndPrint(parent context.Context, dryRun bool) {
	ctx, cancel := context.WithTimeout(parent, 5*time.Minute)
	defer cancel()
	summary, err := claudeingest.Backfill(ctx, claudeingest.BackfillOptions{
		DryRun: dryRun,
	})
	if err != nil {
		fmt.Printf("  Backfill: skipped (%v)\n", err)
		return
	}
	label := "complete"
	if dryRun {
		label = "dry run complete"
	}
	fmt.Printf("  Backfill: %s: %d transcripts scanned, %d matched, %d uploaded, %d skipped, %d failed\n",
		label,
		summary.Scanned,
		summary.Matched,
		summary.Uploaded,
		summary.Skipped,
		summary.Failed,
	)
}

func managedExecutablePath() string {
	exe, err := os.Executable()
	if err != nil {
		return "galois-edge"
	}
	if resolved, err := filepath.EvalSymlinks(exe); err == nil {
		return resolved
	}
	return exe
}

func sameStringSlice(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
