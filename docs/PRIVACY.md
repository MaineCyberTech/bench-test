# Privacy & data governance

What a bench-test run collects, where it goes, how long it is kept, and who else
sees it. This is a map of the current behaviour, not a claim that every control is
in place — the open gaps are listed at the end and tracked in
[`KNOWN_ISSUES.md`](KNOWN_ISSUES.md).

## 1. Data classification

| Class | Examples | Where it comes from | Notes |
|---|---|---|---|
| **Operational telemetry** | GPU util/temp/fan/power/clocks/VRAM/pstate/enc-dec; CPU util/temp/fan/load/swap/package watts/core MHz/PSI; disk read/write + storage temp; net rx/tx; per-core load | `run_win.py`, `telemetry_log.py`, `Sample-Sensors.ps1` | 4 Hz rows. Not personal data on its own. |
| **System inventory** | OS/kernel, CPU, DIMMs (size/speed/mfg/part), GPU (name, driver, vbios, **serial, uuid**, PCIe bus, compute users), disks (+ health/firmware), volumes, NICs, monitors, BIOS/board, uptime | `linux_info.py`, `Get-SystemInfo.ps1` | Contains stable hardware identifiers. |
| **Host / identity (PII)** | **Hostname**, usernames and absolute home paths, control-plane client **IP address**, `machine_id` (Windows `MachineGuid`, Linux DMI uuid / board serial / `/etc/machine-id`), MAC/IP in committed examples | `benchreport.py` (`machine_id()`, `Reporter`), the Windows battery (`$env:COMPUTERNAME`), `Run-LinuxBench.sh` (`hostname -s`), the server (`runners.addr`) | Sensitive: host-identifying. |
| **Health / events** | WHEA, TDR (`nvlddmkm`), Kernel-Power 41, bugcheck, disk/power/thermal events, ECC, SMART | `Get-EventHealth.ps1`, Linux inventory | Feeds the run's stability verdict. |

`docs/DATA.md` is the field-level map of a single run; this document covers the
classification, storage and exposure.

## 2. Where data is stored and exported

| Store | Location | Contents |
|---|---|---|
| **SQLite (control plane)** | `server/data/benchd.sqlite` (git-ignored) | runners (name, `addr` = client IP, `machine_id`, plaintext `api_key`), runs, phases, telemetry, logs, events, artifacts, commands, schedules |
| **Local results** | `results/` on each runner | summary JSON, report Markdown, `*.telemetry.csv`, logs, screenshots, system inventory |
| **Upload (opt-in)** | a GitHub repository | `bench/upload.py` publishes a whole results directory via the GitHub Git Data API using a `GITHUB_TOKEN` from a git-ignored config; `AUTO_UPLOAD=1` makes the dashboards upload after collect |
| **Control plane transport** | HTTP (bearer token) | runners stream phases, telemetry, logs and inventory to `benchd` |
| **Release artifacts** | GitHub Releases | app bundles and the live ISO carry a baked control-plane config (world-readable) |

## 3. Retention defaults

From `docs/DATA.md` § 5 — both knobs default to keeping data and are safe to raise.

| What | Where | Default | Override |
|---|---|---|---|
| Per-sample telemetry, logs and events of **finished** runs | control-plane DB | 30 days | `BENCHD_RETENTION_DAYS=0` keeps everything |
| Run directories under `results/` | each runner | newest 100 | `BENCH_KEEP_RUNS=0` keeps everything |
| Valley screenshots | each run directory | newest 1 | `prune_screenshots(results_dir, keep=N)` |

Run rows, phases, artifacts and summaries are never auto-deleted, because that is
what History, Trends, Compare and Fleet read. There is no scheduled erasure of the
SQLite database or of an uploaded results directory.

## 4. Public-repo exposure rule

The repository, and the app-bundle and live-ISO releases, are **public**. Treat
anything committed or baked into an artifact as world-readable:

- Do not commit tokens, keys or the SQLite database; `server/benchd.conf`,
  `server/data/`, `live/private/` and `~/.config/bench-agent.conf` are git-ignored
  for this reason.
- A control-plane token baked into a release is readable by anyone who downloads
  it — use a limited, rotatable value, and rotate it if a release leaves the lab.
- Results committed under `results/`, and the committed `examples/`, can contain
  hostnames, usernames/home paths, IP/MAC addresses and hardware serials. Do not
  commit a machine's real inventory or results without redacting it first.
- Report a leaked or baked credential privately (see [`SECURITY.md`](../SECURITY.md)),
  not in a public issue.

## 5. Vendor / processor list

Only the third parties the repository actually contacts:

| Vendor / endpoint | Used for | Data involved |
|---|---|---|
| **GitHub** (`github.com`, `api.github.com`, Releases) | repo hosting; app/ISO releases; `upload.py` writes results; the control plane compares commits | committed/uploaded results; release artifacts; the requests themselves |
| **Cloudflare** (`speed.cloudflare.com`) | the `net` group's download/upload throughput test | a small payload sent to Cloudflare, plus the client IP it sees |
| **python-build-standalone** (`astral-sh/python-build-standalone` GitHub releases) | `bench/bootstrap.py` downloads a pinned CPython for machines with no Python | the download request only |
| **PyTorch / PyPI indexes** (`download.pytorch.org`, PyPI) | compute-phase dependencies (CUDA torch, `onnxruntime-directml`, numpy, psutil, cryptography) installed at runtime | the download request only |
| **LibreHardwareMonitor** (`LibreHardwareMonitor` GitHub releases) | AMD/Intel Windows sensor DLL, vendored under `tools/lhm` with a download fallback | the download request only |
| **Ollama** (local) | the `llm` group runs models through a local Ollama server | stays on the workstation; not a remote processor |

No notification/analytics service (for example ntfy) is used by the repository.

## 6. Consent & opt-out

- The control plane only receives data from a runner that has been pointed at it
  (`BENCHD_SERVER`/`BENCHD_TOKEN` or a baked config); a runner with no server
  configured stays local.
- Uploading results to GitHub requires a `bench-upload.conf` with a token, or
  `AUTO_UPLOAD=1` for the dashboards to upload after collect.
- Set `BENCH_NO_UPDATE=1` to stop the startup update check.

## 7. Current gaps

- There is **no redaction mode**: serials, MAC/IP addresses, hostnames and
  usernames are collected and can be published as-is (`AN-P2-001`,
  `PRIV-P2-001`).
- There is **no retention or purge** for the SQLite database or an uploaded results
  directory (`PRIV-P2-004`).
- Privileged actions carry **no actor identity** (single shared operator token),
  so there is no attributable audit trail (`PRIV-P2-002`).
- The operator token and the upload credential are **baked into public artifacts**
  (`SEC-P0-001`).

These, and the rest of the open P0/P1/P2 items, are listed in
[`KNOWN_ISSUES.md`](KNOWN_ISSUES.md).
