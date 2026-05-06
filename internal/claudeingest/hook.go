package claudeingest

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// HookRunner processes a single Claude Code hook invocation.
//
// The runner is safe to construct cheaply per hook fire and discards
// itself afterward. State (anchors, consent, hook log) is persisted
// through filesystem helpers so concurrent hook invocations across
// sessions cooperate correctly.
type HookRunner struct {
	Control *LocalControlClient
	Env     func(string) string
	Now     func() time.Time
}

// NewHookRunner returns a runner that posts to the default local
// control endpoint.
func NewHookRunner() *HookRunner {
	return &HookRunner{
		Control: NewLocalControlClient(""),
		Env:     os.Getenv,
		Now:     time.Now,
	}
}

// Run reads a Claude Code hook payload from r, performs UUID-anchored
// resume against the transcript, applies sidechain/cwd/exclude-glob
// filters, optionally pre-redacts credentials, sends the batch to the
// local daemon control endpoint, and advances the anchor only on a 2xx
// ACK. Run NEVER returns a non-nil error in normal operation: every
// failure path exits the hook quietly so Claude Code is never blocked.
// (The error return is retained for tests to assert on internal state.)
func (h *HookRunner) Run(ctx context.Context, r io.Reader) error {
	var input HookInput
	if err := json.NewDecoder(r).Decode(&input); err != nil {
		return nil // never break Claude Code on malformed hook input
	}
	if input.CWD == "" && h.Env != nil {
		input.CWD = h.Env("CLAUDE_PROJECT_DIR")
	}
	if input.SessionID == "" || input.TranscriptPath == "" || input.CWD == "" {
		return nil
	}

	consent, err := LoadConsent()
	if err != nil || consent == nil || !consent.Enabled {
		return nil
	}
	if !IsPathAllowed(input.CWD, consent.AllowedFolders) {
		return nil
	}
	if matchesAnyExcludeGlob(input.CWD, consent.ExcludeGlobs) {
		return nil
	}

	anchorPath, err := AnchorsPath()
	if err != nil {
		return nil
	}
	store, err := NewAnchorStore(anchorPath)
	if err != nil {
		// Best-effort: continue with an empty in-memory store. The
		// next successful Set will rewrite the corrupt file.
		store, _ = NewAnchorStore(anchorPath)
		if store == nil {
			return nil
		}
	}
	key := AnchorKey(input.SessionID, input.TranscriptPath)
	anchor := store.Get(key)

	parsed, err := parseTranscript(input.TranscriptPath)
	if err != nil || len(parsed) == 0 {
		return nil
	}

	tail, anchorBefore := resumeTail(parsed, anchor.LastAckedUUID)
	if len(tail) == 0 {
		return nil
	}

	filtered := filterTail(tail, consent)
	anchorAfter := tail[len(tail)-1].uuid

	if len(filtered) == 0 {
		// Nothing the cloud should see, but we did process the tail —
		// advance the anchor locally so we don't rescan these events
		// on every subsequent hook fire.
		_ = store.Set(key, Anchor{
			LastAckedUUID:      anchorAfter,
			LastAckedOffset:    fileSizeOrZero(input.TranscriptPath),
			UploadedEventCount: anchor.UploadedEventCount,
			UpdatedAt:          h.now(),
		})
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
		SessionID:          input.SessionID,
		HookEventName:      input.HookEventName,
		CWD:                input.CWD,
		TranscriptPath:     input.TranscriptPath,
		AnchorUUIDBefore:   anchorBefore,
		AnchorUUIDAfter:    anchorAfter,
		IncludeSidechains:  consent.IncludeSidechains,
		CredentialRedactor: consent.CredentialRedactor,
		Lines:              filtered,
		SentAt:             h.now().UTC(),
	}

	control := h.Control
	if control == nil {
		control = NewLocalControlClient("")
	}
	if err := control.PostEvents(ctx, batch); err != nil {
		return nil // do not advance anchor on failure
	}
	return store.Set(key, Anchor{
		LastAckedUUID:      anchorAfter,
		LastAckedOffset:    fileSizeOrZero(input.TranscriptPath),
		UploadedEventCount: anchor.UploadedEventCount + len(filtered),
		UpdatedAt:          h.now(),
	})
}

