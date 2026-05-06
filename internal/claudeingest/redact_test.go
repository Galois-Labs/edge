package claudeingest

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestRedactStringPositiveCases(t *testing.T) {
	cases := []struct {
		name  string
		in    string
		class CredentialClass
	}{
		{"aws-access-key AKIA", "AKIAIOSFODNN7EXAMPLE", classAWSAccessKey},
		{"aws-access-key ASIA", "ASIAIOSFODNN7EXAMPLE", classAWSAccessKey},
		{"github classic", "ghp_" + strings.Repeat("A", 40), classGitHubToken},
		{"github fine-grained", "ghs_" + strings.Repeat("a", 40), classGitHubToken},
		{"anthropic", "sk-ant-" + strings.Repeat("A", 60), classAnthropicKey},
		{"openai legacy", "sk-" + strings.Repeat("A", 50), classOpenAIKey},
		{"openai project", "sk-proj-" + strings.Repeat("A", 50), classOpenAIKey},
		{"jwt", "eyJ" + strings.Repeat("A", 20) + "." + strings.Repeat("B", 20) + "." + strings.Repeat("C", 20), classBearerJWT},
		{"slack", "xoxb-" + strings.Repeat("1", 30), classSlackToken},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := redactString(c.in)
			want := "[REDACTED:" + string(c.class) + "]"
			if got != want {
				t.Errorf("redactString(%q): got %q want %q", c.in, got, want)
			}
		})
	}
}

func TestRedactStringNegativeCases(t *testing.T) {
	cases := []string{
		"",
		"hello",
		"short",
		"00893aaf-19fa-41d2-8238-13269b9b3ca0",                                              // uuid
		"2026-05-05T12:00:00Z",                                                              // timestamp
		"sk-ant",                                                                            // truncated
		"sk-",                                                                               // truncated
		"AKIA",                                                                              // truncated
		"AKIAabcdefghij",                                                                    // wrong case (lowercase) — regex requires uppercase
		"https://example.com/path?q=" + strings.Repeat("a", 50),                             // URL with long string
		"data:image/png;base64," + strings.Repeat("A", 200),                                 // data url
		strings.Repeat("a", 100),                                                            // long lowercase, no prefix
		strings.Repeat("/", 80),                                                             // long slashes — no rule for plain base64
		"some long sentence with random AKIA letters but not the right shape AKIA12345",     // not anchored
		"prefix sk-ant-" + strings.Repeat("A", 60) + " suffix",                              // not whole-value
		"eyJ" + strings.Repeat("A", 20) + "." + strings.Repeat("B", 20),                     // jwt missing third segment
	}
	for _, in := range cases {
		t.Run(in, func(t *testing.T) {
			if got := redactString(in); got != in {
				t.Errorf("redactString(%q) modified to %q (false positive)", in, got)
			}
		})
	}
}

func TestRedactStringRuleOrderingAnthropicBeforeOpenAI(t *testing.T) {
	// sk-ant-... matches both anthropic (^sk-ant-...$) and the openai
	// legacy rule (^sk-...$). Anthropic must win.
	in := "sk-ant-" + strings.Repeat("A", 60)
	got := redactString(in)
	if got != "[REDACTED:anthropic-key]" {
		t.Fatalf("ordering broken: %q got %q (must be anthropic-key)", in, got)
	}
}

func TestRedactEventLinePreservesStructure(t *testing.T) {
	line := json.RawMessage(`{
		"uuid": "00893aaf-19fa-41d2-8238-13269b9b3ca0",
		"parentUuid": null,
		"type": "user",
		"isSidechain": false,
		"message": {
			"role": "user",
			"content": "my key is AKIAIOSFODNN7EXAMPLE today"
		},
		"toolUseResult": {
			"text": "anthropic = sk-ant-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
		}
	}`)
	out := RedactEventLine(line)
	var got map[string]interface{}
	if err := json.Unmarshal(out, &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	// Whole-value match required; substrings inside a sentence are NOT
	// redacted by design (cloud-side handles that).
	msg := got["message"].(map[string]interface{})
	content := msg["content"].(string)
	if !strings.Contains(content, "AKIA") {
		t.Errorf("substring should not be redacted: got %q", content)
	}

	// Whole-value match: a string field whose entire value is the
	// secret IS redacted.
	tu := got["toolUseResult"].(map[string]interface{})
	text := tu["text"].(string)
	if !strings.Contains(text, "anthropic = sk-ant-") {
		t.Errorf("substring inside compound text should not be redacted: %q", text)
	}

	// Structural fields untouched.
	if got["uuid"] != "00893aaf-19fa-41d2-8238-13269b9b3ca0" {
		t.Errorf("uuid mutated: %v", got["uuid"])
	}
	if got["type"] != "user" {
		t.Errorf("type mutated: %v", got["type"])
	}
}

func TestRedactEventLineWholeValueMatch(t *testing.T) {
	line := json.RawMessage(`{
		"uuid": "00893aaf-19fa-41d2-8238-13269b9b3ca0",
		"toolInput": {
			"api_key": "sk-ant-` + strings.Repeat("X", 60) + `"
		}
	}`)
	out := RedactEventLine(line)
	var got map[string]interface{}
	_ = json.Unmarshal(out, &got)
	ti := got["toolInput"].(map[string]interface{})
	if ti["api_key"] != "[REDACTED:anthropic-key]" {
		t.Errorf("whole-value api_key not redacted: %v", ti["api_key"])
	}
}

func TestRedactEventLineMalformedJSONIsPassthrough(t *testing.T) {
	in := json.RawMessage(`{not valid json`)
	out := RedactEventLine(in)
	if string(out) != string(in) {
		t.Errorf("malformed input should pass through, got %q", string(out))
	}
}

func TestRedactBatchLinesEmpty(t *testing.T) {
	if got := RedactBatchLines(nil); got != nil {
		t.Errorf("nil input should round-trip nil")
	}
	if got := RedactBatchLines([]json.RawMessage{}); len(got) != 0 {
		t.Errorf("empty input should round-trip empty")
	}
}
