//go:build windows

package tray

import (
	"context"
	"fmt"
	"log"
	"runtime"
	"time"

	"github.com/galois-labs/edge/internal/config"
)

const (
	pollInterval    = 5 * time.Second
	trayIconID      = 1
	wmLButtonDblClk = 0x0203
)

// App is the main tray application.
type App struct {
	version      string
	cfg          *config.Config
	poller       *Poller
	trayIcon     *TrayIcon
	hWnd         uintptr
	hIconGreen   uintptr
	hIconYellow  uintptr
	hIconRed     uintptr
	dashboardURL string
}

// NewApp creates the tray application. Call Run() to start it.
func NewApp(version string) (*App, error) {
	cfg, err := config.Load("")
	if err != nil {
		// If config not found, use defaults.
		cfg = config.New()
	}

	dashURL := cfg.BackendURL
	if dashURL == "" {
		dashURL = "https://cloud.galoislabs.ai"
	}

	target := fmt.Sprintf("127.0.0.1:%d", cfg.GRPCInternalPort)

	return &App{
		version:      version,
		cfg:          cfg,
		poller:       NewPoller(target, pollInterval),
		dashboardURL: dashURL,
	}, nil
}

// Run starts the tray application. It blocks until the user quits or ctx is cancelled.
func (a *App) Run(ctx context.Context) error {
	runtime.LockOSThread() // Win32 message loop must stay on this thread

	// Create hidden message window.
	hWnd, err := CreateMessageWindow("galois-edge-tray", a.wndProc)
	if err != nil {
		return fmt.Errorf("create message window: %w", err)
	}
	a.hWnd = hWnd

	// Load icons from embedded data.
	if err := a.loadIcons(); err != nil {
		return fmt.Errorf("load icons: %w", err)
	}

	// Create and show tray icon (start with red = unknown).
	a.trayIcon = NewTrayIcon(hWnd, trayIconID, WM_TRAYICON)
	a.trayIcon.SetIcon(a.hIconRed)
	a.trayIcon.SetTooltip("galois-edge: checking...")
	if err := a.trayIcon.Show(); err != nil {
		return fmt.Errorf("show tray icon: %w", err)
	}

	// Start poller.
	a.poller.Start(ctx)

	// Bridge poller updates to Win32 message loop.
	go func() {
		for {
			select {
			case <-ctx.Done():
				PostCustomMessage(a.hWnd, WM_POLLUPDATE)
				return
			case <-a.poller.Updates():
				PostCustomMessage(a.hWnd, WM_POLLUPDATE)
			}
		}
	}()

	// Context cancellation -> post WM_QUIT.
	go func() {
		<-ctx.Done()
		PostQuit()
	}()

	// Run Win32 message loop (blocks).
	RunMessageLoop()

	// Cleanup.
	a.trayIcon.Remove()
	a.poller.Stop()

	return nil
}

func (a *App) loadIcons() error {
	var err error
	a.hIconGreen, err = LoadIconFromBytes(iconGreenData)
	if err != nil {
		return fmt.Errorf("green icon: %w", err)
	}
	a.hIconYellow, err = LoadIconFromBytes(iconYellowData)
	if err != nil {
		return fmt.Errorf("yellow icon: %w", err)
	}
	a.hIconRed, err = LoadIconFromBytes(iconRedData)
	if err != nil {
		return fmt.Errorf("red icon: %w", err)
	}
	return nil
}

// wndProc handles Win32 messages for the hidden window.
func (a *App) wndProc(hWnd uintptr, msg uint32, wParam, lParam uintptr) uintptr {
	switch msg {
	case WM_TRAYICON:
		// lParam contains the mouse message.
		switch uint32(lParam) {
		case wmRButtonUp:
			a.showContextMenu()
		case wmLButtonDblClk:
			HandleAction(ActionOpenDashboard, a.dashboardURL)
		}
		return 0

	case WM_POLLUPDATE:
		a.onPollUpdate()
		return 0
	}

	return DefWindowProc(hWnd, msg, wParam, lParam)
}

func (a *App) onPollUpdate() {
	snap := a.poller.Latest()

	// Update icon color.
	switch snap.State {
	case StateOnline:
		a.trayIcon.SetIcon(a.hIconGreen)
	case StateDegraded:
		a.trayIcon.SetIcon(a.hIconYellow)
	default:
		a.trayIcon.SetIcon(a.hIconRed)
	}

	// Update tooltip.
	tooltip := fmt.Sprintf("galois-edge: %s", snap.State)
	if snap.InstrumentCount > 0 {
		tooltip = fmt.Sprintf("galois-edge: %s (%d instruments)", snap.State, snap.InstrumentCount)
	}
	a.trayIcon.SetTooltip(tooltip)
	a.trayIcon.Update()
}

func (a *App) showContextMenu() {
	snap := a.poller.Latest()
	items := BuildMenuItems(snap, a.version)

	hMenu, err := NewPopupMenu()
	if err != nil {
		log.Printf("tray: create menu: %v", err)
		return
	}
	defer DestroyMenuHandle(hMenu)

	for _, item := range items {
		if item.Separator {
			AddSeparator(hMenu)
		} else {
			AddMenuItem(hMenu, item.ID, item.Text, item.Enabled)
		}
	}

	selectedID := ShowMenu(a.hWnd, hMenu)
	if selectedID == 0 {
		return // user dismissed menu
	}

	action := MenuItemIDToAction(selectedID)
	if action == ActionQuit {
		PostQuit()
		return
	}
	HandleAction(action, a.dashboardURL)
}
