package claudeingest

import (
	"os"
	"strings"
	"testing"

	"github.com/galois-labs/edge/internal/installid"
)

// readInstallIDForTest reads whichever install id file Ensure populated.
// Lives in test_helpers_test.go so the production binary doesn't ship a
// helper that exposes the system path.
func readInstallIDForTest(t *testing.T) (string, error) {
	t.Helper()
	for _, p := range []string{installid.SystemPath(), installid.UserPath()} {
		b, err := os.ReadFile(p)
		if err == nil {
			return strings.TrimSpace(string(b)), nil
		}
	}
	return "", os.ErrNotExist
}