func (h *HookRunner) now() time.Time {
	if h.Now != nil {
		return h.Now()
	}
	return time.Now()
}

// parsedLine is a minimal projection of a Claude Code transcript event
// holding only the fields the resume + filter logic needs, plus the
// original raw bytes so we can ship them unchanged.
type parsedLine struct {
	uuid        string
	parentUUID  string
	isSidechain bool
	cwd         string
	raw         json.RawMessage
}

// parseTranscript reads a JSONL file and returns every parseable line
// projected into parsedLine. Invalid lines are silently skipped — the
// hook prefers to upload less than to fail.
func parseTranscript(path string) ([]parsedLine, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	out := []parsedLine{}
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 64*1024), 16*1024*1024)
	for scanner.Scan() {
		line := scanner.Bytes()
		trimmed := trimJSONLine(line)
		if len(trimmed) == 0 {
			continue
		}
		if !json.Valid(trimmed) {
			continue
		}
		var head struct {
			UUID        string `json:"uuid"`
			ParentUUID  string `json:"parentUuid"`
			IsSidechain bool   `json:"isSidechain"`
			CWD         string `json:"cwd"`
		}
		_ = json.Unmarshal(trimmed, &head)
		cp := append([]byte(nil), trimmed...)
		out = append(out, parsedLine{
			uuid:        head.UUID,
			parentUUID:  head.ParentUUID,
			isSidechain: head.IsSidechain,
			cwd:         head.CWD,
			raw:         cp,
		})
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("scan transcript: %w", err)
	}
	return out, nil
}

// resumeTail implements the UUID-anchored resume rules. It returns the
// slice of events that should be considered for upload (tail) plus the
// anchor that was used for diagnostic anchor_uuid_before.
//
// Rules (mirroring the spec):
//  1. No prior anchor → fresh session: tail = all events.
//  2. Prior anchor exists, found at index i, and events[i+1].parentUuid
//     matches → fast path: tail = events[i+1:].
//  3. Prior anchor exists but not found → file rewound or compacted
//     past the anchor. Full re-stream: tail = all events. The cloud
//     dedupes by uuid.
//  4. Prior anchor found but parent-chain breaks → compaction
//     preserved the anchor but rewrote subsequent events. Re-stream
//     to be safe.
func resumeTail(events []parsedLine, lastAckedUUID string) (tail []parsedLine, anchorBefore string) {
	if lastAckedUUID == "" {
		return events, ""
	}
	idx := -1
	for i, e := range events {
		if e.uuid == lastAckedUUID {
			idx = i
			break
		}
	}
	if idx < 0 {
		// Anchor not present — file mutated; full re-stream.
		return events, ""
	}
	candidate := events[idx+1:]
	if len(candidate) == 0 {
		return nil, lastAckedUUID
	}
	if candidate[0].parentUUID != "" && candidate[0].parentUUID != lastAckedUUID {
		// Chain break — compaction or other mutation. Re-stream.
		return events, ""
	}
	return candidate, lastAckedUUID
}

// filterTail drops events the consent says we shouldn't upload and
// returns the surviving raw bytes. Per-event cwd that falls outside
// allowed_folders is filtered (defense against a session whose top-
// level cwd is allowed but contains nested events from a different
// working directory).
func filterTail(events []parsedLine, consent *Consent) []json.RawMessage {
	out := make([]json.RawMessage, 0, len(events))
	for _, e := range events {
		if !consent.IncludeSidechains && e.isSidechain {
			continue
		}
		if e.cwd != "" {
			if !IsPathAllowed(e.cwd, consent.AllowedFolders) {
				continue
			}
			if matchesAnyExcludeGlob(e.cwd, consent.ExcludeGlobs) {
				continue
			}
		}
		out = append(out, e.raw)
	}
	return out
}

