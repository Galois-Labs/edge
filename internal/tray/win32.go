//go:build windows

package tray

import (
	"encoding/binary"
	"fmt"
	"runtime"
	"unsafe"

	"golang.org/x/sys/windows"
)

// ---------------------------------------------------------------------------
// Lazy DLL / proc bindings (pure Go, no CGo).
// ---------------------------------------------------------------------------

var (
	shell32 = windows.NewLazySystemDLL("shell32.dll")
	user32  = windows.NewLazySystemDLL("user32.dll")
	gdi32   = windows.NewLazySystemDLL("gdi32.dll")

	procShellNotifyIconW = shell32.NewProc("Shell_NotifyIconW")

	procRegisterClassExW   = user32.NewProc("RegisterClassExW")
	procCreateWindowExW    = user32.NewProc("CreateWindowExW")
	procGetMessageW        = user32.NewProc("GetMessageW")
	procTranslateMessage   = user32.NewProc("TranslateMessage")
	procDispatchMessageW   = user32.NewProc("DispatchMessageW")
	procPostMessageW       = user32.NewProc("PostMessageW")
	procCreatePopupMenu    = user32.NewProc("CreatePopupMenu")
	procInsertMenuItemW    = user32.NewProc("InsertMenuItemW")
	procTrackPopupMenuEx   = user32.NewProc("TrackPopupMenuEx")
	procDestroyMenu        = user32.NewProc("DestroyMenu")
	procPostQuitMessage    = user32.NewProc("PostQuitMessage")
	procGetCursorPos       = user32.NewProc("GetCursorPos")
	procSetForegroundWindow = user32.NewProc("SetForegroundWindow")
	procDefWindowProcW     = user32.NewProc("DefWindowProcW")

	procCreateIconFromResourceEx = user32.NewProc("CreateIconFromResourceEx")
)

// ---------------------------------------------------------------------------
// Win32 constants.
// ---------------------------------------------------------------------------

const (
	// Shell_NotifyIcon operations.
	nimAdd    = 0x00000000
	nimModify = 0x00000001
	nimDelete = 0x00000002

	// NOTIFYICONDATA.uFlags
	nifMessage = 0x00000001
	nifIcon    = 0x00000002
	nifTip     = 0x00000004

	// Window messages.
	wmDestroy   = 0x0002
	wmRButtonUp = 0x0205

	// WM_APP range for application-defined messages.
	WM_APP        = 0x8000
	WM_TRAYICON   = WM_APP     // tray icon callback message
	WM_POLLUPDATE = WM_APP + 1 // poller has new data

	// CreateWindowEx: HWND_MESSAGE makes a message-only window.
	hwndMessage = ^uintptr(2) // HWND_MESSAGE = (HWND)-3

	// Window styles.
	wsOverlappedWindow = 0x00CF0000
	wsExAppWindow      = 0x00040000

	// TrackPopupMenuEx flags.
	tpmReturncmd = 0x0100
	tpmLeftAlign = 0x0000

	// MENUITEMINFOW.fMask
	miimString = 0x00000040
	miimID     = 0x00000002
	miimState  = 0x00000001
	miimFType  = 0x00000100

	// MENUITEMINFOW.fType
	mftString    = 0x00000000
	mftSeparator = 0x00000800

	// MENUITEMINFOW.fState
	mfsEnabled  = 0x00000000
	mfsDisabled = 0x00000003 // MFS_DISABLED | MFS_GRAYED

	// CreateIconFromResourceEx flags.
	lrDefaultColor = 0x00000000

	// Icon size (32x32).
	iconDefaultSize = 32
)

// ---------------------------------------------------------------------------
// Win32 structs.
// ---------------------------------------------------------------------------

// notifyIconData is NOTIFYICONDATAW (V1, up to szTip).
type notifyIconData struct {
	cbSize           uint32
	hWnd             uintptr
	uID              uint32
	uFlags           uint32
	uCallbackMessage uint32
	hIcon            uintptr
	szTip            [128]uint16
}

