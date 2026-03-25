//go:build windows

package tray

import (
	"fmt"
	"log"
	"os/exec"
	"time"

	"github.com/galois-labs/edge/internal/service"
)

// MenuAction identifies a user-selected menu action.
type MenuAction uint16

const (
	ActionNone           MenuAction = iota
	ActionOpenDashboard             // opens BackendURL in default browser
	ActionRestartService            // stops then starts the Windows service
	ActionQuit                      // exits the tray application
)

// Menu item IDs used in Win32 menu (must be non-zero for TrackPopupMenu to return them)
const (
	menuIDDashboard uint16 = 100
	menuIDRestart   uint16 = 101
	menuIDQuit      uint16 = 102
)

// MenuItemIDToAction maps a Win32 menu item ID to a MenuAction.
func MenuItemIDToAction(id uint16) MenuAction {
	switch id {
	case menuIDDashboard:
		return ActionOpenDashboard
	case menuIDRestart:
		return ActionRestartService
	case menuIDQuit:
		return ActionQuit
	default:
		return ActionNone
	}
}

// BuildMenuItems is called by the app when constructing the Win32 popup menu.
// It returns the items to add. The app layer handles actual Win32 menu creation
// using the win32.go helpers.
//
// Menu layout:
//
//	galois-edge v1.2.3          (disabled, info)
//	Status: ONLINE              (disabled, info)
//	Instruments: 3 connected    (disabled, info)
//	───────────
//	Open Dashboard              (enabled)
//	───────────
//	Restart Service             (enabled)
//	Quit                        (enabled)
type MenuItemDef struct {
	ID        uint16
	Text      string
	Enabled   bool
	Separator bool // if true, this is a separator (other fields ignored)
}

func BuildMenuItems(snap StatusSnapshot, version string) []MenuItemDef {
	stateStr := snap.State.String()

	instrText := fmt.Sprintf("Instruments: %d connected", snap.InstrumentCount)
	if snap.State == StateOffline {
		instrText = "Instruments: --"
	}

	versionText := "galois-edge"
	if version != "" {
		versionText = fmt.Sprintf("galois-edge %s", version)
	}

	return []MenuItemDef{
		{Text: versionText, Enabled: false},
		{Text: fmt.Sprintf("Status: %s", stateStr), Enabled: false},
		{Text: instrText, Enabled: false},
		{Separator: true},
		{ID: menuIDDashboard, Text: "Open Dashboard", Enabled: true},
		{Separator: true},
		{ID: menuIDRestart, Text: "Restart Service", Enabled: true},
		{ID: menuIDQuit, Text: "Quit", Enabled: true},
	}
}

// HandleAction executes the given menu action.
func HandleAction(action MenuAction, dashboardURL string) {
	switch action {
	case ActionOpenDashboard:
		openBrowser(dashboardURL)
	case ActionRestartService:
		restartService()
	case ActionQuit:
		// Handled by caller (posts WM_QUIT)
	}
}

func openBrowser(url string) {
	if url == "" {
		url = "https://cloud.galoislabs.ai"
	}
	cmd := exec.Command("cmd", "/c", "start", "", url)
	if err := cmd.Start(); err != nil {
		log.Printf("tray: failed to open browser: %v", err)
	}
}

func restartService() {
	log.Printf("tray: restarting galois-edge service...")
	if err := service.StopService(); err != nil {
		log.Printf("tray: stop service: %v", err)
	}
	// Brief pause to let the service fully stop
	time.Sleep(2 * time.Second)
	if err := service.StartService(); err != nil {
		log.Printf("tray: start service: %v", err)
	}
	log.Printf("tray: service restart requested")
}
