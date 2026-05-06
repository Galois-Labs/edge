package claudeingest

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// CloudClient talks to the cloud ingestion API using the daemon's existing API
// key. It deliberately has no knowledge of edge registration internals.
type CloudClient struct {
	baseURL string
	apiKey  string
	client  *http.Client
}

// NewCloudClient creates a cloud ingestion client.
func NewCloudClient(baseURL, apiKey string, client *http.Client) *CloudClient {
	if client == nil {
		client = &http.Client{Timeout: 30 * time.Second}
	}
	return &CloudClient{
		baseURL: strings.TrimRight(baseURL, "/"),
		apiKey:  apiKey,
		client:  client,
	}
}

// PutConsent writes consent state to cloud.
func (c *CloudClient) PutConsent(ctx context.Context, consent Consent) error {
	return c.doJSON(ctx, http.MethodPut, "/api/v1/claude-ingest/consent", consent, nil)
}

// GetConsent fetches cloud consent for subjectKey.
func (c *CloudClient) GetConsent(ctx context.Context, subjectKey string) (*Consent, bool, error) {
	path := "/api/v1/claude-ingest/consent"
	if subjectKey != "" {
		path += "?subject_key=" + url.QueryEscape(subjectKey)
	}
	var consent Consent
	found, err := c.doJSONFound(ctx, http.MethodGet, path, nil, &consent)
	if err != nil || !found {
		return nil, found, err
	}
	return &consent, true, nil
}

// PostEvents uploads a transcript delta batch.
func (c *CloudClient) PostEvents(ctx context.Context, batch EventBatch) error {
	return c.doJSON(ctx, http.MethodPost, "/api/v1/claude-ingest/events", batch, nil)
}

func (c *CloudClient) doJSON(ctx context.Context, method, path string, in any, out any) error {
	_, err := c.doJSONFound(ctx, method, path, in, out)
	return err
}

func (c *CloudClient) doJSONFound(ctx context.Context, method, path string, in any, out any) (bool, error) {
	if c.baseURL == "" {
		return false, fmt.Errorf("backend URL is empty")
	}
	if c.apiKey == "" {
		return false, fmt.Errorf("registration token is empty")
	}

	var body io.Reader
	if in != nil {
		b, err := json.Marshal(in)
		if err != nil {
			return false, fmt.Errorf("marshal request: %w", err)
		}
		body = bytes.NewReader(b)
	}

	req, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, body)
	if err != nil {
		return false, err
	}
	req.Header.Set("X-API-Key", c.apiKey)
	if in != nil {
		req.Header.Set("Content-Type", "application/json")
	}

	resp, err := c.client.Do(req)
	if err != nil {
		return false, err
	}
	defer resp.Body.Close()

	respBody, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode == http.StatusNotFound {
		return false, nil
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return false, fmt.Errorf("%s %s: status %d: %s", method, path, resp.StatusCode, string(respBody))
	}
	if out != nil && len(respBody) > 0 {
		if err := json.Unmarshal(respBody, out); err != nil {
			return true, fmt.Errorf("decode response: %w", err)
		}
	}
	return true, nil
}
