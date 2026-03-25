//go:build windows

package tray

import _ "embed"

//go:embed assets/green.ico
var iconGreenData []byte

//go:embed assets/yellow.ico
var iconYellowData []byte

//go:embed assets/red.ico
var iconRedData []byte
