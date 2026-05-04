#!/bin/sh
# galois-edge install script
#
# Usage:
#   curl -fsSL https://galoislabs.ai/install.sh | sudo sh
#   curl -fsSL https://galoislabs.ai/install.sh | sudo sh -s -- --token glc_XXXXX
#   curl -fsSL https://galoislabs.ai/install.sh | sudo sh -s -- --token glc_XXXXX --name lab-pi-01
#
# Environment variables:
#   GALOIS_VERSION    Install a specific version (default: latest)
#   GALOIS_BASE_URL   Override the download base URL
#
set -e

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Release artifacts are hosted on Cloudflare R2 (public bucket) since the
# GitHub repo is private and its releases are not publicly downloadable.
DEFAULT_BASE_URL="https://releases.galoislabs.ai"
INSTALL_DIR="/usr/local/bin"
CONFIG_DIR="/etc/galois-edge"
CONFIG_FILE="${CONFIG_DIR}/config.env"
UDEV_RULES_FILE="/etc/udev/rules.d/99-galois-edge.rules"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
fatal() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || fatal "Required command not found: $1"
}

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------

TOKEN=""
EDGE_NAME=""
BACKEND_URL=""
VERSION="${GALOIS_VERSION:-latest}"
BASE_URL="${GALOIS_BASE_URL:-${DEFAULT_BASE_URL}}"

while [ $# -gt 0 ]; do
    case "$1" in
        --token)    TOKEN="$2";       shift 2 ;;
        --name)     EDGE_NAME="$2";   shift 2 ;;
        --backend)  BACKEND_URL="$2"; shift 2 ;;
        --version)  VERSION="$2";     shift 2 ;;
        --help|-h)
            cat <<'USAGE'
galois-edge installer

OPTIONS:
  --token TOKEN     API key from the Galois dashboard (glc_...)
  --name NAME       Edge name (default: hostname)
  --backend URL     Backend URL (default: https://cloud.galoislabs.ai)
  --version VER     Version to install (default: latest)
  -h, --help        Show this help

ENVIRONMENT:
  GALOIS_VERSION    Same as --version
  GALOIS_BASE_URL   Override download base URL

EXAMPLES:
  # Install latest, register with token:
  curl -fsSL https://galoislabs.ai/install.sh | sudo sh -s -- --token glc_XXXXX

  # Install specific version:
  curl -fsSL https://galoislabs.ai/install.sh | sudo sh -s -- --version v1.2.0 --token glc_XXXXX
USAGE
            exit 0
            ;;
        *) warn "Unknown argument: $1"; shift ;;
    esac
done

# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------

# Must be root.
if [ "$(id -u)" -ne 0 ]; then
    fatal "This installer must be run as root. Try: curl -fsSL https://galoislabs.ai/install.sh | sudo sh"
fi

need_cmd curl
need_cmd sha256sum
need_cmd install
need_cmd uname

# ---------------------------------------------------------------------------
# Detect architecture
# ---------------------------------------------------------------------------

ARCH="$(uname -m)"
case "${ARCH}" in
    x86_64)         ARCH="amd64" ;;
    aarch64|arm64)  ARCH="arm64" ;;
    armv7l|armv7*)  ARCH="armv7" ;;
    *)              fatal "Unsupported architecture: ${ARCH}" ;;
esac

OS="$(uname -s)"
case "${OS}" in
    Linux)  OS="linux" ;;
    *)      fatal "Unsupported OS: ${OS}. This installer is for Linux only." ;;
esac

info "Detected platform: ${OS}/${ARCH}"

# ---------------------------------------------------------------------------
# Detect Raspberry Pi (used later to suggest the pi-setup helper)
# ---------------------------------------------------------------------------

IS_RASPBERRY_PI=0
if [ -r /proc/device-tree/model ]; then
    # device-tree strings are NUL-terminated; tr strips the NUL.
    PI_MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)"
    case "${PI_MODEL}" in
        *Raspberry\ Pi*) IS_RASPBERRY_PI=1 ;;
    esac
fi
[ "${IS_RASPBERRY_PI}" = 1 ] && info "Raspberry Pi detected: ${PI_MODEL}"

# ---------------------------------------------------------------------------
# Resolve version
# ---------------------------------------------------------------------------

if [ "${VERSION}" = "latest" ]; then
    info "Resolving latest version..."
    VERSION="$(curl -fsSL "${BASE_URL}/latest" 2>/dev/null)" \
        || fatal "Could not determine latest version. Set --version explicitly."
    [ -n "${VERSION}" ] || fatal "Empty version string from ${BASE_URL}/latest"
fi

info "Installing galois-edge ${VERSION}"

# ---------------------------------------------------------------------------
# Check if already installed at this version
# ---------------------------------------------------------------------------

