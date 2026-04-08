package relay

import (
	"encoding/json"
	"testing"
	"time"
)

// ---------------------------------------------------------------------------
// Backoff tests
// ---------------------------------------------------------------------------

func TestBackoffDelayDeterministic(t *testing.T) {
	tests := []struct {
		attempt  int
		expected time.Duration
	}{
		{0, 2 * time.Second},  // clamped to attempt 1
		{1, 2 * time.Second},  // 2s * 2^0 = 2s
		{2, 4 * time.Second},  // 2s * 2^1 = 4s
		{3, 8 * time.Second},  // 2s * 2^2 = 8s
		{4, 16 * time.Second}, // 2s * 2^3 = 16s
		{5, 32 * time.Second}, // 2s * 2^4 = 32s
		{6, 64 * time.Second}, // 2s * 2^5 = 64s
		{7, 128 * time.Second},
		{8, 256 * time.Second},
		{9, 5 * time.Minute}, // capped at 5min
		{10, 5 * time.Minute},
		{20, 5 * time.Minute},
	}

	for _, tt := range tests {
		got := BackoffDelayDeterministic(tt.attempt)
		if got != tt.expected {
			t.Errorf("BackoffDelayDeterministic(%d) = %v, want %v", tt.attempt, got, tt.expected)
		}
	}
}

func TestBackoffDelayHasJitter(t *testing.T) {
	// BackoffDelay should return a value between base and base * 1.25.
	for attempt := 1; attempt <= 5; attempt++ {
		base := BackoffDelayDeterministic(attempt)
		maxWithJitter := time.Duration(float64(base) * 1.25)

		for i := 0; i < 50; i++ {
			got := BackoffDelay(attempt)
			if got < base {
				t.Errorf("BackoffDelay(%d) = %v, want >= %v (base)", attempt, got, base)
			}
			if got > maxWithJitter {
				t.Errorf("BackoffDelay(%d) = %v, want <= %v (base*1.25)", attempt, got, maxWithJitter)
			}
		}
	}
}

func TestBackoffDelayCappedAt5Min(t *testing.T) {
	// Even with jitter, should not exceed 5min * 1.25 = 6.25min.
	maxAllowed := time.Duration(float64(5*time.Minute) * 1.25)

	for i := 0; i < 100; i++ {
		got := BackoffDelay(100)
		if got > maxAllowed {
			t.Errorf("BackoffDelay(100) = %v, exceeded max allowed %v", got, maxAllowed)
		}
	}
}

// ---------------------------------------------------------------------------
// JSON serialization tests
// ---------------------------------------------------------------------------

func TestMarshalHelloMessage(t *testing.T) {
	msg := &relayMessage{
		Type:     "hello",
		EdgeID:   "test-uuid",
		EdgeName: "pi5-demo",
		Version:  "1.0.0",
	}

	data, err := MarshalMessage(msg)
	if err != nil {
		t.Fatalf("MarshalMessage: %v", err)
	}

	// Verify it can be parsed back.
	got, err := UnmarshalMessage(data)
	if err != nil {
		t.Fatalf("UnmarshalMessage: %v", err)
	}

	if got.Type != "hello" {
		t.Errorf("Type = %q, want %q", got.Type, "hello")
	}
	if got.EdgeID != "test-uuid" {
		t.Errorf("EdgeID = %q, want %q", got.EdgeID, "test-uuid")
	}
	if got.EdgeName != "pi5-demo" {
		t.Errorf("EdgeName = %q, want %q", got.EdgeName, "pi5-demo")
	}
	if got.Version != "1.0.0" {
		t.Errorf("Version = %q, want %q", got.Version, "1.0.0")
	}
}

func TestMarshalHeartbeatMessage(t *testing.T) {
	now := time.Now().UnixMilli()
	msg := &relayMessage{
		Type:        "heartbeat",
		TimestampMs: now,
	}

	data, err := MarshalMessage(msg)
	if err != nil {
		t.Fatalf("MarshalMessage: %v", err)
	}

	got, err := UnmarshalMessage(data)
	if err != nil {
		t.Fatalf("UnmarshalMessage: %v", err)
	}

	if got.Type != "heartbeat" {
		t.Errorf("Type = %q, want %q", got.Type, "heartbeat")
	}
	if got.TimestampMs != now {
		t.Errorf("TimestampMs = %d, want %d", got.TimestampMs, now)
	}
}

