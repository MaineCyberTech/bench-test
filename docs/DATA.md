# Data we collect — and what we could add

A map of the data surface: what a run produces, where it lands, what the
dashboards show today, and what else is cheap to add. Everything here is
**stdlib + optional vendor tools** (`nvidia-smi`, `sensors`, `smartctl`) — no
mandatory third-party dependency.

## 1. What one run produces

| Layer | Where it comes from | What's in it |
|---|---|---|
| **summary** | `benchreport.build_summary_dir()` over the phase JSONs | `gpu_matmul_tflops{fp16,bf16,tf32,fp32}`, `gpu_soak_tflops`, `cpu_matmul_gflops{fp32,fp64}`, `cpu_soak_gflops`, `ram_soak_gbps`, `disk_*`, `net`, `nvenc`, `pcie`, `combined`, `fullsoak{verdict}`, per-phase `telemetry` min/avg/max |
| **system / inventory** | `bench/linux_info.py`, `bench/Get-SystemInfo.ps1` | OS/kernel/build, CPU (name, cores/threads, cache, clocks), RAM (total + **DIMM slots**: size/speed/mfg/part), GPU + driver + VRAM, disks (+ health/firmware), volumes, NICs (speed/MAC/IP), monitors, BIOS/board, uptime |
| **telemetry** (1 Hz) | `bench/run_win.py` (Windows), `bench/telemetry_log.py` (Linux) | GPU: util, mem-util, temp, **mem-temp**, fan, power, **power-cap**, sm/mem/**gr**/**video** clocks, VRAM, pstate, enc/dec %, **thermal limit**, **power default/max**, **PCIe gen/width**, **retired pages**, **throttle reasons**. CPU: util, package/max-core temp, fan RPM, load1, swap. Disk I/O and net rx/tx MB/s |
| **telemetry stats** | server (`benchd`) | min/avg/max/n per metric across the run |
| **events / logs / artifacts** | reporter + phases | `[group] label` phase trail, console lines, warn events, report JSON + telemetry CSV |

## 2. Where it surfaces

- **History table** — host, system line, preset, label, started, duration,
  status, GPU soak TF, CPU soak GF, RAM GB/s, peak °C. Sortable + filterable.
- **Run detail** — tiles (+ % **delta vs the previous run of the same preset on
  that host**), derived tiles (**perf/watt, avg power, thermal headroom, time
  throttled**), a **Results** table that flattens the whole summary (so nothing
  measured is hidden), the full inventory, per-metric min/avg/max, phases,
  console, downloads.
- **Live cards** — gauges, a chart (8 presets incl. headroom/throttle, clocks,
  disk/net), phase table, console, remote terminal.
- **Trends / Compare** — metric over time; A/B of headline metrics.

## 3. Gaps worth closing

### Already measured, previously unsurfaced — now shown
`gpu_matmul_tflops.{bf16,tf32}`, `cpu_matmul_gflops.fp64`, `pcie.h2d/d2h_gbps`,
`disk` step results (`seqread`, `rand*` IOPS, `latency` p50/p90/p99),
`net.upload_mbps` + `ping_*`, `nvenc` per-codec fps, `ram` copy/add/reduce +
latency. All reachable via the **Results** table; promote the headline ones into
tiles/columns as needed.

### Collected but empty (needs elevation / a fallback)
Disk **SMART**: `temp_c`, `wear`, `read_errors`, `write_errors`,
`power_on_hours` are already in the inventory shape but come back `null` when
`Get-StorageReliabilityCounter` isn't available/authorised. On Linux, add
`nvme smart-log` (`percentage_used`, `media_errors`, sensor temps) and
`smartctl -A` as fallbacks.

### Cheap additions (confirmed on real hardware)
- **GPU health/headroom** — `temperature.gpu.tlimit`, `power.{default,min,max}_limit`,
  `clocks.video`, `pcie.link.{gen,width}.current`, `retired_pages.pending`,
  `clocks_throttle_reasons.active`. *Added to both samplers; `throttle` = 1 when a
  non-idle slowdown reason bit is set.*
- **GPU identity** — `uuid`, `vbios_version`, `pci.bus_id`, `inforom` (inventory,
  so a re-seated/replaced card is visible).
- **Other GPU users** — `nvidia-smi --query-compute-apps` → flags a run whose
  numbers were polluted by another process (a validity check).
- **Linux** — RAPL package power (`/sys/class/powercap/*/energy_uj`, real CPU
  watts), per-core `scaling_cur_freq`, PSI (`/proc/pressure/*`), EDAC ECC
  counters, NVMe SMART, `sensors -j` full set (chipset/VRM).
- **Windows** — `Get-Counter '\GPU Engine(*)\Utilization Percentage'` per engine,
  `Win32_Processor` current clock, `Get-WinEvent` for **WHEA/MCE** and TDR
  (`nvlddmkm` 13/14), battery.
- **Errors/verdicts** — Xid (dmesg/event log), PCIe AER, thermal events,
  memory `errors_since_boot` (already in the Linux inventory) → promote to run
  **verdicts**, not just the fullsoak clean flag.

### Analytics we can compute from data we already store
- **p95/p99 + stddev/%CV** per metric (server-side; only min/avg/max today) —
  variance is the stability signal.
- **Energy** — ∫power·dt per phase → Joules and perf/joule.
- **Clock residency** — % samples at ≥95 % of max SM clock; **% time throttled**.
- **Auto-diff** — vs the previous run on the same host+preset (implemented in the
  detail modal; could extend to a "worse than last time" fleet badge).

### Benchmarks to extend
- Disk: random 4K **IOPS** at QD1/QD32 + latency percentiles (partly there),
  sequential **read** (measured, surface it).
- Net: **upload** + latency/jitter/packet loss (upload + ping measured).
- GPU: FP64, INT8/FP8 tensor, ray tracing; VRAM bandwidth patterns (have
  add/reduce).
- CPU: per-core clocks under load, AVX-512 scaling, AES.
- NVENC: fps per codec (measured) + quality (VMAF/SSIM).
- LLM: tokens/s, prompt-eval, VRAM, model load time.

## 4. Conventions when adding a field

1. Add the column to the sampler (`run_win.py` `COLUMNS`, `telemetry_log.py`
   `HEADER`) — **append**, never reorder.
2. Map it in `benchreport.CSV_ALIASES` (tests assert every sampler column maps).
3. Name it in the web `SERIES` / a `CHART_PRESETS` entry; it becomes an "extra"
   on the server automatically (unknown numeric keys are kept in `extra`).
4. Fail soft: vendor tools are optional; blank when absent.
