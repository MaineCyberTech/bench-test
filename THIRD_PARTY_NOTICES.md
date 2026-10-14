# Third-party notices

bench-test bundles and/or downloads the third-party components below. Each is
the property of its respective authors and is used under the license shown.
The machine-readable inventory is emitted by `packaging/sbom.py`; the policy
that governs these entries is in [`docs/DEPENDENCIES.md`](docs/DEPENDENCIES.md).

---

## LibreHardwareMonitor

- **Component:** `LibreHardwareMonitorLib.dll` (part of LibreHardwareMonitor)
- **Version:** tracks the upstream `releases/latest` (see `bench/Get-Sensors.ps1`)
- **License:** Mozilla Public License 2.0 (MPL-2.0)
- **Source:** <https://github.com/LibreHardwareMonitor/LibreHardwareMonitor>
- **Used by:** `bench/Get-Sensors.ps1`, `bench/Sample-Sensors.ps1` — supplies
  CPU/GPU temperature, power, clocks, fan RPM, VRAM and load telemetry on
  AMD/Intel Windows, where `nvidia-smi` does not exist.
- **How obtained:** downloaded at run time on first use; shipped in the app
  bundle under `tools/lhm` when present.
- **Notice:** This Source Code Form is subject to the terms of the Mozilla Public
  License, v. 2.0. A copy of the MPL is available at
  <https://mozilla.org/MPL/2.0/>. Source is available at the project link above.

---

## nvfancontrol

- **Component:** `nvfancontrol.exe` (RTX build)
- **Version:** 0.5.1 (pinned release tag)
- **License:** GNU General Public License v3.0 or later (GPL-3.0-or-later)
- **Source:** <https://github.com/foucault/nvfancontrol>
- **Used by:** `bench/Install-GpuFanCurve.ps1` — installs a custom NVIDIA GPU fan
  curve as an elevated logon task (separate process; not linked into the
  toolkit).
- **How obtained:** downloaded at run time from the pinned GitHub release
  (`nvfancontrol-rtx-win32-x64.zip`); **not redistributed** with bench-test.
- **Notice:** This program is free software: you can redistribute it and/or
  modify it under the terms of the GNU General Public License as published by the
  Free Software Foundation, either version 3 of the License, or (at your option)
  any later version. A copy of the GPL is available at
  <https://www.gnu.org/licenses/gpl-3.0.html>.
