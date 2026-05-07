# Windows MSI — Manual QA Test Plan

Automated MSI testing requires a Windows VM and cannot run in the standard macOS/Linux
CI environment. This document records the six manual test cases that must be executed
before any Windows MSI release is marked "production ready."

Each test case should be run on both supported OS targets unless noted otherwise:
- Windows 10 21H2 (build 19044)
- Windows 11 23H2

Run all `msiexec` commands from an elevated (Administrator) command prompt.
Record the result (PASS / FAIL), the tester name, and the date in the table at the
bottom of this document before closing the QA pass.

---

## Test 1 — Fresh install on a clean machine

**Objective:** Verify the complete install sequence on a machine with no prior
galois-edge installation.

**Pre-conditions:**
- No `galois-edge` service registered (`sc query galois-edge` returns error 1060).
- No `C:\Program Files\galois-edge\` directory.
- No `C:\ProgramData\galois-edge\` directory.

**Steps:**
1. Copy `galois-edge-windows-amd64.msi` to the test machine.
2. Run: `msiexec /i galois-edge-windows-amd64.msi /qn /l*v install.log`
3. Wait for exit code 0.

**Verification:**
- `dir "C:\Program Files\galois-edge\"` — `galois-edge.exe`, `galois-edge-daemon.exe`,
  `galois-edge-tray.exe` all present.
- `dir "C:\ProgramData\galois-edge\"` — `config.env` present.
- `sc query galois-edge` — reports `STATE: 4 RUNNING`.
- Start Menu — "Galois Edge" folder contains "Galois Edge Status" shortcut.
- `reg query "HKLM\Software\Microsoft\Windows\CurrentVersion\Run" /v GaloisEdgeTray`
  — value present, pointing at `C:\Program Files\galois-edge\galois-edge-tray.exe`.
- Open a new Command Prompt: `galois-edge status` — runs without "not found" error
  (PATH entry active; new shell session required after install).

**Expected result:** All verification steps pass, exit code 0, install.log shows no
errors.

---

## Test 2 — Install over a running service (same-version reinstall)

**Objective:** Verify `AllowSameVersionUpgrades="yes"` allows re-installing the same
MSI version, that the installer gracefully stops the service before replacing files,
and that no "file in use" error (MSI error 1618 / ERROR_INSTALL_ALREADY_RUNNING) occurs.

**Pre-conditions:**
- galois-edge already installed at the same version as the MSI under test.
- `sc query galois-edge` — reports `STATE: 4 RUNNING`.

**Steps:**
1. Run: `msiexec /i galois-edge-windows-amd64.msi /qn /l*v reinstall.log`
2. Wait for exit code 0.

