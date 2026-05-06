package claudeingest

import (
	"testing"
	"time"
)

func TestKnownFeaturesContainsAllConstants(t *testing.T) {
	expected := map[string]bool{
		FeatureUUIDAnchoredResume: false,
		FeatureSidechainFilter:    false,
		FeatureExcludeGlobs:       false,
		FeatureCredentialRedactor: false,
	}
	for _, f := range KnownFeatures {
		if _, ok := expected[f]; !ok {
			t.Errorf("KnownFeatures contains unknown feature %q", f)
			continue
		}
		expected[f] = true
	}
	for f, found := range expected {
		if !found {
			t.Errorf("KnownFeatures missing feature constant %q", f)
		}
	}
}

func TestNewConsentDefaults(t *testing.T) {
	now := time.Now().UTC()
	c := NewConsent(Subject{Key: "k"}, []string{"/a"}, now)
	if c.Version != ConsentVersion {
		t.Errorf("Version: got %d want %d", c.Version, ConsentVersion)
	}
	if !c.Enabled {
		t.Errorf("Enabled: got false")
	}
	if !c.IncludeSidechains {
		t.Errorf("IncludeSidechains: default should be true")
	}
	if c.CredentialRedactor {
		t.Errorf("CredentialRedactor: default should be false")
	}
	if len(c.Features) != len(KnownFeatures) {
		t.Errorf("Features: got %v want %v", c.Features, KnownFeatures)
	}
	if c.Source != "galois-edge-cli" {
		t.Errorf("Source: got %q", c.Source)
	}
	if !c.ConsentedAt.Equal(now) || !c.UpdatedAt.Equal(now) {
		t.Errorf("timestamps not set to now")
	}
}

func TestNewConsentWithOptions(t *testing.T) {
	now := time.Now().UTC()
	c := NewConsentWithOptions(
		Subject{Key: "k"},
		[]string{"/a"},
		now,
		ConsentOptions{
			ExcludeGlobs:       []string{"**/secrets/**"},
			ExcludeSidechains:  true,
			CredentialRedactor: true,
			ClientVersion:      "galois-edge/0.42.0",
		},
	)
	if c.IncludeSidechains {
		t.Errorf("ExcludeSidechains=true should set IncludeSidechains=false")
	}
	if !c.CredentialRedactor {
		t.Errorf("CredentialRedactor: got false want true")
	}
	if c.ClientVersion != "galois-edge/0.42.0" {
		t.Errorf("ClientVersion: got %q", c.ClientVersion)
	}
	if len(c.ExcludeGlobs) != 1 || c.ExcludeGlobs[0] != "**/secrets/**" {
		t.Errorf("ExcludeGlobs: got %v", c.ExcludeGlobs)
	}
}

func TestDisabledConsent(t *testing.T) {
	now := time.Now().UTC()
	c := DisabledConsent(Subject{Key: "k"}, now)
	if c.Enabled {
		t.Errorf("DisabledConsent.Enabled: got true")
	}
	if c.Version != ConsentVersion {
		t.Errorf("Version: got %d want %d", c.Version, ConsentVersion)
	}
	if !c.UpdatedAt.Equal(now) {
		t.Errorf("UpdatedAt not now")
	}
}
