package claudeingest

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

// ClaudeSettingsPath returns Claude Code's per-user settings file path.
func ClaudeSettingsPath() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, ".claude", "settings.json"), nil
}

// InstallManagedHooks merges the managed Galois hook into Claude Code user
// settings. Unknown settings are preserved.
func InstallManagedHooks(settingsPath, command string) error {
	settings, err := readSettings(settingsPath)
	if err != nil {
		return err
	}

	hooks, _ := settings["hooks"].(map[string]any)
	if hooks == nil {
		hooks = map[string]any{}
		settings["hooks"] = hooks
	}

	for _, event := range []string{"Stop", "SessionEnd"} {
		hooks[event] = upsertManagedGroup(hooks[event], command)
	}

	return writeSettings(settingsPath, settings)
}

// RemoveManagedHooks removes only hook handlers containing ManagedHookMarker.
func RemoveManagedHooks(settingsPath string) error {
	settings, err := readSettings(settingsPath)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	hooks, _ := settings["hooks"].(map[string]any)
	if hooks == nil {
		return nil
	}

	for _, event := range []string{"Stop", "SessionEnd"} {
		cleaned, keep := removeManagedFromEvent(hooks[event])
		if keep {
			hooks[event] = cleaned
		} else {
			delete(hooks, event)
		}
	}
	if len(hooks) == 0 {
		delete(settings, "hooks")
	}
	return writeSettings(settingsPath, settings)
}

// ManagedHookInstalled reports whether the managed hook marker appears in the
// user settings file.
func ManagedHookInstalled(settingsPath string) bool {
	data, err := os.ReadFile(settingsPath)
	return err == nil && strings.Contains(string(data), ManagedHookMarker)
}

// ManagedHookCommand returns the shell command written into Claude settings.
func ManagedHookCommand(exePath string) string {
	return shellQuote(exePath) + " claude hook --managed-hook " + ManagedHookMarker
}

func readSettings(path string) (map[string]any, error) {
	settings := map[string]any{}
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return settings, nil
		}
		return nil, err
	}
	if len(strings.TrimSpace(string(data))) == 0 {
		return settings, nil
	}
	if err := json.Unmarshal(data, &settings); err != nil {
		return nil, fmt.Errorf("parse Claude settings %s: %w", path, err)
	}
	return settings, nil
}

func writeSettings(path string, settings map[string]any) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	data, err := json.MarshalIndent(settings, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	return os.WriteFile(path, data, 0o600)
}

func upsertManagedGroup(existing any, command string) []any {
	groups, _ := existing.([]any)
	groups = removeManagedGroups(groups)
	group := map[string]any{
		"hooks": []any{
			map[string]any{
				"type":    "command",
				"command": command,
				"async":   true,
				"timeout": float64(120),
			},
		},
	}
	return append(groups, group)
}

func removeManagedFromEvent(existing any) ([]any, bool) {
	groups, _ := existing.([]any)
	groups = removeManagedGroups(groups)
	return groups, len(groups) > 0
}

func removeManagedGroups(groups []any) []any {
	out := make([]any, 0, len(groups))
	for _, groupAny := range groups {
		group, ok := groupAny.(map[string]any)
		if !ok {
			out = append(out, groupAny)
			continue
		}
		hooksAny, _ := group["hooks"].([]any)
		cleanedHooks := make([]any, 0, len(hooksAny))
		for _, hookAny := range hooksAny {
			hook, ok := hookAny.(map[string]any)
			if !ok {
				cleanedHooks = append(cleanedHooks, hookAny)
				continue
			}
			cmd, _ := hook["command"].(string)
			if strings.Contains(cmd, ManagedHookMarker) {
				continue
			}
			cleanedHooks = append(cleanedHooks, hookAny)
		}
		if len(cleanedHooks) == 0 {
			continue
		}
		group["hooks"] = cleanedHooks
		out = append(out, group)
	}
	return out
}

func shellQuote(s string) string {
	if runtime.GOOS == "windows" {
		return `"` + strings.ReplaceAll(s, `"`, `\"`) + `"`
	}
	return `'` + strings.ReplaceAll(s, `'`, `'\''`) + `'`
}
