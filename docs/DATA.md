# Data we collect — and what we could add

A map of the data surface: what a run produces, where it lands, what the
dashboards show, and what else is cheap to add. Everything here is **stdlib +
optional vendor tools** (`nvidia-smi`, `sensors`, `nvme`, `smartctl`) — no
mandatory third-party dependency.

## 1. What one run produces

| Layer | Where it comes from | What's in it |
|---|---|---|
| **summary** | `benchreport.build_summary_dir()` over the phase JSONs | `gpu_matmul_tflops{fp16,bf16,tf32,fp32,fp64}`, `gpu_soak_tflops`, `cpu_matmul_gflops{fp32,fp64}`, `cpu_soak_gflops`, `ram_soak_gbps`, `disk_*`, `net` (+ `ping_mdev_ms`, `ping_loss_pct`), `nvenc`, `pcie`, `combined`, `fullsoak{verdict}`, `verdicts[]`, per-phase `telemetry` |
| **system / inventory** | `bench/linux_info.py`, `bench/Get-SystemInfo.ps1` | OS/kernel, CPU, RAM (**DIMM slots**: size/speed/mfg/part), GPU (**uuid, vbios, PCIe bus**, compute users), disks (+ health/firmware), volumes, NICs, monitors, BIOS/board, uptime |
| **health report** | `bench/Get-EventHealth.ps1` (run by the battery) | WHEA, TDR (`nvlddmkm`), disk/power/thermal events; per-disk reliability counters |
| **telemetry** (4 Hz rows) | `bench/run_win.py` (Windows), `bench/telemetry_log.py` (Linux) | GPU: util, mem-util, temp, mem-temp, fan, power, power-cap, sm/mem/gr/**video** clocks, VRAM (**used + total**), pstate, enc/dec %, **thermal limit**, **power default/max**, **PCIe gen/width**, **retired pages**, **throttle bitmask**, **GPU fan RPM**, **3D/copy engine %**. CPU: util, package/max-core temp, fan RPM, load1, swap, **RAPL/package watts**, **avg core MHz**, **PSI io stall**, **one column per logical core** (`cpu_core1..N`). Disk I/O + net rx/tx MB/s + **storage temperature** |
| **telemetry on AMD/Intel Windows** | `bench/Sample-Sensors.ps1` (LibreHardwareMonitor) | No `nvidia-smi` exists on AMD/Intel, so the library supplies GPU temp/power/clocks/util/VRAM + load, fan RPM, engine loads, CPU temp/package watts/clock and per-core load. It is the only local source for those columns there. Best-effort by design: **a sensor the library cannot read goes out blank, never a fake zero**, and a 0 reading for temp/power/clock is treated as unreadable. CPU temp/power/clock normally need an **administrator** shell; without it those columns stay blank and the runner says so. The DLL is vendored under `tools/lhm` and shipped in the app bundle, with a mirror + retries if a download is needed |
| **telemetry stats** | server (`benchd._compute_stats`) | per metric: min/avg/max/**std**/**p50/p95/p99**/n, plus **derived**: energy (J), energy kJ/h, clock residency %, throttled %, thermal headroom, power headroom |
| **events / logs / artifacts** | reporter + phases | `[group] label` phase trail, console, warn events, report JSON + telemetry CSV |

## 2. Where it surfaces

Both dashboards render the **same** field set from one shared list
(`benchreport.DASH_FIELDS`) and the same hardware line
(`benchreport.hardware_line()`), so the GUI and TUI cannot drift:

- **History table** — sortable/filterable: host, system, preset, label, started,
  duration, status, GPU soak TF, CPU soak GF, RAM GB/s, peak °C.
- **Run detail** — headline tiles (**+ %Δ vs the previous run of the same preset
  on that host**); derived tiles (perf/watt, energy, thermal headroom, time
  throttled, clock residency, power headroom); **hardware verdicts**
  (WHEA/TDR/ECC/disk/SMART); a **Results** table flattening the whole summary;
  the full inventory; per-metric min/avg/max·std·p95/p99; phases, console,
  downloads.
- **Live cards** — gauges, chart presets (incl. headroom·throttle, clocks·video,
  disk·net, CPU power·clocks), phase table, console, remote terminal.
- **Trends / Compare** — metric over time; A/B of headline metrics.

## 3. Status of the gaps

### Closed
- **Measured-but-hidden results** are now shown (Results table): PCIe H2D/D2H,
  disk seq-read + random **IOPS** + latency **p50/p90/p99**, net upload + ping,
  NVENC fps, RAM copy/add/reduce + latency, the fp16/bf16/tf32/fp32 matrix.
- **GPU headroom/health** at 4 Hz rows: thermal limit, power default/max, video clock,
  PCIe link gen/width, retired pages, throttle/slowdown bitmask.
- **Windows GPU identity** comes from `nvidia-smi` when present and falls back to WMI
  (`Win32_VideoController`, skipping the Basic Display Adapter) otherwise - without
  that fallback an AMD/Intel machine registered with no GPU at all and the fleet
  view showed a blank card.
- **Hardware verdicts** from `health.json` (WHEA, TDR, disk I/O errors, wear) and
  the Linux inventory (Xid/MCE/AER lines since boot, EDAC ECC, NVMe SMART) →
  `summary.verdicts[]`, shown as pass/warn/fail in run detail.
