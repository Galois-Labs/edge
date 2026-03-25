//go:build windows

package main

import (
	"context"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"

	"github.com/galois-labs/edge/internal/tray"
)

// version is injected at build time via -ldflags.
var version = "dev"

func main() {
	// Log to file since -H windowsgui has no console.
	logPath := filepath.Join(os.Getenv("ProgramData"), "galois-edge", "tray.log")
	if f, err := os.OpenFile(logPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0644); err == nil {
		log.SetOutput(f)
		defer f.Close()
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	app, err := tray.NewApp(version)
	if err != nil {
		log.Fatalf("galois-edge-tray: %v", err)
	}
	if err := app.Run(ctx); err != nil {
		log.Fatalf("galois-edge-tray: %v", err)
	}
}