if command -v galois-edge >/dev/null 2>&1; then
    CURRENT="$(galois-edge version 2>/dev/null || echo unknown)"
    if echo "${CURRENT}" | grep -q "${VERSION}"; then
        info "galois-edge ${VERSION} is already installed."
        # Still run setup if token provided.
        if [ -n "${TOKEN}" ]; then
            info "Running setup with provided token..."
            SETUP_ARGS="--config ${CONFIG_FILE}"
            [ -n "${EDGE_NAME}" ]   && SETUP_ARGS="${SETUP_ARGS} --name ${EDGE_NAME}"
            [ -n "${BACKEND_URL}" ] && SETUP_ARGS="${SETUP_ARGS} --backend ${BACKEND_URL}"
            galois-edge setup "${TOKEN}" ${SETUP_ARGS}
        fi
        exit 0
    fi
    info "Upgrading from ${CURRENT} to ${VERSION}"
fi

# ---------------------------------------------------------------------------
# Download binaries
# ---------------------------------------------------------------------------

TMPDIR="$(mktemp -d)"
trap 'rm -rf "${TMPDIR}"' EXIT

GO_BIN="galois-edge-${OS}-${ARCH}"
PY_BIN="galois-edge-daemon-${OS}-${ARCH}"
CHECKSUMS="checksums-${OS}-${ARCH}.sha256"

download() {
    local url="${BASE_URL}/${VERSION}/$1"
    local dest="${TMPDIR}/$1"
    info "Downloading $1..."
    curl -fsSL -o "${dest}" "${url}" \
        || fatal "Download failed: ${url}"
}

download "${GO_BIN}"
download "${PY_BIN}"
download "${CHECKSUMS}"

# ---------------------------------------------------------------------------
# Verify checksums
# ---------------------------------------------------------------------------

info "Verifying checksums..."
(cd "${TMPDIR}" && sha256sum -c "${CHECKSUMS}" --quiet) \
    || fatal "Checksum verification failed. Aborting."

# ---------------------------------------------------------------------------
# Stop existing service (if running)
# ---------------------------------------------------------------------------

if systemctl is-active --quiet galois-edge 2>/dev/null; then
    info "Stopping existing galois-edge service..."
    systemctl stop galois-edge
fi

# ---------------------------------------------------------------------------
# Install binaries
# ---------------------------------------------------------------------------

info "Installing binaries to ${INSTALL_DIR}..."
install -m 755 "${TMPDIR}/${GO_BIN}" "${INSTALL_DIR}/galois-edge"
install -m 755 "${TMPDIR}/${PY_BIN}" "${INSTALL_DIR}/galois-edge-daemon"

# Verify they execute.
"${INSTALL_DIR}/galois-edge" version >/dev/null 2>&1 \
    || fatal "Installed binary failed to execute. Architecture mismatch?"

# ---------------------------------------------------------------------------
# Create config directory
# ---------------------------------------------------------------------------

mkdir -p "${CONFIG_DIR}"
if [ ! -f "${CONFIG_FILE}" ]; then
    echo "# galois-edge configuration — managed by installer" > "${CONFIG_FILE}"
    chmod 640 "${CONFIG_FILE}"
fi

# ---------------------------------------------------------------------------
# Install udev rules for instrument access
# ---------------------------------------------------------------------------

info "Installing udev rules..."
cat > "${UDEV_RULES_FILE}" <<'UDEV'
# galois-edge — instrument device permissions
# Installed by https://galoislabs.ai/install.sh
# Reload with: sudo udevadm control --reload-rules && sudo udevadm trigger

# USBTMC (USB Test & Measurement Class) — oscilloscopes, multimeters, etc.
SUBSYSTEM=="usb", ATTR{bInterfaceClass}=="fe", ATTR{bInterfaceSubClass}=="03", MODE="0660", GROUP="plugdev"

# Keysight/Agilent USBTMC devices (vendor 0x0957)
SUBSYSTEM=="usb", ATTR{idVendor}=="0957", MODE="0660", GROUP="plugdev"

# Tektronix USBTMC devices (vendor 0x0699)
SUBSYSTEM=="usb", ATTR{idVendor}=="0699", MODE="0660", GROUP="plugdev"

# Rohde & Schwarz USBTMC devices (vendor 0x0aad)
SUBSYSTEM=="usb", ATTR{idVendor}=="0aad", MODE="0660", GROUP="plugdev"

# National Instruments USB (vendor 0x3923)
SUBSYSTEM=="usb", ATTR{idVendor}=="3923", MODE="0660", GROUP="plugdev"

# Rigol USBTMC devices (vendor 0x1ab1)
SUBSYSTEM=="usb", ATTR{idVendor}=="1ab1", MODE="0660", GROUP="plugdev"