- **Linux telemetry**: RAPL package watts, average core MHz, PSI I/O stall.
- **Disk SMART**: `nvme smart-log` + `smartctl -A -j` on Linux (used %, media
  errors, unsafe shutdowns, temps). On Windows, `Win32_DiskDrive.Status`
  (`OK`/`Pred Fail`) + `MSStorageDriver_FailurePredictStatus` are read **without
  elevation**, and the reliability counters (temp/wear/error totals/power-on
  hours) are read when elevated; the inventory records `smart` and `Elevated`
  so the verdict layer can report predictive failure and note when SMART was not
  collected (an *info* row, not a warning).
- **Analytics**: p95/p99/stddev, energy (J), clock residency, throttled %,
  thermal/power headroom; Δ vs the previous run of the same preset.
- **Benchmarks**: GPU **fp64**; net **jitter (mdev)** + **packet loss**.
- **Fleet analytics**: `/trends` carries median/p95/p99 per series; the **Fleet**
  tab shows medians per machine and per **GPU model**; History flags a run whose
  throughput is ≥3% below the previous run of the same preset on that host.
- **Results that were measured but dropped**: `_merge_into` now carries through
  NVENC (**fps + SSIM/PSNR**), LLM (**tokens/s** per model), PCIe **H2D/D2H**,
  per-phase disk (seq/rand/**latency p50/p90/p99**) and RAM
  bandwidth/latency — previously only a handful of phases reached the summary.
- **Windows events → verdicts**: Kernel-Power **41** (unexpected shutdown),
  bugcheck/BSOD, storage and thermal events, alongside WHEA/TDR.
- **Windows per-engine GPU**: 3D + Copy engine utilisation (`Get-Counter`) at
  5 s, so Copy/Encode saturation isn't hidden by `utilization.gpu`.

### Still open
- **VMAF** for encode quality (needs ffmpeg built with libvmaf) — SSIM + PSNR are
  collected instead, per codec.
- **LLM prompt-eval and model-load time** — `Invoke-LlmBench.ps1` records eval
  tokens/s (now surfaced) but not TTFT / prefill.
- **Windows battery / lid** state.

### Cheap next additions
- **Linux**: `dmidecode` (true DIMM speed/part), `ethtool -S` (drops/errors),
  per-core frequency residency.
- **Windows**: `Win32_Processor` current clock; per-fan RPM.
- **Sensors**: promote `Get-Sensors.ps1` / `sensors -j` voltage + VRM rails into
  the summary so rails can be trended.

## 4. Conventions when adding a field

1. Add the column to the sampler (`run_win.py` `COLUMNS`, `telemetry_log.py`
   `HEADER`) — **append**, never reorder.
2. Map it in `benchreport.CSV_ALIASES` (tests assert every sampler column maps).
3. Name it in the web `SERIES` / a `CHART_PRESETS` entry; unknown numeric keys
   become "extras" on the server automatically.
4. Fail soft: vendor tools are optional; blank when absent.

## 5. Retention

Two knobs bound the growth; both default to generous limits and are safe to raise.
Retention **never** touches a running run, and it never deletes a run itself.

| What | Where | Default | Override |
|---|---|---|---|
| Per-sample telemetry, logs and events of **finished** runs | control-plane DB | 30 days | `BENCHD_RETENTION_DAYS=0` keeps everything |
| Run directories under `results/` | each runner, when a run finishes | newest 100 | `BENCH_KEEP_RUNS=0` keeps everything |
| Valley screenshots | each run directory | newest 1 | `prune_screenshots(results_dir, keep=N)` |

The run row — with the stats materialised just before its samples go — plus its
phases, artifacts and summary stay, because that is what History, Trends, Compare
and Fleet read; the sweep deletes nothing else (not artifacts, commands or
schedules), and the runner-side prune only considers directories named like a run
(`host-mode-YYYYMMDD-HHMMSS`). Only the per-sample tables grow with run length and
sample rate (4 Hz is ~4.5 MB per 15-minute run), so only those are swept — hourly,
from the control plane's scheduler loop.

## 6. Schema and integrity

The control-plane database (`server/benchd.py`) is stdlib SQLite with no migration
framework; one integer and a few pragmas carry its integrity guarantees.

- **Schema version** — `SCHEMA_VERSION` is written to `PRAGMA user_version` on a
  fresh database. On startup a **newer** version is refused with a clear message and
  a non-zero exit (the process never touches a schema it does not understand); an
  **older** one is upgraded in place and logged.
- **Foreign keys** — `PRAGMA foreign_keys=ON` on every connection. The run-dependent
  tables (`phases`, `telemetry`, `logs`, `events`, `artifacts`, `commands`) declare
  `REFERENCES runs(id) ON DELETE CASCADE` in `CREATE TABLE`, so a fresh database
  clears dependents at the storage layer as well as in `Store.delete_run`.
- **Atomic phase writes** — `phases(run_id, seq)` has a unique index and writes use
  `INSERT OR REPLACE`, so a replayed phase is idempotent. A legacy database that
  already holds duplicates logs a warning and keeps the old behaviour.
- **Per-runner keys at rest** — a runner's API key is stored only as its `sha256`
  digest; what a client presents is hashed before comparison. Any plaintext key in
  an existing database is migrated to its digest on startup, so old keys keep
  working. (Operators and the join token are separate shared secrets, unchanged.)

