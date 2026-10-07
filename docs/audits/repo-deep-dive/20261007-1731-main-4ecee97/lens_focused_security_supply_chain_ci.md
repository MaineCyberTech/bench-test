# Focused security / supply-chain / CI deep-dive - bench-test

## Findings

| ID | Severity | Title | Report |
|---|---|---|---|
| SUPPLY-P1-001 | P1 | Get-Sensors.ps1 downloads the mutable 'latest' LibreHardwareMonitor release and loads it in-process without integrity verification | lens_focused_security_supply_chain_ci.md |
| SUPPLY-P2-001 | P2 | Install-GpuFanCurve.ps1 downloads nvfancontrol with no checksum and runs it from an elevated logon task | lens_focused_security_supply_chain_ci.md |
| SUPPLY-P2-002 | P2 | PyTorch/numpy are installed at runtime from an unpinned, caller-controlled package index | lens_focused_security_supply_chain_ci.md |
| DATA-P2-001 | P2 | Collectors capture stable hardware identifiers and PII with no redaction option, and the workflow encourages sharing results | lens_focused_security_supply_chain_ci.md |
| SAFE-P2-001 | P2 | Default battery runs host-impacting phases without opt-in, and the wrapper defeats the VRAM-fill confirmation | lens_focused_security_supply_chain_ci.md |
| CI-P3-001 | P3 | No CI, dependency automation, or secret scanning in the repository | lens_focused_security_supply_chain_ci.md |
| GOV-P3-001 | P3 | Repository is public while LICENSE declares the material PROPRIETARY AND CONFIDENTIAL | lens_focused_security_supply_chain_ci.md |

---

# Focused security / supply-chain / CI deep-dive — bench-test

Target: `MaineCyberTech/bench-test` (public) @ `main` `4ecee97`
Profile: focused security / supply-chain / CI (base pack). Read-only audit.
Deterministic lens: run locally with the pack's `tools/deterministic_checks.py`
(lab unavailable — see Verification Performed).

## Scope

A reusable Windows workstation benchmark/stress toolkit (GPU compute, RAM, disk,
PCIe, NVENC, LLM, gaming) written as PowerShell runners plus Python phases. The
audit focused on the code paths that run on the target workstation and touch the
network, download and execute binaries, install packages, or collect host data.

Reviewed: `bench/*.ps1`, `bench/*.py`, `README.md`, `docs/*`, `examples/*`,
`LICENSE`, `.gitignore`, repository settings (visibility, workflows).

## Verification Performed

- Repository content read at the bound commit `4ecee979cea2e09292446262b0a84a1cece8187d`
  (cloned locally; `git ls-files --eol` confirms LF in the index).
- Deterministic lens: `tools/deterministic_checks.py` (pack tooling) run against
  the clone with gitleaks 8.30.1 and actionlint 1.7.12, installed pinned and
  sha256-verified exactly as `.github/workflows/deep-dive-deterministic.yml` does.
  Result: 2 findings, both P3 (`[CI] No GitHub Actions workflows`,
  `[PORT] .gitattributes missing`); **gitleaks reported no secrets**.
- Lab: attempted, not used. The scoped `labvpn` endpoint health reports the lab
  peer handshake stale (~134 000 s) and no self-hosted runner is registered;
  `192.168.222.201:8722` is unreachable from this host. The deterministic lens
  therefore ran locally (`MODE=local`), which the runbook permits when the lab is
  genuinely down. Recorded, not faked.
- Machine-check hits were reviewed for real / false-positive / partial; severities
  reflect exploitability and host impact, not just scanner output.

## Findings

| Severity | Count | Summary |
|---|---|---|
| P0 | 0 | — |
| P1 | 1 | Unpinned, unverified LibreHardwareMonitor download loaded in-process |
| P2 | 4 | Unverified nvfancontrol + elevated task; unpinned pip from caller index; PII/serial over-collection with no redaction; destructive defaults |
| P3 | 2 | No CI/secret scanning; proprietary license on a public repo |

### SUPPLY-P1-001 — LibreHardwareMonitor: unpinned "latest" + no integrity, loaded in-process

`Get-Sensors.ps1` fetches `.../releases/latest/download/LibreHardwareMonitor.zip`,
extracts it, and `Add-Type -Path` loads `LibreHardwareMonitorLib.dll` as code in the
current PowerShell process. There is no pin, checksum, or signature check, and the
script is called by the default battery with guidance to run elevated. A mutable
upstream artifact therefore gets code execution on every benchmark host.