# Siglent USBTMC devices (vendor 0xf4ec)
SUBSYSTEM=="usb", ATTR{idVendor}=="f4ec", MODE="0660", GROUP="plugdev"

# USBTMC character devices
KERNEL=="usbtmc[0-9]*", MODE="0660", GROUP="plugdev"

# Serial adapters (FTDI, Prolific, CH340, CP210x) — already handled by dialout
# group on most distros, but ensure consistency.
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", MODE="0660", GROUP="dialout"
SUBSYSTEM=="tty", ATTRS{idVendor}=="067b", MODE="0660", GROUP="dialout"
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", MODE="0660", GROUP="dialout"
SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", MODE="0660", GROUP="dialout"
UDEV

udevadm control --reload-rules 2>/dev/null || true
udevadm trigger 2>/dev/null || true

# ---------------------------------------------------------------------------
# Register with backend (if token provided)
# ---------------------------------------------------------------------------

if [ -n "${TOKEN}" ]; then
    info "Registering edge with cloud backend..."
    SETUP_ARGS="--config ${CONFIG_FILE}"
    [ -n "${EDGE_NAME}" ]   && SETUP_ARGS="${SETUP_ARGS} --name ${EDGE_NAME}"
    [ -n "${BACKEND_URL}" ] && SETUP_ARGS="${SETUP_ARGS} --backend ${BACKEND_URL}"
    galois-edge setup "${TOKEN}" ${SETUP_ARGS}
else
    warn "No --token provided. You can register later with:"
    warn "  galois-edge setup <TOKEN> --config ${CONFIG_FILE}"
fi

# ---------------------------------------------------------------------------
# Install systemd service
# ---------------------------------------------------------------------------

info "Installing systemd service..."
galois-edge install --config "${CONFIG_FILE}"

# ---------------------------------------------------------------------------
# Start the service
# ---------------------------------------------------------------------------

info "Starting galois-edge..."
systemctl start galois-edge

# ---------------------------------------------------------------------------
# Install uninstall helper
# ---------------------------------------------------------------------------

cat > "${INSTALL_DIR}/galois-edge-uninstall" <<'UNINSTALL'
#!/bin/sh
set -e
if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root: sudo galois-edge-uninstall" >&2
    exit 1
fi
echo "Stopping galois-edge..."
systemctl stop galois-edge 2>/dev/null || true
galois-edge uninstall 2>/dev/null || true
echo "Removing binaries..."
rm -f /usr/local/bin/galois-edge
rm -f /usr/local/bin/galois-edge-daemon
rm -f /usr/local/bin/galois-edge-uninstall
echo "Removing udev rules..."
rm -f /etc/udev/rules.d/99-galois-edge.rules
udevadm control --reload-rules 2>/dev/null || true
echo ""
echo "galois-edge has been uninstalled."
echo "Config preserved at /etc/galois-edge/ — remove manually if desired."
UNINSTALL
chmod 755 "${INSTALL_DIR}/galois-edge-uninstall"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
info "galois-edge installed successfully!"
echo ""
echo "  Binaries:   ${INSTALL_DIR}/galois-edge"
echo "              ${INSTALL_DIR}/galois-edge-daemon"
echo "  Config:     ${CONFIG_FILE}"
echo "  Service:    systemctl status galois-edge"
echo "  Logs:       journalctl -u galois-edge -f"
echo "  Uninstall:  sudo galois-edge-uninstall"
echo ""

# Check if the service is actually running.
if systemctl is-active --quiet galois-edge 2>/dev/null; then
    info "Service is running."
else
    warn "Service may not have started. Check: journalctl -u galois-edge --no-pager -n 20"
fi

# ---------------------------------------------------------------------------
# Raspberry Pi follow-up: serial UART configuration
# ---------------------------------------------------------------------------
#
# On Raspberry Pi the GPIO UART (/dev/serial0) is used for serial-instrument
# support but ships with a login getty attached, Bluetooth bound to the PL011,
# and the daemon user not in the dialout group. The galois-edge pi-setup
# subcommand fixes all three. We do not run it automatically because it edits
# /boot/firmware/cmdline.txt and config.txt and requires a reboot — instead,
# point the operator at the command.

if [ "${IS_RASPBERRY_PI}" = 1 ]; then
    echo ""
    info "Raspberry Pi detected — to enable serial-instrument access on the GPIO UART:"
    echo "    sudo galois-edge pi-setup            # interactive"
    echo "    sudo galois-edge pi-setup --dry-run  # preview only"
    echo "    sudo galois-edge pi-setup --yes      # apply without prompting"
    echo ""
    echo "  This disables the login console on /dev/ttyAMA0, frees the PL011 from"
    echo "  Bluetooth, and adds your user to the dialout group. A reboot is required"
    echo "  for the cmdline.txt / config.txt changes to take effect."
fi
