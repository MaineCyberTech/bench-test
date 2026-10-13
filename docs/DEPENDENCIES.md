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
