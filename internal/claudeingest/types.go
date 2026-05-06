// Package claudeingest implements the daemon-side Claude Code ingestion
// connector. It is intentionally Go-only: Claude Code hooks run as the user
// and the Go daemon owns cloud auth/tsnet upload.
package claudeingest

import (
	"encoding/json"
	"time"
)

const (
	// ManagedHookMarker is embedded in the installed Claude Code hook command
	// so enable/disable can update only the Galois-managed hook entry.
	ManagedHookMarker = "galois-claude-ingest-v1"

	// ConsentVersion is the schema version of Consent payloads emitted by
	// the daemon. Per spec, additive changes do not bump this; only
	// semantic changes do.
	ConsentVersion = 1

	// BatchVersion is the schema version of EventBatch payloads.
	BatchVersion = 1

	// ConsentCacheTTL is how long the daemon caches a cloud consent answer
	// before refetching. Applied identically to enabled and disabled
	// answers. Cloud-side changes propagate via relay invalidation
	// (see cloud-requirements doc) when available; otherwise within one
	// TTL.
	ConsentCacheTTL = 60 * time.Second

	// MaxBatchBytes caps the size of a /v1/claude/events request body at
	// the local control endpoint. Larger requests are rejected 400.
	MaxBatchBytes = 8 * 1024 * 1024

	// DaemonShutdownDrain is the maximum time the control endpoint waits
	// for in-flight uploads to finish during daemon shutdown.
	DaemonShutdownDrain = 30 * time.Second

	// HookLogMaxBytes caps the per-user hook.log file before rotation.
	HookLogMaxBytes = 5 * 1024 * 1024

	// PrefixHashWindow is the byte window used by the legacy offset+prefix
	// verification path. Retained as a constant for tests; v2 resume is
	// UUID-anchored and does not depend on this value.
	PrefixHashWindow = 4096

	// DefaultControlAddr is the legacy TCP loopback address. Kept for
	// transitional compatibility while control.go is being migrated to
	// Unix sockets / named pipes (see spec § Local Control Endpoint).
	// Will be removed once the migration lands.
	DefaultControlAddr = "127.0.0.1:50117"
)

// Feature strings used in Consent.Features and EventBatch.Features. The
// cloud uses these for capability negotiation; absence of a feature must
// be treated conservatively (assume the edge does NOT have that
// capability). See docs/claude-code-ingest-cloud-requirements.md §7.
const (
	FeatureUUIDAnchoredResume = "uuid-anchored-resume"
	FeatureSidechainFilter    = "sidechain-filter"
	FeatureExcludeGlobs       = "exclude-globs"
	FeatureCredentialRedactor = "credential-redactor"
)

// KnownFeatures is the canonical list of feature strings this client
// advertises as supported. The list reflects daemon capabilities, not
// per-consent runtime settings — e.g., the credential pre-redactor
// capability is advertised regardless of whether any active consent
// has it turned on. Per-batch behavior is conveyed in EventBatch /
// Consent boolean fields. Tests verify the slice stays in sync with
// the Feature* constants.
var KnownFeatures = []string{
	FeatureUUIDAnchoredResume,
	FeatureSidechainFilter,
	FeatureExcludeGlobs,
	FeatureCredentialRedactor,
}

// Subject identifies the local user scope for consent. Key is the stable
// identifier; the other fields are display/audit metadata. Per v2 spec the
// key is sha256(install_id || "\x00" || os_user); install_id itself is not
// exposed on the wire.
type Subject struct {
	Key      string `json:"key"`
	OSUser   string `json:"os_user"`
	Hostname string `json:"hostname"`
	HomeDir  string `json:"home_dir"`
}

