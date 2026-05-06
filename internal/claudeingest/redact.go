package claudeingest

import (
	"encoding/json"
	"fmt"
	"regexp"
)

// CredentialClass is the redaction tag applied to a matched value.
type CredentialClass string

const (
	classAWSAccessKey CredentialClass = "aws-access-key"
	classGitHubToken  CredentialClass = "gh-token"
	classAnthropicKey CredentialClass = "anthropic-key"
	classOpenAIKey    CredentialClass = "openai-key"
	classBearerJWT    CredentialClass = "bearer-jwt"
	classSlackToken   CredentialClass = "slack-token"
)

// credentialRule pairs a class label with a whole-string regex. Whole-
// string matches (^...$) prevent the substring-shred problem that would
// otherwise corrupt legitimate base64 blobs, signed URLs, and content
// hashes embedded in tool results.
type credentialRule struct {
	class CredentialClass
	re    *regexp.Regexp
}

// credentialRules is the ordered rule set. Order matters: the
// anthropic-key rule must be evaluated before openai-key because both
// start with `sk-`. Tests enforce this ordering.
var credentialRules = []credentialRule{
	{
		class: classAnthropicKey,
		re:    regexp.MustCompile(`^sk-ant-[A-Za-z0-9_-]{32,}$`),
	},
	{
		class: classOpenAIKey,
		re:    regexp.MustCompile(`^sk-(?:proj-)?[A-Za-z0-9_-]{40,}$`),
	},
	{
		class: classAWSAccessKey,
		re:    regexp.MustCompile(`^(AKIA|ASIA)[0-9A-Z]{16}$`),
	},
	{
		class: classGitHubToken,
		re:    regexp.MustCompile(`^gh[ps]_[A-Za-z0-9]{36,255}$`),
	},
	{
		class: classBearerJWT,
		re:    regexp.MustCompile(`^eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}$`),
	},
	{
		class: classSlackToken,
		re:    regexp.MustCompile(`^xox[abprs]-[A-Za-z0-9-]{10,}$`),
	},
}

// redactString runs the rule set against s and returns the redaction
// marker if any rule matches end-to-end, or s unchanged otherwise.
func redactString(s string) string {
	if len(s) < 16 {
		// All credential classes are at least ~16 chars; skip the regex
		// engine for short strings to avoid scanning every "type",
		// "role", uuid fragment, etc.
		return s
	}
	for _, rule := range credentialRules {
		if rule.re.MatchString(s) {
			return fmt.Sprintf("[REDACTED:%s]", rule.class)
		}
	}
	return s
}

// RedactEventLine walks a JSONL event recursively and replaces any
// string leaf whose entire value matches a credential rule. Structural
// fields (uuids, timestamps, type tags) won't match the rules and are
// returned untouched. The event's JSON shape is preserved exactly.
//
// On JSON parse failure RedactEventLine returns the input bytes
// unchanged — defensive: if a future Claude Code event format isn't
// parseable as JSON for some reason, we'd rather upload it as-is and
// rely on cloud-side redaction than drop it.
func RedactEventLine(line json.RawMessage) json.RawMessage {
	var v interface{}
	if err := json.Unmarshal(line, &v); err != nil {
		return line
	}
	v = redactValue(v)
	out, err := json.Marshal(v)
	if err != nil {
		return line
	}
	return out
}

// redactValue is the recursive walker.
func redactValue(v interface{}) interface{} {
	switch t := v.(type) {
	case string:
		return redactString(t)
	case map[string]interface{}:
		for k, child := range t {
			t[k] = redactValue(child)
		}
		return t
	case []interface{}:
		for i, child := range t {
			t[i] = redactValue(child)
		}
		return t
	default:
		// numbers, booleans, nulls — never carry credential strings.
		return v
	}
}

// RedactBatchLines applies the pre-redactor to every line in a batch.
// Convenience for hook callers; equivalent to mapping RedactEventLine
// across batch.Lines.
func RedactBatchLines(lines []json.RawMessage) []json.RawMessage {
	if len(lines) == 0 {
		return lines
	}
	out := make([]json.RawMessage, len(lines))
	for i, line := range lines {
		out[i] = RedactEventLine(line)
	}
	return out
}
