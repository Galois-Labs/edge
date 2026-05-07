package grpcclient

import (
	"context"
	"testing"
)

// TestWithBearerToken_GetRequestMetadata verifies that the bearer-token
// PerRPCCredentials implementation injects the correct Authorization header.
func TestWithBearerToken_GetRequestMetadata(t *testing.T) {
	token := "glc_internal_test_abc123"
	cred := bearerTokenCredentials{token: token}

	md, err := cred.GetRequestMetadata(context.Background())
	if err != nil {
		t.Fatalf("GetRequestMetadata returned unexpected error: %v", err)
	}

	want := "Bearer " + token
	got, ok := md["authorization"]
	if !ok {
		t.Fatal("authorization key missing from metadata")
	}
	if got != want {
		t.Errorf("authorization header: got %q, want %q", got, want)
	}
}

// TestWithBearerToken_RequireTransportSecurity verifies that the credential
// does NOT require TLS (daemon uses plaintext on loopback).
func TestWithBearerToken_RequireTransportSecurity(t *testing.T) {
	cred := bearerTokenCredentials{token: "any"}
	if cred.RequireTransportSecurity() {
		t.Error("RequireTransportSecurity should return false for plaintext loopback connections")
	}
}

// TestWithBearerToken_DialOption verifies that WithBearerToken returns a
// non-nil DialOption (structural check only — we don't actually dial here).
func TestWithBearerToken_DialOption(t *testing.T) {
	opt := WithBearerToken("glc_internal_abc")
	if opt == nil {
		t.Error("WithBearerToken must return a non-nil grpc.DialOption")
	}
}

// TestWithBearerToken_EmptyToken verifies that an empty token still produces
// a valid (if useless) DialOption without panicking.
func TestWithBearerToken_EmptyToken(t *testing.T) {
	opt := WithBearerToken("")
	if opt == nil {
		t.Error("WithBearerToken('') must return a non-nil grpc.DialOption")
	}

	cred := bearerTokenCredentials{token: ""}
	md, err := cred.GetRequestMetadata(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	got := md["authorization"]
	want := "Bearer "
	if got != want {
		t.Errorf("authorization with empty token: got %q, want %q", got, want)
	}
}
