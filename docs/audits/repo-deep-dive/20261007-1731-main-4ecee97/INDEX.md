# Audit run 20261007-1731-main-4ecee97 - bench-test

Focused security / supply-chain / CI deep-dive (`main` @ `4ecee97`).

Counts: P0 x0, P1 x1, P2 x4, P3 x2

| File | Contents |
|---|---|
| lens_focused_security_supply_chain_ci.md | Full findings write-up |
| findings.json | Machine-readable findings |
| EXECUTIVE_SUMMARY.md | Summary |
| risk_register.md / follow_up_register.md | Findings register |
| RELEASE_GATE.md | Gate verdict |
| roadmap.md / patch_plan.md | Remediation plan |
| audit_manifest.json | Run manifest |

## P0/P1
- **SUPPLY-P1-001** (P1) Get-Sensors.ps1 downloads the mutable 'latest' LibreHardwareMonitor release and loads it in-process without integrity verification

