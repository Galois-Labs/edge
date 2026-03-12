#!/usr/bin/env bash
# Provision a Linux lab machine for galois-edge daemon.
# Run as root or with sudo.

set -euo pipefail

echo "=== Galois Edge — Linux Provisioning ==="

# 1. System dependencies
apt-get update && apt-get install -y \
    python3.10 python3.10-venv python3-pip \
    libusb-1.0-0-dev    # for pyusb (Ocean Optics, LabBrick, etc.)

# 2. udev rules for USB instruments
# Ocean Optics / SeaBreeze: the seabreeze package ships an OS setup command
if pip3 show seabreeze >/dev/null 2>&1; then
    seabreeze_os_setup
fi

# 3. Install daemon with all pip-installable extras
pip3 install galois-edge[gpib,usb,discovery,streaming,ocean-optics,ni-daq]

# 4. Vendor SDK instructions — these require manual action by the lab admin
cat <<'MSG'

=== Manual vendor SDK installation (install only what your lab needs) ===

LabBrick (Vaunix) synthesizers / attenuators:
  Instruments: LMS synthesizers, LSG signal generators, attenuators
  Download platform-specific binaries (.so / .dylib) from:
    https://vaunix.com/software/
  After download:
    sudo cp vnx_fsynth.so vnx_atten.so /usr/local/lib/
    sudo ldconfig
  Env var override: set LABBRICK_LIB_PATH=/path/to/dir before starting daemon.

AlazarTech digitizers (ATS9870, ATS9373, ATS9360, etc.):
  Instruments: AlazarTech digitizer boards
  Download ATS-SDK (Linux) from:
    https://www.alazartech.com/Support/Download%20Files/
  Install per vendor instructions (places libATSApi.so on system).
  After install: sudo ldconfig

NI-DAQmx — PCI/PCIe/PXI DAQs only (USB DAQs NOT supported on Linux):
  Instruments: NI DAQ cards (NI USB-6218 is WINDOWS ONLY — do not attempt)
  Add the NI package repository, then:
    sudo apt-get install ni-daqmx
  Repository and full instructions at:
    https://www.ni.com/en/support/downloads/drivers/download.ni-linux-device-drivers.html
  After install:
    pip3 install nidaqmx

Aeroflex / NI-RFSG (302x signal generators):
  pip3 install nirfsg
  Also install NI-RFSG runtime driver (Linux supported since 2023):
    https://www.ni.com/en/support/downloads/drivers/download.ni-rfsg.html

Aeroflex / NI-RFSA (303x signal analyzers):
  No Python package on PyPI yet. Use NI gRPC Device Server instead:
    https://github.com/ni/grpc-device/releases
  The daemon's aeroflex_wrapper connects to this server over local gRPC.
  Install and run the gRPC server on the machine with the PXI hardware.

MSG

echo "=== Done ==="
