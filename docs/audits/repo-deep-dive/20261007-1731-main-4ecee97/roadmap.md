# Roadmap

- SUPPLY-P1-001 (P1) - Get-Sensors.ps1 downloads the mutable 'latest' LibreHardwareMonitor release and loads it in-process without integrity verification
- SUPPLY-P2-001 (P2) - Install-GpuFanCurve.ps1 downloads nvfancontrol with no checksum and runs it from an elevated logon task
- SUPPLY-P2-002 (P2) - PyTorch/numpy are installed at runtime from an unpinned, caller-controlled package index
- DATA-P2-001 (P2) - Collectors capture stable hardware identifiers and PII with no redaction option, and the workflow encourages sharing results
- SAFE-P2-001 (P2) - Default battery runs host-impacting phases without opt-in, and the wrapper defeats the VRAM-fill confirmation
- CI-P3-001 (P3) - No CI, dependency automation, or secret scanning in the repository
- GOV-P3-001 (P3) - Repository is public while LICENSE declares the material PROPRIETARY AND CONFIDENTIAL