func TestMarshalCommandResponse(t *testing.T) {
	msg := &relayMessage{
		Type:            "command_response",
		RequestID:       "req-123",
		Success:         true,
		Data:            "1.234",
		ScpiCommand:     "MEAS:VOLT:DC?",
		ExecutionTimeMs: 45,
	}

	data, err := MarshalMessage(msg)
	if err != nil {
		t.Fatalf("MarshalMessage: %v", err)
	}

	got, err := UnmarshalMessage(data)
	if err != nil {
		t.Fatalf("UnmarshalMessage: %v", err)
	}

	if got.Type != "command_response" {
		t.Errorf("Type = %q, want %q", got.Type, "command_response")
	}
	if got.RequestID != "req-123" {
		t.Errorf("RequestID = %q, want %q", got.RequestID, "req-123")
	}
	if !got.Success {
		t.Error("Success = false, want true")
	}
	if got.Data != "1.234" {
		t.Errorf("Data = %q, want %q", got.Data, "1.234")
	}
	if got.ScpiCommand != "MEAS:VOLT:DC?" {
		t.Errorf("ScpiCommand = %q, want %q", got.ScpiCommand, "MEAS:VOLT:DC?")
	}
	if got.ExecutionTimeMs != 45 {
		t.Errorf("ExecutionTimeMs = %d, want %d", got.ExecutionTimeMs, 45)
	}
}

func TestUnmarshalCommandRequest(t *testing.T) {
	raw := `{
		"type": "command_request",
		"request_id": "uuid-456",
		"instrument_id": "GPIB0::22::INSTR",
		"command_name": "measure_voltage",
		"parameters": {"range": "10"},
		"is_query": true
	}`

	got, err := UnmarshalMessage([]byte(raw))
	if err != nil {
		t.Fatalf("UnmarshalMessage: %v", err)
	}

	if got.Type != "command_request" {
		t.Errorf("Type = %q, want %q", got.Type, "command_request")
	}
	if got.RequestID != "uuid-456" {
		t.Errorf("RequestID = %q, want %q", got.RequestID, "uuid-456")
	}
	if got.InstrumentID != "GPIB0::22::INSTR" {
		t.Errorf("InstrumentID = %q, want %q", got.InstrumentID, "GPIB0::22::INSTR")
	}
	if got.CommandName != "measure_voltage" {
		t.Errorf("CommandName = %q, want %q", got.CommandName, "measure_voltage")
	}
	if !got.IsQuery {
		t.Error("IsQuery = false, want true")
	}
	if got.Parameters["range"] != "10" {
		t.Errorf("Parameters[range] = %q, want %q", got.Parameters["range"], "10")
	}
}

func TestOmitEmptyFields(t *testing.T) {
	// A hello message should not include command_response fields.
	msg := &relayMessage{
		Type:     "hello",
		EdgeID:   "test",
		EdgeName: "test-name",
		Version:  "1.0.0",
	}

	data, err := MarshalMessage(msg)
	if err != nil {
		t.Fatalf("MarshalMessage: %v", err)
	}

	// Parse into a generic map to check omitted fields.
	var m map[string]interface{}
	if err := json.Unmarshal(data, &m); err != nil {
		t.Fatalf("json.Unmarshal: %v", err)
	}

	// These fields should be omitted (omitempty).
	omitted := []string{
		"timestamp_ms", "request_id", "instrument_id", "command_name",
		"parameters", "is_query", "success", "data", "error_message",
		"scpi_command", "execution_time_ms",
	}
	for _, key := range omitted {
		if _, ok := m[key]; ok {
			t.Errorf("field %q should be omitted from hello message, but was present", key)
		}
	}
}

func TestMarshalErrorResponse(t *testing.T) {
	msg := &relayMessage{
		Type:            "command_response",
		RequestID:       "req-err",
		Success:         false,
		ErrorMessage:    "instrument not found",
		ExecutionTimeMs: 12,
	}

	data, err := MarshalMessage(msg)
	if err != nil {
		t.Fatalf("MarshalMessage: %v", err)
	}

	got, err := UnmarshalMessage(data)
	if err != nil {
		t.Fatalf("UnmarshalMessage: %v", err)
	}

	if got.Success {
		t.Error("Success = true, want false")
	}
	if got.ErrorMessage != "instrument not found" {
		t.Errorf("ErrorMessage = %q, want %q", got.ErrorMessage, "instrument not found")
	}
}
