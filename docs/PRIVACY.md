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

# Privacy controls (opt-in)

bench-test collects real hardware inventory and telemetry, and the workflow
encourages sharing the results. This page documents the **opt-in** controls that
limit what leaves a machine, and what each one covers. Defaults are unchanged:
with no flags set, a run behaves exactly as before.

## 1. Redaction mode (`BENCH_REDACT=1` / `--redact`)

Off by default. When enabled, stable identifiers and personal data are replaced
with **stable pseudonyms** (`kind-<sha256[:8]>`, e.g. `host-1a2b3c4d`) or
blanked, before they are sent to the control plane, uploaded, or written to a
run's artifacts. The pseudonym is deterministic, so runs from the same machine
still correlate without revealing which machine it is; a second redaction pass
leaves an existing token unchanged.

Enable it one of these ways:

| Surface | How |
|---|---|
| Environment (all collectors) | `BENCH_REDACT=1` |
| Windows runner | `python bench/run_win.py --redact` |
| Linux inventory phase | `python bench/linux_info.py info --redact` |
| Headless agent | `python bench/agent.py --redact ...` |

### What is redacted

| Category | Fields (by key, case-insensitive) | Result |
|---|---|---|
| Hostname | `hostname`, `host`, `host_name`, `computername`, `computer_name`, `nodename` | `host-<sha256[:8]>` |
| User / home | `username`, `user`, `user_name`, `login`, `owner`, `userprofile`, `home`, `homepath`, `homedir` | `user-<sha256[:8]>` |
| Serials | `serial`, `serialnumber`, `serial_number` | `serial-<sha256[:8]>` |
| GPU / product UUID | `uuid`, `machine_uuid`, `product_uuid` | `uuid-<sha256[:8]>` |
| Machine id | `machine_id`, `machineid`, `machineguid` | `machine-<sha256[:8]>` |
| Network | `mac`, `macaddress`, `mac_address`, `hwaddr` | `mac-<sha256[:8]>` |
| Network | `ip`, `ipaddress`, `ip_address`, `ipv4`, `ipv6` | `ip-<sha256[:8]>` |

Free text (log lines, health strings, report Markdown) is also scrubbed of:

* MAC addresses (`aa:bb:cc:dd:ee:ff`),
* valid IPv4 addresses (four 0–255 octets),
* e-mail addresses,
* home directories — `/home/<user>`, `/Users/<user>`, `C:\Users\<user>`,
* the local machine's own hostname.

### What is *not* redacted

Hardware **models** are kept, because they are the point of the benchmark and are
not personal data: CPU/GPU names, RAM size, disk models, BIOS/board
manufacturer/model, and all measured metrics (TFLOPS, GB/s, temps, …). Binary
artifacts (screenshots) pass through unchanged.

### Where it is applied

* `benchreport.system_info()` / `read_inventory()` — the live inventory sent at
  register/start/finish, and the merged summary (`build_summary`,
  `build_summary_dir`).
* `Reporter.register`/`start_run`/`finish` — runner name, machine id, run host,
  summary and system blocks.
* `upload_run_files()` / `upload_telemetry_csv()` — text artifacts (JSON/JSONL
  are parsed and walked; other text is scrubbed) before they are attached.
* `bench/linux_info.py` — the Linux inventory phase result.
* `bench/run_win.py --redact` — redacts the run's files in place after the
  battery, scoped to the files this run wrote.
* `server/import_results.py` — the same redaction when backfilling.

> Windows inventory is collected by PowerShell (`Get-SystemInfo.ps1`). It is
> redacted on the Python boundary — the in-memory summary/inventory sent to the
> control plane, the uploaded artifacts, and (with `run_win.py --redact`) the
> on-disk run files — rather than inside the `.ps1`, so there is one redaction
> implementation for both platforms.

## 2. Upload consent (`BENCH_UPLOAD_CONSENT=1`)

Publishing a results directory to a GitHub repository requires an explicit
opt-in. Without it, `bench/upload.py` skips the upload and logs:

```
[upload] skipped: no consent. Results were NOT published to GitHub.
```

* `BENCH_UPLOAD_CONSENT=1` grants consent for that process/session.
* In the TUI, pressing **`u`** is the operator's confirmation and sets
  `BENCH_UPLOAD_CONSENT` for the session. The automatic post-collect upload
  (`AUTO_UPLOAD=1`) only runs once consent has been granted this way — it never
  publishes on its own.
* The GUI does not publish to GitHub, so no confirmation is needed there.

## 3. Control plane: client IP, retention and anonymisation

`server/benchd.py` stores a runner's source address (`runners.addr`), hostname
(`name`) and machine id (`machine_id`).

* **Stop storing the client IP** — `--store-client-ip=off` (or
  `BENCHD_STORE_CLIENT_IP=0`). The `addr` column is then written as `NULL`.
  Default is `on` (unchanged).
* **Anonymise stale runners** — the retention sweep (hourly, same
  `BENCHD_RETENTION_DAYS` window as per-sample retention, default 30 days)
  additionally runs `anonymise_old_runners()`: for runners quiet longer than the
  window it blanks `addr` and pseudonymises `name` / `machine_id`. Run history
  keeps its own host label (what History/Trends group on), so fleet trends are
  unaffected; a returning machine simply re-registers. `BENCHD_RETENTION_DAYS=0`
  disables both the per-sample retention and the anonymisation.

| Setting | Where | Default | Effect |
|---|---|---|---|
| `--store-client-ip=off` / `BENCHD_STORE_CLIENT_IP=0` | `benchd.py` | on | do not persist the client IP |
| `BENCHD_RETENTION_DAYS` | `benchd.py` | 30 | window for per-sample retention and runner anonymisation (`0` = keep everything) |