// Consent is the local/cloud representation of Claude Code ingestion
// consent.
type Consent struct {
	Version            int        `json:"version"`
	ClientVersion      string     `json:"client_version,omitempty"`
	Features           []string   `json:"features,omitempty"`
	Enabled            bool       `json:"enabled"`
	Subject            Subject    `json:"subject"`
	AllowedFolders     []string   `json:"allowed_folders"`
	ExcludeGlobs       []string   `json:"exclude_globs,omitempty"`
	IncludeSidechains  bool       `json:"include_sidechains"`
	CredentialRedactor bool       `json:"credential_redactor"`
	ConsentedAt        time.Time  `json:"consented_at"`
	UpdatedAt          time.Time  `json:"updated_at"`
	CloudSyncedAt      *time.Time `json:"cloud_synced_at,omitempty"`
	Source             string     `json:"source"`
}

// HookInput is the common Claude Code hook payload subset this connector
// uses.
type HookInput struct {
	SessionID      string `json:"session_id"`
	TranscriptPath string `json:"transcript_path"`
	CWD            string `json:"cwd"`
	HookEventName  string `json:"hook_event_name"`
}

// EventBatch is sent from the hook to the local daemon, then to cloud.
//
// Anchor fields (AnchorUUIDBefore / AnchorUUIDAfter) are diagnostic; they
// describe the resume position and the new acked position. They are NOT
// dedup keys — the cloud dedupes by per-line uuid. See
// docs/claude-code-ingest-cloud-requirements.md §1.
//
// OffsetStart and OffsetEnd are kept for v1-client compatibility during
// migration. v2 callers may leave them zero.
type EventBatch struct {
	Version            int               `json:"version"`
	ClientVersion      string            `json:"client_version,omitempty"`
	Features           []string          `json:"features,omitempty"`
	Subject            Subject           `json:"subject"`
	SessionID          string            `json:"session_id"`
	HookEventName      string            `json:"hook_event_name"`
	CWD                string            `json:"cwd"`
	TranscriptPath     string            `json:"transcript_path"`
	AnchorUUIDBefore   string            `json:"anchor_uuid_before,omitempty"`
	AnchorUUIDAfter    string            `json:"anchor_uuid_after,omitempty"`
	IncludeSidechains  bool              `json:"include_sidechains"`
	CredentialRedactor bool              `json:"credential_redactor"`
	OffsetStart        int64             `json:"offset_start,omitempty"`
	OffsetEnd          int64             `json:"offset_end,omitempty"`
	Lines              []json.RawMessage `json:"lines"`
	SentAt             time.Time         `json:"sent_at"`
}

// ConsentOptions configures non-default behavior at consent creation time.
// Zero value yields the defaults documented in the spec.
type ConsentOptions struct {
	ExcludeGlobs       []string
	ExcludeSidechains  bool // when true, IncludeSidechains is set false
	CredentialRedactor bool
	ClientVersion      string
}

// NewConsent creates an enabled consent payload for a normalized folder
// list. Sidechains are included by default; the credential pre-redactor is
// off by default.
func NewConsent(subject Subject, folders []string, now time.Time) Consent {
	return NewConsentWithOptions(subject, folders, now, ConsentOptions{})
}

// NewConsentWithOptions builds an enabled consent record honoring opts.
func NewConsentWithOptions(subject Subject, folders []string, now time.Time, opts ConsentOptions) Consent {
	return Consent{
		Version:            ConsentVersion,
		ClientVersion:      opts.ClientVersion,
		Features:           append([]string(nil), KnownFeatures...),
		Enabled:            true,
		Subject:            subject,
		AllowedFolders:     append([]string(nil), folders...),
		ExcludeGlobs:       append([]string(nil), opts.ExcludeGlobs...),
		IncludeSidechains:  !opts.ExcludeSidechains,
		CredentialRedactor: opts.CredentialRedactor,
		ConsentedAt:        now,
		UpdatedAt:          now,
		Source:             "galois-edge-cli",
	}
}

// DisabledConsent creates a disabled consent payload while preserving
// subject identity. AllowedFolders, etc. are left empty by default; the
// caller may copy them from a prior record for audit.
func DisabledConsent(subject Subject, now time.Time) Consent {
	return Consent{
		Version:   ConsentVersion,
		Enabled:   false,
		Subject:   subject,
		UpdatedAt: now,
		Source:    "galois-edge-cli",
	}
}