**Verification:**
- reinstall.log contains no 1618 error.
- `sc query galois-edge` — reports `STATE: 4 RUNNING` after reinstall.
- Binaries in `C:\Program Files\galois-edge\` have current timestamps.
- `config.env` content is unchanged (NeverOverwrite in effect).

**Expected result:** Reinstall completes without error, service is running, config
preserved.

---

## Test 3 — Upgrade from an older version

**Objective:** Verify the MajorUpgrade path: old version is cleanly removed before
new files land, and `config.env` is preserved across the upgrade.

**Pre-conditions:**
- galois-edge v0.1.0 (or any earlier version) installed and running.
- `config.env` has been edited to include a custom key, e.g.:
  `echo "MY_CUSTOM_KEY=sentinel_value" >> C:\ProgramData\galois-edge\config.env`

**Steps:**
1. Run: `msiexec /i galois-edge-windows-amd64.msi /qn /l*v upgrade.log`
   (where the MSI is a newer version than the installed one)
2. Wait for exit code 0.

**Verification:**
- `findstr /i "sentinel_value" "C:\ProgramData\galois-edge\config.env"` — returns
  the custom line (config preserved).
- `sc query galois-edge` — reports `STATE: 4 RUNNING`.
- Add/Remove Programs (appwiz.cpl) — shows only the new version, not both versions.
- `galois-edge --version` — prints the new version number.
- upgrade.log shows no errors and shows the MajorUpgrade sequence
  (UninstallService before new files, InstallService after).

**Expected result:** Custom config key survives; only new version appears in ARP;
service running on new binary.

---

## Test 4 — Uninstall preserves config

**Objective:** Verify that `Permanent="yes"` keeps `config.env` in place after
uninstall, mirroring the Linux uninstaller behaviour.

**Pre-conditions:**
- galois-edge installed and running.
- `config.env` exists at `C:\ProgramData\galois-edge\config.env`.

**Steps:**
1. Note the product code from the registry or from `msiexec /x` with the MSI file:
   `msiexec /x galois-edge-windows-amd64.msi /qn /l*v uninstall.log`
2. Wait for exit code 0.

**Verification:**
- `dir "C:\Program Files\galois-edge\"` — directory is gone (or empty).
- `dir "C:\ProgramData\galois-edge\"` — directory exists, `config.env` is present.
- `sc query galois-edge` — returns error 1060 ("The specified service does not exist
  as an installed service") — service completely removed from SCM.
- Start Menu — "Galois Edge" folder and "Galois Edge Status" shortcut are gone.
- `reg query "HKLM\Software\Microsoft\Windows\CurrentVersion\Run" /v GaloisEdgeTray`
  — returns error (value removed).
- PATH — `galois-edge` command no longer found in a new shell session (PATH entry
  removed).

**Expected result:** Binary directory gone, service gone, shortcuts gone; config
directory and config.env remain.

---

## Test 5 — Install with GALOIS_TOKEN property

**Objective:** Verify the `SetupToken` custom action fires when `GALOIS_TOKEN` is
passed, and that the token is recorded in config.env or acknowledged by the binary.

**Pre-conditions:**
- No prior galois-edge installation (clean machine, or after Test 4).

**Steps:**
1. Run:
   ```
   msiexec /i galois-edge-windows-amd64.msi GALOIS_TOKEN=glc_test123 /qn /l*v token.log
   ```
2. Wait for exit code 0.

**Verification:**
- token.log — search for `SetupToken` action; it should appear and complete without
  error.
- `type "C:\ProgramData\galois-edge\config.env"` — verify that the token has been
  recorded (e.g., a `REGISTRATION_TOKEN=` or `GALOIS_TOKEN=` line), OR confirm that
  `galois-edge setup` ran successfully by checking its own log output or the Windows
  Event Log under Application source `galois-edge`.
- `sc query galois-edge` — service running (InstallService also succeeded).

**Expected result:** SetupToken action runs, token stored, service running.

---

## Test 6 — Compatibility matrix

**Objective:** Verify the installer runs without compatibility errors on both
supported Windows versions, and that SmartScreen does not block a signed MSI.

**Pre-conditions:**
- Signed MSI (`SIGNING_TENANT_ID` secrets configured, tag-push build).
- One Windows 10 21H2 (build 19044) VM and one Windows 11 23H2 VM, both clean.

**Steps (repeat on each OS):**
1. Download `galois-edge-windows-amd64.msi` from the release artifacts.
2. Double-click the MSI (do NOT use `/qn` — the goal is to observe UI behaviour).
3. If SmartScreen appears, record the publisher name shown and whether the warning
   is a hard block ("Windows protected your PC — an app was blocked") or a soft
   warning ("Windows protected your PC — Unknown publisher"). An EV cert should
   produce no SmartScreen block at all.
4. Complete the install through the UI wizard.
5. Run: `galois-edge status`

**Verification:**
- No compatibility shim errors or "This app can't run on your PC" dialogs.
- SmartScreen does NOT block (EV cert expected to carry immediate reputation).
  If a warning appears, note the exact text — it may indicate the cert is not yet
  trusted or is OV, not EV.
- `sc query galois-edge` — `STATE: 4 RUNNING`.
- Windows Event Log (eventvwr.msc) — Application log shows service start event
  from source `galois-edge`.

**Expected result (per OS):**
- Windows 10 21H2: install completes, no compatibility errors, no SmartScreen block.
- Windows 11 23H2: install completes, no compatibility errors, no SmartScreen block.

---

## QA Sign-off

| Test | Windows 10 21H2 | Windows 11 23H2 | Tester | Date |
|---|---|---|---|---|
| 1 — Fresh install | | | | |
| 2 — Install over running service | | | | |
| 3 — Upgrade from older version | | | | |
| 4 — Uninstall preserves config | | | | |
| 5 — Install with GALOIS_TOKEN | | | | |
| 6 — Compatibility matrix | N/A (OS-specific) | N/A (OS-specific) | | |

All six test cases must be marked PASS before the MSI is published to
`s3://galois-edge-releases/<version>/galois-edge-windows-amd64.msi`.
