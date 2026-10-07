# Follow-up register

| Finding | Severity | Title | Owner | Target | Status | Note |
|---|---|---|---|---|---|---|
| SUPPLY-P1-001 | P1 | Get-Sensors.ps1 downloads the mutable 'latest' LibreHardwareMonitor release and loads it in-process without integrity verification | @owner | SUPPLY | open | Pin a specific LibreHardwareMonitor release and verify a committed SHA-256 (or Authenticode signature) before Expand-Arc |
| SUPPLY-P2-001 | P2 | Install-GpuFanCurve.ps1 downloads nvfancontrol with no checksum and runs it from an elevated logon task | @owner | SUPPLY | open | Verify a pinned SHA-256 (or the publisher's signature) before extracting and refuse to register the task on mismatch; al |
| SUPPLY-P2-002 | P2 | PyTorch/numpy are installed at runtime from an unpinned, caller-controlled package index | @owner | SUPPLY | open | Pin exact versions and hashes (requirements with hashes + pip --require-hashes) and constrain -TorchIndex to an allowlis |
| DATA-P2-001 | P2 | Collectors capture stable hardware identifiers and PII with no redaction option, and the workflow encourages sharing results | @owner | DATA | open | Add a redaction mode (drop/hash serials, MACs, IPs, usernames, startup commands) and make it the default for shareable o |
| SAFE-P2-001 | P2 | Default battery runs host-impacting phases without opt-in, and the wrapper defeats the VRAM-fill confirmation | @owner | SAFE | open | Make integrity/soak phases opt-in (or require one explicit confirmation), stop passing --force by default, bound the fil |
| CI-P3-001 | P3 | No CI, dependency automation, or secret scanning in the repository | @owner | CI | open | Add a minimal CI workflow (PSScriptAnalyzer / python compile / pytest plus gitleaks, pinned and sha256-verified) and Dep |
| GOV-P3-001 | P3 | Repository is public while LICENSE declares the material PROPRIETARY AND CONFIDENTIAL | @owner | GOV | open | Either make the repository private or relicense for public distribution, and reconcile README/LICENSE with the actual vi |
