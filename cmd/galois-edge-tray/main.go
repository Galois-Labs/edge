//go:build windows

package main

import (
	"context"
	"log"
	"os/signal"
	"syscall"

	"github.com/galois-labs/edge/internal/tray"
)

// version is injected at build time via -ldflags.
var version = "dev"

func main() {
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
