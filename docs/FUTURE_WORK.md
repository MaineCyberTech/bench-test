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
