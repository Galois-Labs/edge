// Command galois-edge is the entry point for the galois-edge daemon.
// When launched by the Windows Service Control Manager it delegates to the
// service handler; otherwise it runs the Cobra CLI.
package main

import (
	"log"
	"os"

	"github.com/galois-labs/edge/internal/cli"
	"github.com/galois-labs/edge/internal/service"
)

func main() {
	// When running as a Windows service, the SCM calls us without CLI args.
	// Detect this case and hand off to the service handler.
	if service.IsWindowsService() {
		err := service.RunAsService(
			func() error {
				// Simulate "galois-edge start" by invoking the CLI.
				os.Args = []string{os.Args[0], "start"}
				cli.Execute()
				return nil
			},
			func() {
				// Send SIGINT to ourselves to trigger the graceful shutdown
				// path in the start command's signal.NotifyContext.
				p, err := os.FindProcess(os.Getpid())
				if err == nil {
					_ = p.Signal(os.Interrupt)
				}
			},
		)
		if err != nil {
			log.Fatalf("service failed: %v", err)
		}
		return
	}

	cli.Execute()
}
