package claudeingest

import (
	"bufio"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// BackfillOptions configures historical transcript ingestion.
type BackfillOptions struct {
	Consent *Consent
	Control *LocalControlClient
	RootDir string
	Now     func() time.Time
	DryRun  bool
}

// BackfillSummary reports what a backfill pass did.
type BackfillSummary struct {
	Scanned  int
	Matched  int
	Uploaded int
	Skipped  int
	Failed   int
}

// Backfill scans historical Claude Code transcripts and uploads deltas for
// transcripts whose recorded cwd is inside the consented folder set.
func Backfill(ctx context.Context, opts BackfillOptions) (BackfillSummary, error) {
	var summary BackfillSummary
	consent := opts.Consent
	if consent == nil {
		var err error
		consent, err = LoadConsent()
		if err != nil || consent == nil || !consent.Enabled {
			return summary, err
		}
	}
	if !consent.Enabled {
		return summary, nil
	}

	root := opts.RootDir
	if root == "" {
		var err error
		root, err = ClaudeProjectsDir()
		if err != nil {
			return summary, err
		}
	}

	offsetPath, err := OffsetsPath()
	if err != nil {
		return summary, err
	}
	offsets, err := NewOffsetStore(offsetPath)
	if err != nil {
		return summary, err
	}

	control := opts.Control
	if control == nil {
		control = NewLocalControlClient("")
	}
	now := time.Now
	if opts.Now != nil {
		now = opts.Now
	}

	err = filepath.WalkDir(root, func(path string, d os.DirEntry, walkErr error) error {
		if walkErr != nil {
			summary.Skipped++
			return nil
		}
		if d.IsDir() || !strings.EqualFold(filepath.Ext(path), ".jsonl") {
			return nil
		}
		summary.Scanned++

		meta := InspectTranscript(path)
		if meta.SessionID == "" {
			meta.SessionID = strings.TrimSuffix(filepath.Base(path), filepath.Ext(path))
		}
		if meta.CWD == "" {
			meta.CWD = fallbackCWDFromProjectDir(filepath.Base(filepath.Dir(path)), consent.AllowedFolders)
		}
		if meta.CWD == "" || !IsPathAllowed(meta.CWD, consent.AllowedFolders) {
			summary.Skipped++
			return nil
		}
		summary.Matched++

		key := OffsetKey(meta.SessionID, path)
		start := offsets.Get(key)
		lines, end, err := ReadTranscriptLines(path, start)
		if err != nil || len(lines) == 0 {
			summary.Skipped++
			return nil
		}
		if opts.DryRun {
			summary.Uploaded++
			return nil
		}

		batch := EventBatch{
			Version:        BatchVersion,
			Subject:        consent.Subject,
			SessionID:      meta.SessionID,
			HookEventName:  "Backfill",
			CWD:            meta.CWD,
			TranscriptPath: path,
			OffsetStart:    start,
			OffsetEnd:      end,
			Lines:          lines,
			SentAt:         now().UTC(),
		}
		if err := control.PostEvents(ctx, batch); err != nil {
			summary.Failed++
			return nil
		}
		if err := offsets.Set(key, end); err != nil {
			summary.Failed++
			return nil
		}
		summary.Uploaded++
		return nil
	})
	return summary, err
}

// ClaudeProjectsDir returns Claude Code's default transcript project root.
func ClaudeProjectsDir() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, ".claude", "projects"), nil
}

// TranscriptMeta contains identifying fields recovered from a transcript.
type TranscriptMeta struct {
	SessionID string
	CWD       string
}

// InspectTranscript scans a JSONL transcript for top-level cwd/session fields.
func InspectTranscript(path string) TranscriptMeta {
	f, err := os.Open(path)
	if err != nil {
		return TranscriptMeta{}
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 64*1024), 4*1024*1024)
	var meta TranscriptMeta
	for scanner.Scan() {
		var rec map[string]any
		if err := json.Unmarshal(scanner.Bytes(), &rec); err != nil {
			continue
		}
		if meta.CWD == "" {
			if cwd, ok := rec["cwd"].(string); ok {
				meta.CWD = cwd
			}
		}
		if meta.SessionID == "" {
			if sessionID, ok := rec["sessionId"].(string); ok {
				meta.SessionID = sessionID
			} else if sessionID, ok := rec["session_id"].(string); ok {
				meta.SessionID = sessionID
			}
		}
		if meta.CWD != "" && meta.SessionID != "" {
			break
		}
	}
	return meta
}

func fallbackCWDFromProjectDir(name string, allowed []string) string {
	for _, folder := range allowed {
		encoded := encodeClaudeProjectDir(folder)
		if name == encoded || strings.HasPrefix(name, encoded+"-") {
			return folder
		}
	}
	return ""
}

func encodeClaudeProjectDir(path string) string {
	path = filepath.Clean(path)
	replacer := strings.NewReplacer(
		"/", "-",
		`\`, "-",
		":", "-",
	)
	return replacer.Replace(path)
}
