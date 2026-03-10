package cli

import (
	"context"
	"fmt"
	"net"
	"os"
	"time"

	"github.com/galois-labs/edge/internal/grpcclient"
	"github.com/spf13/cobra"
)

// statusCmd implements "galois-edge status".
var statusCmd = &cobra.Command{
	Use:   "status",
	Short: "Query the daemon health and instrument summary",
	Long: `Status checks whether the Python instrument engine is reachable on its
internal gRPC port and, if so, queries the current instrument list.`,
	Run: runStatus,
}

func init() {
	statusCmd.Flags().String("config", "", "path to config.env (for port settings)")
}

func runStatus(cmd *cobra.Command, args []string) {
	cfgPath, _ := cmd.Flags().GetString("config")
	cfg, _, err := loadConfig(cfgPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}

	grpcAddr := fmt.Sprintf("127.0.0.1:%d", cfg.GRPCInternalPort)

	// Step 1: TCP probe to check if the Python engine is listening.
	conn, err := net.DialTimeout("tcp", grpcAddr, 3*time.Second)
	if err != nil {
		fmt.Printf("Daemon status: NOT RUNNING\n")
		fmt.Printf("  Cannot reach Python gRPC at %s\n", grpcAddr)
		os.Exit(1)
	}
	conn.Close()
	fmt.Printf("Daemon status: RUNNING\n")
	fmt.Printf("  Python gRPC: %s\n", grpcAddr)

	// Step 2: Query instruments via gRPC.
	gc, err := grpcclient.New(grpcAddr)
	if err != nil {
		fmt.Printf("  Instruments: (could not connect: %v)\n", err)
		return
	}
	defer gc.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	instruments, err := gc.GetInstruments(ctx)
	if err != nil {
		fmt.Printf("  Instruments: (query failed: %v)\n", err)
		return
	}

	fmt.Printf("  Instruments: %d found\n", len(instruments))
	for _, inst := range instruments {
		status := "connected"
		if !inst.GetIsConnected() {
			status = "disconnected"
		}
		fmt.Printf("    - %s %s (%s) [%s]\n",
			inst.GetManufacturer(),
			inst.GetModel(),
			inst.GetAddress(),
			status,
		)
	}
}