// wndClassEx is WNDCLASSEXW.
type wndClassEx struct {
	cbSize        uint32
	style         uint32
	lpfnWndProc   uintptr
	cbClsExtra    int32
	cbWndExtra    int32
	hInstance     uintptr
	hIcon         uintptr
	hCursor       uintptr
	hbrBackground uintptr
	lpszMenuName  *uint16
	lpszClassName *uint16
	hIconSm       uintptr
}

// menuItemInfo is MENUITEMINFOW.
type menuItemInfo struct {
	cbSize        uint32
	fMask         uint32
	fType         uint32
	fState        uint32
	wID           uint32
	hSubMenu      uintptr
	hbmpChecked   uintptr
	hbmpUnchecked uintptr
	dwItemData    uintptr
	dwTypeData    *uint16
	cch           uint32
	hbmpItem      uintptr
}

// point is POINT.
type point struct {
	x, y int32
}

// msg is MSG.
type msg struct {
	hwnd    uintptr
	message uint32
	wParam  uintptr
	lParam  uintptr
	time    uint32
	pt      point
}

// ---------------------------------------------------------------------------
// TrayIcon — Shell_NotifyIcon lifecycle wrapper.
// ---------------------------------------------------------------------------

// TrayIcon manages the Shell_NotifyIcon lifecycle.
type TrayIcon struct {
	hWnd uintptr
	id   uint32
	data notifyIconData
}

// NewTrayIcon creates a TrayIcon bound to hWnd. Call Show() to make it visible.
func NewTrayIcon(hWnd uintptr, id uint32, callbackMsg uint32) *TrayIcon {
	t := &TrayIcon{
		hWnd: hWnd,
		id:   id,
	}
	t.data.cbSize = uint32(unsafe.Sizeof(t.data))
	t.data.hWnd = hWnd
	t.data.uID = id
	t.data.uFlags = nifMessage
	t.data.uCallbackMessage = callbackMsg
	return t
}

// SetIcon sets the icon handle. Call Update() afterwards if already shown.
func (t *TrayIcon) SetIcon(hIcon uintptr) error {
	t.data.hIcon = hIcon
	t.data.uFlags |= nifIcon
	return nil
}

// SetTooltip sets the tooltip text (max 127 UTF-16 code units).
func (t *TrayIcon) SetTooltip(text string) error {
	tip, err := windows.UTF16FromString(text)
	if err != nil {
		return fmt.Errorf("UTF16FromString: %w", err)
	}
	// Copy into fixed-size buffer, leave room for null terminator.
	copy(t.data.szTip[:], tip)
	t.data.uFlags |= nifTip
	return nil
}

// Show adds the tray icon (NIM_ADD).
func (t *TrayIcon) Show() error {
	return t.shellNotify(nimAdd)
}

// Update modifies the existing tray icon (NIM_MODIFY).
func (t *TrayIcon) Update() error {
	return t.shellNotify(nimModify)
}

// Remove deletes the tray icon (NIM_DELETE).
func (t *TrayIcon) Remove() error {
	return t.shellNotify(nimDelete)
}

func (t *TrayIcon) shellNotify(op uint32) error {
	ret, _, err := procShellNotifyIconW.Call(
		uintptr(op),
		uintptr(unsafe.Pointer(&t.data)),
	)
	if ret == 0 {
		return fmt.Errorf("Shell_NotifyIconW(%d): %w", op, err)
	}
	return nil
}

// ---------------------------------------------------------------------------
// Icon loading.
// ---------------------------------------------------------------------------

