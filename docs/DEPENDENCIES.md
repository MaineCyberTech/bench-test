# Dependencies & license policy

bench-test is a Windows/Linux workstation benchmark toolkit. The **dashboards
and agent are stdlib-only** and ship as PyInstaller bundles that run without a
Python install; only running a **battery** needs a runner interpreter with the
packages below. `bench/bootstrap.py` provisions that interpreter and virtualenv
on first run (per-user cache: `%LOCALAPPDATA%\bench-test` on Windows,
`~/.local/share/bench-test` elsewhere; a repo `.venv` or `$BENCH_REPO` wins).

The declared manifests are the machine-readable mirror of what the code
installs:

| Manifest | Contents |
|---|---|
| [`requirements.txt`](../requirements.txt) | base runtime deps + the Windows TUI extra |
| [`requirements-compute.txt`](../requirements-compute.txt) | GPU backends (DirectML / CUDA torch) |

> **Keep these in sync with the code.** They mirror `bench/bootstrap.py`
> (`BASE_PACKAGES`, `GPU_PACKAGES`, `packages_for()`) and the runtime installs in
> `bench/Invoke-GpuBench.ps1`, `bench/Run-WorkstationBench.ps1`, `bench-gui.cmd`,
> `bench-tui.cmd` and `live/ci-build.sh`. When one changes, change the other.

## Runtime dependencies

`bootstrap.py` installs these **unpinned** (bare package names, no version
specifier); the manifests therefore do not invent versions. Where each is needed:

| Package | License | Where needed | How it is installed |
|---|---|---|---|
| `numpy` | BSD-3-Clause | **base** — CPU/RAM/disk/GPU phases (array math) | `bootstrap.ensure_env` → `BASE_PACKAGES`; `live/ci-build.sh` venv |
| `psutil` | BSD-3-Clause | **base** — process/memory/disk telemetry | `bootstrap.ensure_env` → `BASE_PACKAGES` |
| `cryptography` | Apache-2.0 / BSD-3-Clause | **base** — AES-256-GCM phase in `cpu_bench.py` (skipped if absent) | `bootstrap.ensure_env` → `BASE_PACKAGES` |
| `windows-curses` | MIT | **base, Windows only** — the curses TUI dashboard | `bootstrap.packages_for` when `os.name == "nt"`; `bench-tui.cmd` |
| `onnxruntime-directml` | MIT | **compute** — AMD/Intel Windows GPU backend (DirectML) | `bootstrap.packages_for` for vendor `amd`/`intel`; `Invoke-GpuBench.ps1` |
| `onnx` | Apache-2.0 | **compute** — model/backend support for the DirectML path | `bootstrap` `GPU_PACKAGES["dml"]`; `Invoke-GpuBench.ps1` |
| `torch` | BSD-3-Clause | **compute** — NVIDIA CUDA backend (and CPU fallback) | `Invoke-GpuBench.ps1` / `Run-WorkstationBench.ps1` (`-TorchIndex`); baked into the **live USB** by `live/ci-build.sh` |

**live USB:** `live/ci-build.sh` builds a venv with `torch numpy psutil
cryptography` (torch from `--index-url https://download.pytorch.org/whl/cu126`,
`--extra-index-url https://pypi.org/simple`) and bakes it into the image
(`/opt/bench-test/py-packages`, re-linked to `TORCH_DIR` at runtime by
`benchreport.ensure_torch_packages`). The live USB's dashboards are stdlib-only;
the baked packages serve the battery phases.

**NVIDIA torch index:** the runners default `-TorchIndex` to
`https://download.pytorch.org/whl/cu128` (README uses `.../cu126` for pre-Turing
cards on Linux). The index is caller-controllable and the installs are unpinned —
hardening this (exact pins + `--require-hashes`, index allowlist) is tracked as a
separate supply-chain follow-up; the manifests here record the *current* behavior.

## Third-party binaries

These are downloaded and/or bundled by the toolkit and are listed in
[`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md):

| Binary | License | How it is obtained |
|---|---|---|
| LibreHardwareMonitor (`LibreHardwareMonitorLib.dll`) | MPL-2.0 | `bench/Get-Sensors.ps1` / `Sample-Sensors.ps1` download `releases/latest`; the app bundle ships `tools/lhm` when present |
| nvfancontrol (RTX, `0.5.1`) | GPL-3.0-or-later | `bench/Install-GpuFanCurve.ps1` downloads the pinned tag at run time; installed as an elevated logon task |

## Generating the SBOM

`packaging/sbom.py` (stdlib only) emits a **CycloneDX 1.4** JSON document with a
component for every declared Python dependency, the bundled third-party binaries,
and the toolkit itself (release `VERSION` + git build ref/commit in `metadata`):

```bash
python packaging/sbom.py                 # -> stdout
python packaging/sbom.py --out sbom.json  # -> file
```

Attach the emitted `sbom.json` to a release so consumers can inspect the exact
dependency set for a build.

## License policy

**Goal:** every dependency and bundled binary must carry a license we can
distribute under, and every exception must be recorded and reviewed.

**Allowed** (permissive / weak-copyleft, no copyleft obligation on our code):
MIT, BSD-2-Clause, BSD-3-Clause, ISC, Apache-2.0, PSF-2.0, Python-2.0, MPL-2.0.

**Denied** (strong copyleft or non-open terms that conflict with this
proprietary, distributed toolkit): GPL-2.0, GPL-3.0, AGPL-3.0, LGPL (unless
dynamically linked with a clear separation), SSPL, BUSL/BSL, Elastic License,
CC-BY-NC*, and any **proprietary / unlicensed / unknown** component.

**Bundled binaries.** LibreHardwareMonitor is **MPL-2.0** (allowed): keep its
license notice and offer its source. nvfancontrol is **GPL-3.0-or-later**
(denied by default) — it is *not redistributed* (downloaded at run time, invoked
as a separate process, never linked into the toolkit), which is the accepted
mitigation; it is recorded here and in `THIRD_PARTY_NOTICES.md`.

**Exception process:**
1. Open an issue naming the package/binary, its version, license, why it is
   needed, and the proposed mitigation (e.g. runtime download, separate process,
   or a permissively-licensed replacement).
2. Get sign-off from the repo owner (and legal, where the license is copyleft or
   non-open).
3. Record the approval and the full license text/link in
   `THIRD_PARTY_NOTICES.md`, and re-run `packaging/sbom.py` so the SBOM reflects
   it.
4. Prefer eliminating the dependency over carrying an exception.

# Artifact provenance

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
