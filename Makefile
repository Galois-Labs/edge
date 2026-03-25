# galois-edge daemon — build targets
#
# Prerequisites:
#   Go 1.23+          (go build)
#   Python 3.10+      (pip, pytest)
#   buf CLI            (proto generation)
#   PyInstaller        (freeze target)

VERSION    ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
GIT_COMMIT ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)
BUILD_DATE ?= $(shell date -u +%Y-%m-%dT%H:%M:%SZ)
LDFLAGS     = -X github.com/galois-labs/edge/internal/cli.Version=$(VERSION) \
              -X github.com/galois-labs/edge/internal/cli.GitCommit=$(GIT_COMMIT) \
              -X github.com/galois-labs/edge/internal/cli.BuildDate=$(BUILD_DATE)

GO         ?= go
PYTHON     ?= python3
PIP        ?= pip3
BUF        ?= buf
PYTEST     ?= $(PYTHON) -m pytest
PYINSTALLER?= pyinstaller

BIN_DIR    := bin
PROTO_DIR  := proto

.PHONY: all proto build-go build-python test test-go test-python clean install \
        freeze lint help build-tray

# -----------------------------------------------------------------------
# Default
# -----------------------------------------------------------------------
all: build-go build-python

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*## "}; {printf "  %-20s %s\n", $$1, $$2}'

# -----------------------------------------------------------------------
# Proto generation
# -----------------------------------------------------------------------
proto: ## Regenerate Go + Python protobuf/gRPC stubs
	cd $(PROTO_DIR) && $(BUF) generate
	@echo "--- copying Python stubs into src/galois_edge/ ---"
	cp $(PROTO_DIR)/gen/python/edge/v1/edge_pb2.py     src/galois_edge/edge_pb2.py
	cp $(PROTO_DIR)/gen/python/edge/v1/edge_pb2_grpc.py src/galois_edge/edge_pb2_grpc.py

# -----------------------------------------------------------------------
# Go build
# -----------------------------------------------------------------------
build-go: ## Build Go supervisor binary
	$(GO) build -ldflags "$(LDFLAGS)" -o $(BIN_DIR)/galois-edge ./cmd/galois-edge

# -----------------------------------------------------------------------
# Python build (editable install for development)
# -----------------------------------------------------------------------
build-python: ## Install Python package in editable mode
	$(PIP) install -e ".[dev]"

# -----------------------------------------------------------------------
# Freeze Python into a standalone binary via PyInstaller
# -----------------------------------------------------------------------
freeze: ## Freeze Python engine via PyInstaller (uses .spec for full hidden imports)
	$(PYINSTALLER) galois-edge-daemon.spec
	@echo "Frozen binary: dist/galois-edge-daemon"

# -----------------------------------------------------------------------
# Test
# -----------------------------------------------------------------------
test: test-go test-python ## Run all tests

test-go: ## Run Go tests
	$(GO) test -race ./...

test-python: ## Run Python tests
	$(PYTEST) tests/ -x -v

# -----------------------------------------------------------------------
# Lint
# -----------------------------------------------------------------------
lint: ## Lint proto files with buf
	cd $(PROTO_DIR) && $(BUF) lint

# -----------------------------------------------------------------------
# Install (Linux: systemd service)
# -----------------------------------------------------------------------
install: build-go freeze ## Build + install as system service (Linux)
	@echo "--- Installing galois-edge ---"
	install -D -m 755 $(BIN_DIR)/galois-edge /usr/local/bin/galois-edge
	install -D -m 755 dist/galois-edge-daemon /usr/local/bin/galois-edge-daemon
	mkdir -p /etc/galois-edge
	@if [ ! -f /etc/galois-edge/config.env ]; then \
		echo "# galois-edge configuration" > /etc/galois-edge/config.env; \
		echo "Created /etc/galois-edge/config.env (edit as needed)"; \
	fi
	/usr/local/bin/galois-edge install
	@echo "--- Done. Run: systemctl start galois-edge ---"

# -----------------------------------------------------------------------
# Cross-compilation targets
# -----------------------------------------------------------------------
build-linux-amd64: ## Cross-compile Go for linux/amd64
	GOOS=linux GOARCH=amd64 $(GO) build -ldflags "$(LDFLAGS)" -o $(BIN_DIR)/galois-edge-linux-amd64 ./cmd/galois-edge

build-linux-arm64: ## Cross-compile Go for linux/arm64
	GOOS=linux GOARCH=arm64 $(GO) build -ldflags "$(LDFLAGS)" -o $(BIN_DIR)/galois-edge-linux-arm64 ./cmd/galois-edge

build-windows-amd64: ## Cross-compile Go for windows/amd64
	GOOS=windows GOARCH=amd64 $(GO) build -ldflags "$(LDFLAGS)" -o $(BIN_DIR)/galois-edge-windows-amd64.exe ./cmd/galois-edge

build-tray: ## Build Windows tray application (no console window)
	GOOS=windows GOARCH=amd64 $(GO) build \
		-ldflags "$(LDFLAGS) -H windowsgui" \
		-o $(BIN_DIR)/galois-edge-tray.exe ./cmd/galois-edge-tray

# -----------------------------------------------------------------------
# Clean
# -----------------------------------------------------------------------
clean: ## Remove build artefacts
	rm -rf $(BIN_DIR)/ dist/ build/ *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
