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
| **telemetry** (1 Hz) | `bench/run_win.py` (Windows), `bench/telemetry_log.py` (Linux) | GPU: util, mem-util, temp, mem-temp, fan, power, power-cap, sm/mem/gr/**video** clocks, VRAM, pstate, enc/dec %, **thermal limit**, **power default/max**, **PCIe gen/width**, **retired pages**, **throttle bitmask**. CPU: util, package/max-core temp, fan RPM, load1, swap, **RAPL package watts**, **avg core MHz**, **PSI io stall**. Disk I/O + net rx/tx MB/s |
| **telemetry stats** | server (`benchd._compute_stats`) | per metric: min/avg/max/**std**/**p50/p95/p99**/n, plus **derived**: energy (J), energy kJ/h, clock residency %, throttled %, thermal headroom, power headroom |
| **events / logs / artifacts** | reporter + phases | `[group] label` phase trail, console, warn events, report JSON + telemetry CSV |

## 2. Where it surfaces

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
- **GPU headroom/health** at 1 Hz: thermal limit, power default/max, video clock,
  PCIe link gen/width, retired pages, throttle/slowdown bitmask.
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

### Still open
- **Windows per-engine GPU** (`Get-Counter '\GPU Engine(*)\Utilization Percentage'`)
  and battery/lid state.
- **NVENC quality** (VMAF/SSIM needs ffmpeg) and **LLM** tokens/s · prompt-eval ·
  model-load time.

### Cheap next additions- **Linux**: `dmidecode` (true DIMM speed/part), `ethtool -S` (drops/errors),
  per-core frequency residency.
- **Windows**: `Win32_Processor` current clock, `Get-WinEvent` Kernel-Power 41
  (unexpected shutdown) → verdict.
- **Sensors**: promote `Get-Sensors.ps1` / `sensors -j` voltage + VRM rails into
  the summary so rails can be trended.

## 4. Conventions when adding a field

1. Add the column to the sampler (`run_win.py` `COLUMNS`, `telemetry_log.py`
   `HEADER`) — **append**, never reorder.
2. Map it in `benchreport.CSV_ALIASES` (tests assert every sampler column maps).
3. Name it in the web `SERIES` / a `CHART_PRESETS` entry; unknown numeric keys
   become "extras" on the server automatically.
4. Fail soft: vendor tools are optional; blank when absent.