// LoadIconFromBytes creates an HICON from raw .ico file bytes.
// It skips the 6-byte ICO header and 16-byte directory entry to reach the
// resource data (BITMAPINFOHEADER) that CreateIconFromResourceEx expects.
func LoadIconFromBytes(data []byte) (uintptr, error) {
	// ICO header: 2 reserved + 2 type + 2 count = 6 bytes.
	// Each directory entry is 16 bytes.
	// The directory entry at offset 6 contains the offset to image data
	// at bytes 12-15 (little-endian uint32).
	if len(data) < 22 { // 6-byte header + 16-byte directory minimum
		return 0, fmt.Errorf("ico data too short: %d bytes", len(data))
	}

	imageOffset := binary.LittleEndian.Uint32(data[18:22])
	imageSize := binary.LittleEndian.Uint32(data[14:18])

	if uint32(len(data)) < imageOffset+imageSize {
		return 0, fmt.Errorf("ico data truncated: need %d, have %d", imageOffset+imageSize, len(data))
	}

	resData := data[imageOffset:]

	hIcon, _, err := procCreateIconFromResourceEx.Call(
		uintptr(unsafe.Pointer(&resData[0])),
		uintptr(imageSize),
		1, // TRUE = icon (not cursor)
		0x00030000, // version 0x30000
		uintptr(iconDefaultSize),
		uintptr(iconDefaultSize),
		uintptr(lrDefaultColor),
	)
	if hIcon == 0 {
		return 0, fmt.Errorf("CreateIconFromResourceEx: %w", err)
	}
	return hIcon, nil
}

// ---------------------------------------------------------------------------
// Menu helpers.
// ---------------------------------------------------------------------------

// NewPopupMenu creates a new popup menu.
func NewPopupMenu() (uintptr, error) {
	hMenu, _, err := procCreatePopupMenu.Call()
	if hMenu == 0 {
		return 0, fmt.Errorf("CreatePopupMenu: %w", err)
	}
	return hMenu, nil
}

// AddMenuItem appends a text item to the menu.
func AddMenuItem(hMenu uintptr, id uint16, text string, enabled bool) error {
	textPtr, err := windows.UTF16PtrFromString(text)
	if err != nil {
		return fmt.Errorf("UTF16PtrFromString: %w", err)
	}

	state := uint32(mfsEnabled)
	if !enabled {
		state = mfsDisabled
	}

	mii := menuItemInfo{
		cbSize:     uint32(unsafe.Sizeof(menuItemInfo{})),
		fMask:      miimString | miimID | miimState | miimFType,
		fType:      mftString,
		fState:     state,
		wID:        uint32(id),
		dwTypeData: textPtr,
	}

	ret, _, callErr := procInsertMenuItemW.Call(
		hMenu,
		uintptr(0xFFFFFFFF), // append at end
		1,                   // fByPosition = TRUE
		uintptr(unsafe.Pointer(&mii)),
	)
	if ret == 0 {
		return fmt.Errorf("InsertMenuItemW: %w", callErr)
	}
	return nil
}

// AddSeparator appends a separator to the menu.
func AddSeparator(hMenu uintptr) error {
	mii := menuItemInfo{
		cbSize: uint32(unsafe.Sizeof(menuItemInfo{})),
		fMask:  miimFType,
		fType:  mftSeparator,
	}

	ret, _, err := procInsertMenuItemW.Call(
		hMenu,
		uintptr(0xFFFFFFFF), // append at end
		1,                   // fByPosition = TRUE
		uintptr(unsafe.Pointer(&mii)),
	)
	if ret == 0 {
		return fmt.Errorf("InsertMenuItemW (separator): %w", err)
	}
	return nil
}

// ShowMenu displays a popup menu at the cursor position and returns the
// selected item ID. Returns 0 if the user dismissed the menu without choosing.
func ShowMenu(hWnd, hMenu uintptr) uint16 {
	var pt point
	procGetCursorPos.Call(uintptr(unsafe.Pointer(&pt))) //nolint:errcheck

	// Win32 quirk: SetForegroundWindow must be called before TrackPopupMenu
	// so that the menu dismisses properly when the user clicks elsewhere.
	procSetForegroundWindow.Call(hWnd) //nolint:errcheck

	ret, _, _ := procTrackPopupMenuEx.Call(
		hMenu,
		uintptr(tpmReturncmd|tpmLeftAlign),
		uintptr(pt.x),
		uintptr(pt.y),
		hWnd,
		0, // no TPMPARAMS
	)
	return uint16(ret)
}

