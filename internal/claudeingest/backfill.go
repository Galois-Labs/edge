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
	// Cancel, when closed, signals the walk to stop at the next
	// transcript boundary. Useful for `claude disable` to abort an
	// in-flight backfill via /v1/claude/cancel-backfill.
	Cancel <-chan struct{}
	Now    func() time.Time
	DryRun bool
}

// BackfillSkipReason captures why a transcript was skipped for
// observability.
type BackfillSkipReason string

const (
	SkipNoCWD            BackfillSkipReason = "no-cwd"
	SkipCWDNotAllowed    BackfillSkipReason = "cwd-not-allowed"
	SkipExcludeGlob      BackfillSkipReason = "exclude-glob"
	SkipNothingNew       BackfillSkipReason = "nothing-new"
	SkipReadFailed       BackfillSkipReason = "read-failed"
	SkipUploadFailed     BackfillSkipReason = "upload-failed"
)

// BackfillSummary reports what a backfill pass did.
type BackfillSummary struct {
	Scanned   int
	Matched   int
	Uploaded  int
	Skipped   int
	Failed    int
	SkipsBy   map[BackfillSkipReason]int
	Cancelled bool
}

// Backfill scans historical Claude Code transcripts and uploads deltas
// for transcripts whose recorded cwd is inside the consented folder
// set. Idempotent under the same anchor store as live hooks.
//
// The encoded-directory-name fallback is intentionally not used in v2.
// Earlier designs derived cwd from the encoded directory name (which
// is lossy because hyphenated paths collide), creating a path where a
// session in a non-consented sibling could be uploaded under a forged
// cwd. v2 requires per-event cwd in the transcript itself; transcripts
// without it are skipped with reason "no-cwd".
func Backfill(ctx context.Context, opts BackfillOptions) (BackfillSummary, error) {
	summary := BackfillSummary{SkipsBy: map[BackfillSkipReason]int{}}
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

	anchorPath, err := AnchorsPath()
	if err != nil {
		return summary, err
	}
	store, err := NewAnchorStore(anchorPath)
	if err != nil {
		// Best-effort: continue with an in-memory store. Future Set
		// will rewrite the corrupt file.
		store, _ = NewAnchorStore(anchorPath)
		if store == nil {
			return summary, err
		}
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
		// Cancellation check at every transcript boundary.
		select {
		case <-ctx.Done():
			summary.Cancelled = true
			return filepath.SkipAll
		default:
		}
		if opts.Cancel != nil {
			select {
			case <-opts.Cancel:
				summary.Cancelled = true
				return filepath.SkipAll
			default:
			}
		}
		if walkErr != nil {
			summary.Skipped++
			summary.SkipsBy[SkipReadFailed]++
			return nil
		}
		if d.IsDir() || !strings.EqualFold(filepath.Ext(path), ".jsonl") {
			return nil
		}
		summary.Scanned++

		meta := InspectTranscript(path)
		if meta.CWD == "" {
			// v2 dropped the encoded-name fallback. Skip with a
			// distinct reason for observability.
			summary.Skipped++
			summary.SkipsBy[SkipNoCWD]++
			return nil
		}
		if !IsPathAllowed(meta.CWD, consent.AllowedFolders) {
			summary.Skipped++
			summary.SkipsBy[SkipCWDNotAllowed]++
			return nil
		}
		if matchesAnyExcludeGlob(meta.CWD, consent.ExcludeGlobs) {
			summary.Skipped++
			summary.SkipsBy[SkipExcludeGlob]++
			return nil
		}
		if meta.SessionID == "" {
			meta.SessionID = strings.TrimSuffix(filepath.Base(path), filepath.Ext(path))
		}
		summary.Matched++

		key := AnchorKey(meta.SessionID, path)
		anchor := store.Get(key)

		parsed, err := parseTranscript(path)
		if err != nil || len(parsed) == 0 {
			summary.Skipped++
			summary.SkipsBy[SkipReadFailed]++
			return nil
		}

		tail, anchorBefore := resumeTail(parsed, anchor.LastAckedUUID)
		if len(tail) == 0 {
			summary.Skipped++
			summary.SkipsBy[SkipNothingNew]++
			return nil
		}

		filtered := filterTail(tail, consent)
		anchorAfter := tail[len(tail)-1].uuid
		if len(filtered) == 0 {
			// Filter dropped everything; advance anchor locally so
			// subsequent backfills don't rescan the same range.
			_ = store.Set(key, Anchor{
				LastAckedUUID:      anchorAfter,
				LastAckedOffset:    fileSizeOrZero(path),
				UploadedEventCount: anchor.UploadedEventCount,
				UpdatedAt:          now(),
			})
			summary.Skipped++
			summary.SkipsBy[SkipNothingNew]++
			return nil
		}

		if opts.DryRun {
			summary.Uploaded++
			return nil
		}

		if consent.CredentialRedactor {
			filtered = RedactBatchLines(filtered)
		}

		batch := EventBatch{
			Version:            BatchVersion,
			ClientVersion:      consent.ClientVersion,
			Features:           append([]string(nil), KnownFeatures...),
			Subject:            consent.Subject,
			SessionID:          meta.SessionID,
			HookEventName:      "Backfill",
			CWD:                meta.CWD,
			TranscriptPath:     path,
			AnchorUUIDBefore:   anchorBefore,
			AnchorUUIDAfter:    anchorAfter,
			IncludeSidechains:  consent.IncludeSidechains,
			CredentialRedactor: consent.CredentialRedactor,
			Lines:              filtered,
			SentAt:             now().UTC(),
		}
		if err := control.PostEvents(ctx, batch); err != nil {
			summary.Failed++
			summary.SkipsBy[SkipUploadFailed]++
			return nil
		}
		if err := store.Set(key, Anchor{
			LastAckedUUID:      anchorAfter,
			LastAckedOffset:    fileSizeOrZero(path),
			UploadedEventCount: anchor.UploadedEventCount + len(filtered),
			UpdatedAt:          now(),
		}); err != nil {
			summary.Failed++
			return nil
		}
		summary.Uploaded++
		return nil
	})
	if err != nil {
		return summary, err
	}
	return summary, nil
}

// ClaudeProjectsDir returns Claude Code's default transcript project
// root.
func ClaudeProjectsDir() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, ".claude", "projects"), nil
}

// TranscriptMeta contains identifying fields recovered from a
// transcript.
type TranscriptMeta struct {
	SessionID string
	CWD       string
}

// InspectTranscript scans a JSONL transcript for top-level cwd/session
// fields. It reads only the leading lines; extracting these fields
// requires no full parse.
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
