# Dependencies & artifact provenance

This document records what the shipped artifacts depend on, how the vendored
binaries are reproduced, and what is **not** yet pinned. It exists so the
provenance gaps are stated precisely instead of being implicit in the build
scripts (audit findings **SBOM-P3-001**, **SC-P3-001**, **EVOL-P3-002**).

## 1. Vendored binaries (app bundles)

The toolkit has one vendored binary dependency:

| Component | Where | How it is obtained |
|---|---|---|
| LibreHardwareMonitor sensor library (`LibreHardwareMonitorLib.dll` + managed deps) | `tools/lhm/` (git-ignored, shipped in the PyInstaller bundles) | `tools/fetch-lhm.py` — pinned release + SHA-256 verified |

`tools/lhm` is **not committed** (it is a ~6 MB third-party zip of Windows DLLs).
`tools/fetch-lhm.py` is committed and reproduces it: it downloads the pinned
release (`LHM_VERSION`), verifies the asset's SHA-256 against the committed
constant (`LHM_SHA256`, **fail closed**), and extracts the library and its
managed dependencies. `packaging/build_app.py` runs it automatically when
`tools/lhm` is missing and warns loudly if the library still cannot be restored,
so a fresh clone / CI no longer builds a bundle without sensor telemetry
(EVOL-P3-002).

```bash
python tools/fetch-lhm.py        # -> tools/lhm
```

The runtime PowerShell fallback (`bench/Sample-Sensors.ps1`) still downloads
"latest" when the DLL is absent; that path is a convenience for a hand-run
script, not the build. See §4.

## 2. Live ISO package set — version-unpinned (SBOM-P3-001)

`live/packages*.x86_64` list packages **by name only** (e.g. `python`,
`python-numpy`, `fio`, `mesa-utils`). `live/Build-LiveUSB.sh` appends them to
the archiso `releng` list and `pacstrap`s from the **rolling Arch mirrors at
build time**. Nothing records which versions were resolved.

Consequences:

- An ISO built from tag/run *N* **cannot be reproduced** — the same commit on a
  later day installs newer packages.
- The exact third-party set (and therefore the CVE surface) of a shipped image
  is **unknown after the fact**; only the ISO hash is published, not its
  contents.

**Cheap, safe mitigation implemented now** — the build emits an inventory next
to the ISO (see §3). **Remaining work** (out of scope here, tracked): pin to a
dated snapshot/archive mirror or commit a lock so a build is reproducible.

## 3. ISO artifact checksums + package inventory (implemented)

`live/Build-LiveUSB.sh` writes two provenance files into the variant output dir
(`live/out-<variant>/`) alongside the ISO:

- **`packages.txt`** — the resolved package inventory (`name version`). It is
  taken from the installed rootfs package DB (`pacman -Q` against the built
  rootfs); if that DB is unavailable it falls back to the declared names from
  `live/packages.x86_64` + `live/packages-<variant>.x86_64`, with the source
  noted in the header.
- **`SHA256SUMS`** — `sha256sum` of the ISO.

These are what a downstream SBOM/provenance step consumes; the CI workflow
(`.github/workflows/build-iso.yml`) additionally copies the ISO into `dist/` and
publishes `SHA256SUMS` with the release parts. To reproduce the release check:

```bash
cat bench-live-<gpu>.iso.part.* > bench-live-<gpu>.iso
sha256sum -c SHA256SUMS
```

**Remaining work** (tracked, not done here): emit a real SBOM (e.g. CycloneDX)
from `packages.txt`, and add the same checksum/SBOM step for the **app bundles**
(`.github/workflows/build-app.yml` currently uploads the zips with no checksum,
signature or attestation).

## 4. Unpinned third-party trust (SC-P3-001)

- **Third-party Arch repo.** For the `nvidia-legacy` variant,
  `live/Build-LiveUSB.sh` adds `[omarchy] Server = https://pkgs.omarchy.org/stable/$arch`
  to `pacman.conf`, and `live/ci-build.sh` imports + locally signs
  `live/keys/omarchy.gpg` (key `40DFB630FF42BCFFB047046CF0134EE680CAC571`).
  `nvidia-580xx-dkms`/`utils` and `opencl-nvidia-580xx` are then installed
  **unpinned** from that repo. A compromise there lands in every stick.
  *Review/vendor or pin the 580xx packages before widening use.*
- **Container base.** The non-Arch CI path builds in `archlinux:latest`
  (`live/ci-build.sh`) and runs `pacman -Sy` without `-u`. The base image is
  **not pinned by digest**; pin it (and use `-Syu`) for reproducibility.
- **Runtime PowerShell downloads.** `bench/Sample-Sensors.ps1` (LHM) and
  `bench/Install-GpuFanCurve.ps1` (nvfancontrol 0.5.1) download binaries that
  are loaded/executed; the LHM path has no checksum and the nvfancontrol path
  is tag-pinned but unverified. Prefer the verified `tools/lhm` fetch, and
  verify a pinned hash before executing nvfancontrol (tracked).

## 5. Python / compute dependencies

`bench/bootstrap.py` provisions numpy/psutil and PyTorch/DirectML at runtime;
`live/ci-build.sh` installs torch from the PyTorch cu126 index. These are
installed **unpinned** and are not covered by a lockfile (tracked under
SBOM-P2-001 / SC-P2-001). The dashboards/agent/control plane are **stdlib
only** and add no third-party dependency.
