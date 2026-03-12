# Provision a Windows lab machine for galois-edge daemon.
# Run as Administrator in a PowerShell prompt.

Write-Host "=== Galois Edge — Windows Provisioning ===" -ForegroundColor Green

# 1. Install daemon with pip-installable extras
pip install galois-edge[usb,discovery,streaming,ocean-optics,ni-daq]

# 2. Vendor SDK instructions — install only what your lab needs
Write-Host @"

=== Manual vendor SDK installation (install only what your lab needs) ===

Keysight PXI AWG / Digitizer / HVI Trigger (M3201A, M3202A, M3100A, M3102A):
  Also covers Signadyne AWG and Digitizer (acquired by Keysight, same SDK).
  This SDK is Windows-only and NOT available on PyPI.
  Download and run the Keysight SD1 Software installer:
    https://www.keysight.com/sd1
  The installer places keysightSD1 in the Python path automatically.
  Requires PXI chassis with physical cards installed.

NI-DAQmx (all device types including USB — NI USB-6218 is Windows-only):
  Download and run the NI-DAQmx runtime installer:
    https://www.ni.com/en/support/downloads/drivers/download.ni-daqmx.html
  After install:
    pip install nidaqmx

LabBrick (Vaunix) synthesizers / attenuators:
  Instruments: LMS synthesizers, LSG signal generators, attenuators
  Download DLLs from:
    https://vaunix.com/software/
  Place vnx_fsynth.dll and vnx_atten.dll on the system PATH, e.g.:
    C:\Windows\System32\
  Or set env var LABBRICK_LIB_PATH to the directory containing the DLLs.

AlazarTech digitizers (ATS9870, ATS9373, ATS9360, etc.):
  Download ATS-SDK (Windows) from:
    https://www.alazartech.com/Support/Download%20Files/
  Install to the default location — the installer adds ATSApi.dll to PATH.

Aeroflex / NI-RFSG (302x signal generators):
  pip install nirfsg
  Also install NI-RFSG runtime driver from:
    https://www.ni.com/en/support/downloads/drivers/download.ni-rfsg.html

Aeroflex / NI-RFSA (303x signal analyzers):
  No Python package on PyPI. Use NI gRPC Device Server:
    https://github.com/ni/grpc-device/releases
  The daemon's aeroflex_wrapper connects to this server over local gRPC.
  Install and start the gRPC server on the machine with the PXI hardware.

SignalHound SA124B spectrum analyzer:
  No PyPI package. Download bb_api.dll from:
    https://signalhound.com/support/
  Place bb_api.dll on the system PATH (e.g., same directory as daemon, or
  C:\Windows\System32\).

QD PPMS DynaCool (Quantum Design):
  pip install MultiPyVu
  The MultiVu GUI application must be running on this machine.
  The daemon connects to it as a local client on port 5000.

"@ -ForegroundColor Yellow

Write-Host "=== Done ===" -ForegroundColor Green