Fix: pin a release and verify a committed SHA-256 (or Authenticode signature) before
extraction/`Add-Type`; fail closed on mismatch.

### SUPPLY-P2-002 — nvfancontrol: unverified download installed as an elevated logon task

`Install-GpuFanCurve.ps1` downloads `nvfancontrol-rtx-win32-x64.zip` (tag 0.5.1) with
no integrity check, extracts it, and registers a scheduled task that runs the exe at
logon with `-RunLevel Highest`. Version pinning helps, but a tampered asset still
becomes persistent elevated execution. Fix: verify a pinned hash/signature before
extraction and before registering the task.

### SUPPLY-P2-003 — Runtime package install from an unpinned, caller-controlled index

`Invoke-GpuBench.ps1` runs `pip install --upgrade --index-url $TorchIndex torch` (no
version pin, no `--require-hashes`); `Run-WorkstationBench.ps1` exposes `-TorchIndex`;
`Invoke-RamBench.ps1` installs numpy unpinned. A hostile or typo'd index can supply
arbitrary wheels that execute in the benchmark process. Fix: pin versions+hashes and
allowlist the index.

### DATA-P2-004 — Hardware identifiers and PII collected with no redaction; sharing encouraged

`Get-SystemInfo.ps1` captures DIMM/GPU serials, GPU UUID, processor id, NIC MAC/IP,
BIOS and board data; `Get-EnvInfo.ps1` captures startup commands (user paths),
installed software and DNS servers. There is no `-Anonymize` mode, yet the README
presents committing/sharing a result as the normal workflow (and the repo is public).
A shared result leaks device-unique identifiers that fingerprint and correlate a
machine. Fix: add a redaction mode and make it the default for shareable output;
scrub the committed examples.

### SAFE-P2-005 — Destructive/host-impacting defaults; wrapper defeats the VRAM confirmation

The default one-command battery runs RAM integrity (fills 70 % of free RAM, 2
passes) and GPU integrity (~90 % of free VRAM) with no opt-in. `Invoke-GpuBench.ps1`
passes `--force` to every phase, which bypasses `gpu_bench.py`'s own
"continue? [y/N]" guard. Disk bench writes a 2 GiB temp file under `%TEMP%` and
deletes it only on normal completion. On a shared/working machine this can evict or
OOM other work, cause swap and SSD wear, and leave a large file if interrupted.
Fix: make integrity/soak opt-in, stop passing `--force` by default, bound the fill
percentages, and clean up in a `finally`/EXIT handler.

### CI-P3-006 — No CI, dependency automation, or secret scanning

There is no `.github/` directory, so changes to code that downloads and executes
third-party binaries merge with no lint/test/secret-scan gate. Fix: add a minimal
pinned CI workflow (PSScriptAnalyzer / compile / pytest + gitleaks) and Dependabot,
and make it required.

### GOV-P3-007 — Public repo, "PROPRIETARY AND CONFIDENTIAL" license

GitHub reports the repository as public while `LICENSE` and `README` declare it
proprietary and confidential. Fix: make the repo private or relicense for public
distribution and reconcile the docs.

## Benchmark-toolkit notes (task focus)

- **Download+execute paths reviewed**: `Get-Sensors.ps1` (LibreHardwareMonitor,
  unpinned, no hash), `Install-GpuFanCurve.ps1` (nvfancontrol, pinned tag, no hash,
  elevated persistence), `Invoke-GpuBench.ps1`/`Invoke-RamBench.ps1` (pip, unpinned,
  caller index). All three lack integrity verification.
- **HTTP endpoints used**: `download.pytorch.org` (pip, HTTPS, user-overridable),
  `github.com` release assets (HTTPS), `speed.cloudflare.com/__down|__up` (network
  bench, HTTPS, 100/25 MiB), `127.0.0.1:11434` (Ollama, localhost only). No
  plaintext HTTP endpoints; no credentials are sent.
- **Secrets**: gitleaks (pinned/verified) found none in the tree; no `.env`/key
  material is tracked.
- **Safe defaults**: the RAM integrity fill (70 % of free RAM), the forced ~90 %
  VRAM fill, and the 2 GiB disk temp file are the main host-safety concerns; the
  Unigine Valley phase requires a user-supplied binary and does not auto-download.
- **Data hygiene**: device serials/MAC/IP/software inventory are collected by
  default and are intended to be shared, with no redaction switch.

## Gate

No P0. One P1 (unverified code download loaded in-process) blocks a clean
supply-chain verdict for a toolkit that runs on managed workstations; P2 items are
high-value hardening. See `RELEASE_GATE.md`.
