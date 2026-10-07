# Future work — data sources not yet collected

The toolkit already covers GPU, CPU, RAM, disk, network, sensors, PCIe topology, health
events and environment. Remaining ideas, roughly in priority order:

## Hardware / sensors
- **CPU package power (RAPL)** — read Intel/AMD energy counters via MSR (needs a signed
  driver or vendor tool; not exposed via ACPI on workstation boards like the HP Z4 G4).
- **CPU core temperatures where the board hides them** — fall back to Intel Power Gadget /
  OpenHardwareMonitor MSR path when LHM's core-temp sensors are blank.
- **NVMe SMART via passthrough** — vendor-specific `Get-StorageReliabilityCounter` often
  returns blank (temp/wear) on some NVMe drives; add an IOCTL/NVMe-log fallback.
- **PCIe AER error counters** — corrected/uncorrected Advanced Error Reporting counts.
- **Wall power** — via a smart PDU / UPS / IPMI (kWh + instantaneous W).
- **USB power/reset/error counters**, and USB device inventory by class.

## Firmware / platform
- **BIOS/UEFI update-availability check** — compare the installed SMBIOS version against a
  vendor database (HP/Dell/Lenovo/ASRock/ASUS) and flag out-of-date firmware.
- **Secure Boot / TPM attestation** — measured-boot log + TPM PCR baselines.
- **Boot/POST timing** — repeated cold/warm boot timing (needs a reboot harness).

## Stress / endurance
- **Simultaneous full-system soak** — GPU + CPU + RAM + disk all at once for hours, with
  per-subsystem telemetry and a stability verdict.
- **GPU VRAM error soak with ECC** — for datacenter GPUs (ECC error counters via nvidia-smi).
- **Power-fluctuation / brownout resilience** — not automatable without controllable power.

## Network
- **LAN throughput** — `iperf3` client/server (needs a peer) for real link saturation.
- **Latency under load**, jumbo-frame, and link-error counters.

## Software / fleet
- **Driver signing / WHQL status** and out-of-box driver baseline.
- **Windows Update compliance** against a target patch level.
- **Automatic cross-host comparison** — a `tools/compare.py` that ingests several
  `results/<host>-*.json` and emits the `COMPARISON.md`/`.json` we build by hand.

## Nice-to-haves
- A single `results/report.html` dashboard aggregating a run.
- Signed/timestamped run manifests for tamper evidence.

## Additional tests to add (beyond the current battery)

### CPU
- **AVX-512 / SIMD FLOPS** — Skylake-W (W-2123) and newer support AVX-512; measure GFLOPs AVX-512 vs AVX2.
- **AES-NI encryption throughput** (`openssl speed -evp aes-256-gcm`), plus SHA-256.
- **Compression/decompression** throughput (zstd / gzip / 7z) — CPU + memory bound.
- **Single-thread vs multi-thread** scaling (sieve/prime), and Linpack-lite GFLOPS.

### GPU
- **FP8 / INT8 tensor throughput** (newer architectures).
- **Ray tracing / mesh shaders** (DX12 RT benchmark, e.g. 3DMark Speed Way / Port Royal).
- **DLSS/FSR upscaling** test.
- **Video decode (NVDEC)** throughput — 4K/8K, H.264/HEVC/AV1.
- **NVENC quality** (VMAF/SSIM) vs x264, not just fps.
- **CUDA memory latency** via a compiled pointer-chase kernel.

### Storage
- **Steady-state / SLC-cache-exhaustion write** — write 50-100 GiB and chart the throughput drop.
- **Queue-depth scaling** (QD1/8/32/128) for 4 KiB random.
- **Latency percentiles** (p50/p99/p99.9).
- **Full SMART attribute dump** (`smartctl -a`) — richer than `Get-StorageReliabilityCounter`.
- **NVMe temperature under load**; RAID/array throughput.

### Memory
- **True latency** (compiled dependent-load pointer chase).
- **Multi-threaded STREAM** (numpy is single-threaded).
- **ECC error counters** (server platforms).

### Network
- **LAN throughput** via `iperf3` client/server.
- **Latency under load** (bufferbloat), jumbo frames, link-error counters, Wi-Fi signal/throughput.

### System / thermal
- **Thermal soak** — steady-state CPU/GPU/mobo temps over 30-60 min at defined load (fan-curve validation).
- **Fan RPM / PWM** where sensors expose it.
- **Full-system simultaneous soak** — GPU + CPU + RAM + disk together with a stability verdict.
- **Suspend/resume + reboot stress**, POST/boot timing.
- **Wall power draw** (smart PDU / UPS / IPMI).

### ML / software
- **Image inference** (ResNet) img/s, **Stable Diffusion** images/min, **YOLO** FPS, **embeddings**/s.
- **Container/VM performance** (docker build, sysbench), **database** (pgbench/sysbench).