// DestroyMenuHandle destroys a menu.
func DestroyMenuHandle(hMenu uintptr) error {
	ret, _, err := procDestroyMenu.Call(hMenu)
	if ret == 0 {
		return fmt.Errorf("DestroyMenu: %w", err)
	}
	return nil
}

// ---------------------------------------------------------------------------
// Message window + message loop.
// ---------------------------------------------------------------------------

// CreateMessageWindow registers a window class and creates a message-only
// hidden window. The wndProc callback receives messages from Windows; return
// the result of DefWindowProcW for unhandled messages.
func CreateMessageWindow(className string, wndProc func(hwnd uintptr, msg uint32, wParam, lParam uintptr) uintptr) (uintptr, error) {
	classNamePtr, err := windows.UTF16PtrFromString(className)
	if err != nil {
		return 0, fmt.Errorf("UTF16PtrFromString: %w", err)
	}

	wc := wndClassEx{
		cbSize:      uint32(unsafe.Sizeof(wndClassEx{})),
		lpfnWndProc: windows.NewCallback(wndProc),
		lpszClassName: classNamePtr,
	}

	atom, _, regErr := procRegisterClassExW.Call(uintptr(unsafe.Pointer(&wc)))
	if atom == 0 {
		return 0, fmt.Errorf("RegisterClassExW: %w", regErr)
	}

	hwnd, _, createErr := procCreateWindowExW.Call(
		0,                           // dwExStyle
		uintptr(unsafe.Pointer(classNamePtr)), // lpClassName
		0,                           // lpWindowName (no title)
		0,                           // dwStyle
		0, 0, 0, 0,                  // x, y, w, h
		hwndMessage,                 // hWndParent = HWND_MESSAGE
		0,                           // hMenu
		0,                           // hInstance
		0,                           // lpParam
	)
	if hwnd == 0 {
		return 0, fmt.Errorf("CreateWindowExW: %w", createErr)
	}
	return hwnd, nil
}

// RunMessageLoop locks the current goroutine to its OS thread (required for
// Win32 message pumps) and blocks until WM_QUIT is received.
func RunMessageLoop() {
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()

	var m msg
	for {
		// GetMessageW returns 0 on WM_QUIT, -1 on error.
		ret, _, _ := procGetMessageW.Call(
			uintptr(unsafe.Pointer(&m)),
			0, // hWnd = NULL: all messages for this thread
			0, // wMsgFilterMin
			0, // wMsgFilterMax
		)
		// ret is BOOL but may be -1 (error); treat 0 and -1 as exit.
		if int32(ret) <= 0 {
			break
		}
		procTranslateMessage.Call(uintptr(unsafe.Pointer(&m)))
		procDispatchMessageW.Call(uintptr(unsafe.Pointer(&m)))
	}
}

// PostCustomMessage posts an application-defined message to the given window
// from any goroutine. This is safe to call from goroutines other than the
// message-loop thread.
func PostCustomMessage(hWnd uintptr, msgID uint32) error {
	ret, _, err := procPostMessageW.Call(hWnd, uintptr(msgID), 0, 0)
	if ret == 0 {
		return fmt.Errorf("PostMessageW: %w", err)
	}
	return nil
}

// DefWindowProc calls the default window procedure. Useful inside a wndProc
// callback for messages you don't handle.
func DefWindowProc(hwnd uintptr, msg uint32, wParam, lParam uintptr) uintptr {
	ret, _, _ := procDefWindowProcW.Call(hwnd, uintptr(msg), wParam, lParam)
	return ret
}

// PostQuit posts WM_QUIT to terminate the message loop.
func PostQuit() {
	procPostQuitMessage.Call(0)
}
