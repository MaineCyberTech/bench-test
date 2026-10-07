# Patch plan

## SUPPLY-P1-001 - Get-Sensors.ps1 downloads the mutable 'latest' LibreHardwareMonitor release and loads it in-process without integrity verification

Pin a specific LibreHardwareMonitor release and verify a committed SHA-256 (or Authenticode signature) before Expand-Archive/Add-Type; fail closed on mismatch. Keep the existing -LibDir override and document supplying a pre-vetted local build.

## SUPPLY-P2-001 - Install-GpuFanCurve.ps1 downloads nvfancontrol with no checksum and runs it from an elevated logon task

Verify a pinned SHA-256 (or the publisher's signature) before extracting and refuse to register the task on mismatch; allow a pre-downloaded local binary. Document the elevated persistence the script installs.

## SUPPLY-P2-002 - PyTorch/numpy are installed at runtime from an unpinned, caller-controlled package index

Pin exact versions and hashes (requirements with hashes + pip --require-hashes) and constrain -TorchIndex to an allowlist; default to not installing (require -NoInstall-style opt-in).

## DATA-P2-001 - Collectors capture stable hardware identifiers and PII with no redaction option, and the workflow encourages sharing results

Add a redaction mode (drop/hash serials, MACs, IPs, usernames, startup commands) and make it the default for shareable output; document the sensitive fields; scrub the committed examples to redacted values.

## SAFE-P2-001 - Default battery runs host-impacting phases without opt-in, and the wrapper defeats the VRAM-fill confirmation

Make integrity/soak phases opt-in (or require one explicit confirmation), stop passing --force by default, bound the fill percentages, and remove the disk temp file in a finally/EXIT handler. Document resource impact and required free space up front.

## CI-P3-001 - No CI, dependency automation, or secret scanning in the repository

Add a minimal CI workflow (PSScriptAnalyzer / python compile / pytest plus gitleaks, pinned and sha256-verified) and Dependabot; make the workflow a required status check on main.

## GOV-P3-001 - Repository is public while LICENSE declares the material PROPRIETARY AND CONFIDENTIAL

Either make the repository private or relicense for public distribution, and reconcile README/LICENSE with the actual visibility.

