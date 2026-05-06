package claudeingest

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"time"
)

// HookRunner processes a single Claude Code hook invocation.
type HookRunner struct {
	Control *LocalControlClient
	Env     func(string) string
	Now     func() time.Time
}

// NewHookRunner returns a runner that posts to the default local control
// endpoint.
func NewHookRunner() *HookRunner {
	return &HookRunner{
		Control: NewLocalControlClient(""),
		Env:     os.Getenv,
		Now:     time.Now,
	}
}

// Run reads a Claude Code hook payload from r, extracts new transcript lines,
// sends them to the local daemon, and advances offsets only after success.
func (h *HookRunner) Run(ctx context.Context, r io.Reader) error {
	var input HookInput
	if err := json.NewDecoder(r).Decode(&input); err != nil {
		return nil // never break Claude Code for malformed hook input
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

	offsetPath, err := OffsetsPath()
	if err != nil {
		return nil
	}
	offsets, err := NewOffsetStore(offsetPath)
	if err != nil {
		return nil
	}
	key := OffsetKey(input.SessionID, input.TranscriptPath)
	start := offsets.Get(key)

	lines, end, err := ReadTranscriptLines(input.TranscriptPath, start)
	if err != nil || len(lines) == 0 {
		return nil
	}

	now := time.Now()
	if h.Now != nil {
		now = h.Now()
	}
	batch := EventBatch{
		Version:        BatchVersion,
		Subject:        consent.Subject,
		SessionID:      input.SessionID,
		HookEventName:  input.HookEventName,
		CWD:            input.CWD,
		TranscriptPath: input.TranscriptPath,
		OffsetStart:    start,
		OffsetEnd:      end,
		Lines:          lines,
		SentAt:         now.UTC(),
	}

	control := h.Control
	if control == nil {
		control = NewLocalControlClient("")
	}
	if err := control.PostEvents(ctx, batch); err != nil {
		return nil // retry later by not advancing offset
	}
	return offsets.Set(key, end)
}

// ReadTranscriptLines reads newline-delimited JSON objects from path starting
// at startOffset. It returns only valid JSON lines and the byte offset after the
// last complete line read.
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