// matchesAnyExcludeGlob returns true if path matches any of patterns.
// Patterns support a small subset of doublestar syntax sufficient for
// the v1 use cases (`**/X/**`, `**/X`, `X/**`, plain filepath.Match).
// Adding full doublestar support means pulling in a third-party dep;
// not worth it for v1.
func matchesAnyExcludeGlob(path string, patterns []string) bool {
	if len(patterns) == 0 {
		return false
	}
	clean := filepath.Clean(path)
	clean = filepath.ToSlash(clean)
	for _, pat := range patterns {
		if matchExcludeGlob(pat, clean) {
			return true
		}
	}
	return false
}

func matchExcludeGlob(pattern, path string) bool {
	pattern = strings.TrimSpace(pattern)
	if pattern == "" {
		return false
	}
	pattern = filepath.ToSlash(pattern)

	// **/X/** — match if path contains /X/.
	if strings.HasPrefix(pattern, "**/") && strings.HasSuffix(pattern, "/**") {
		mid := strings.TrimSuffix(strings.TrimPrefix(pattern, "**/"), "/**")
		if mid == "" {
			return true
		}
		needle := "/" + mid + "/"
		// Also match when path == /mid (trailing).
		return strings.Contains(path+"/", needle)
	}
	// **/X — suffix match on /X.
	if strings.HasPrefix(pattern, "**/") {
		suffix := strings.TrimPrefix(pattern, "**/")
		return strings.HasSuffix(path, "/"+suffix) || path == suffix
	}
	// X/** — prefix match on X/.
	if strings.HasSuffix(pattern, "/**") {
		prefix := strings.TrimSuffix(pattern, "/**")
		return path == prefix || strings.HasPrefix(path, prefix+"/")
	}
	// Fallback to filepath.Match (no recursive ** support).
	matched, _ := filepath.Match(pattern, path)
	return matched
}

// trimJSONLine strips leading/trailing whitespace from a JSONL record
// so json.Valid sees only the payload.
func trimJSONLine(line []byte) []byte {
	for len(line) > 0 {
		switch line[0] {
		case ' ', '\t', '\r', '\n':
			line = line[1:]
		default:
			goto trimRight
		}
	}
trimRight:
	for len(line) > 0 {
		switch line[len(line)-1] {
		case ' ', '\t', '\r', '\n':
			line = line[:len(line)-1]
		default:
			return line
		}
	}
	return line
}

// fileSizeOrZero returns the file size if stat succeeds, else 0.
func fileSizeOrZero(path string) int64 {
	st, err := os.Stat(path)
	if err != nil {
		return 0
	}
	return st.Size()
}

// ReadTranscriptLines is retained as a thin wrapper for legacy callers
// (notably backfill.go before its rewrite). It returns the raw lines
// from offset to EOF and the resulting byte offset; it does not perform
// UUID-anchored resume.
//
// Deprecated: legacy offset-based read. New code should use
// parseTranscript + resumeTail.
func ReadTranscriptLines(path string, startOffset int64) ([]json.RawMessage, int64, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, startOffset, err
	}
	defer f.Close()

	info, err := f.Stat()
	if err != nil {
		return nil, startOffset, err
	}
	if startOffset < 0 || startOffset > info.Size() {
		startOffset = 0
	}
	if _, err := f.Seek(startOffset, io.SeekStart); err != nil {
		return nil, startOffset, err
	}

	reader := bufio.NewReader(f)
	offset := startOffset
	lines := []json.RawMessage{}
	for {
		line, err := reader.ReadBytes('\n')
		if len(line) > 0 {
			offset += int64(len(line))
			trimmed := trimJSONLine(line)
			if len(trimmed) > 0 && json.Valid(trimmed) {
				cp := append([]byte(nil), trimmed...)
				lines = append(lines, json.RawMessage(cp))
			}
		}
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, startOffset, fmt.Errorf("read transcript: %w", err)
		}
	}
	return lines, offset, nil
}
