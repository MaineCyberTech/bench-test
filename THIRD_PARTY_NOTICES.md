# Third-party notices

This repository, and the app bundles built from it by `packaging/build_app.py`,
include or download the third-party components listed below. Their licences apply
to those components only; the rest of this project is covered by `LICENSE`.

## LibreHardwareMonitor

- **Component:** LibreHardwareMonitor (the `LibreHardwareMonitorLib.dll` sensor library)
- **Version:** v0.9.6
- **Upstream / source:** <https://github.com/LibreHardwareMonitor/LibreHardwareMonitor>
- **Pinned source for this version:** <https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/tree/v0.9.6>
- **Release asset:** <https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/download/v0.9.6/LibreHardwareMonitor.zip>
- **SHA-256 of `LibreHardwareMonitor.zip`:** `086d9f1b5a99e643edc2cfaaac16051685b551e4c5ac0b32a57c58c0e529c001`
- **Licence:** Mozilla Public License 2.0 (MPL-2.0) — <https://www.mozilla.org/en-US/MPL/2.0/>
- **How it is used:** `bench/Get-Sensors.ps1` and `bench/Sample-Sensors.ps1` load the
  library in-process to read CPU / GPU / motherboard / storage sensors on Windows
  machines that have no `nvidia-smi` (AMD/Intel GPUs, laptops, handhelds). The DLL is
  fetched at run time with the release and SHA-256 pinned above; a pre-vetted copy may
  instead be vendored under `tools/lhm/` (git-ignored) and is then shipped inside the
  app bundle when present (`packaging/build_app.py`).
- **Source availability:** MPL-2.0 requires that the source of the MPL-covered files be
  made available. The library is redistributed **unmodified**; its source is published
  upstream at the URL above (and at the pinned tag for this version).

## nvfancontrol

- **Component:** nvfancontrol (RTX Windows build)
- **Version:** 0.5.1
- **Upstream / source:** <https://github.com/foucault/nvfancontrol>
- **Pinned source for this version:** <https://github.com/foucault/nvfancontrol/tree/0.5.1>
- **Release asset:** <https://github.com/foucault/nvfancontrol/releases/download/0.5.1/nvfancontrol-rtx-win32-x64.zip>
- **SHA-256 of `nvfancontrol-rtx-win32-x64.zip`:** `c1d0b40fde6814e5c431d1062daa71192bf25ef47603c96c42d18a1342c24b2a`
- **Licence:** GNU General Public License v3.0 (GPL-3.0) — <https://www.gnu.org/licenses/gpl-3.0.html>
- **How it is used:** `bench/Install-GpuFanCurve.ps1` downloads it and registers a
  persistent logon task that drives a custom NVIDIA fan curve. It is fetched at run
  time and is **not** redistributed in the app bundle.
- **Source availability:** the source is published upstream at the URL above (and at the
  pinned tag for this version).
