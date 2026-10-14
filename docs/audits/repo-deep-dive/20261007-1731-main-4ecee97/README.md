# Committed audit artifacts — provenance & regeneration

These files are a **published audit run**, kept as a dated snapshot. They are pinned
to the commit that was audited, **not** to the current `HEAD`.

| Field | Value |
|---|---|
| Pack | `repo-deep-dive-full-hardening-audit-pack` v1.5.0 |
| Profile | `focused-security-supply-chain-ci` |
| Target | `MaineCyberTech/bench-test` `main` @ `4ecee979cea2e09292446262b0a84a1cece8187d` (`4ecee97`) |
| Produced | 2026-10-07T17:35:45Z |
| Scaffolded by | `tools/publish_audit.py` (part of the audit pack — **not** this repo) |
| Secret scan | pass (7 rules) |

Contents: `findings.json`, `lens_focused_security_supply_chain_ci.md`,
`EXECUTIVE_SUMMARY.md`, `risk_register.md` / `follow_up_register.md`,
`RELEASE_GATE.md`, `roadmap.md` / `patch_plan.md`, `audit_manifest.json`, `INDEX.md`,
and `deterministic-findings.json`.

## How they were produced

The audit pack was run **read-only** against a clone of `bench-test` at the commit
above. The deterministic lens used the pack's `tools/deterministic_checks.py` (the lab
was unavailable for that run), and `tools/publish_audit.py` assembled this run folder
under `docs/audits/{name}/{run}/` and opened a draft PR adding it to the repo.

## How to regenerate

**There is no tooling in this repository that regenerates these artifacts.**
Regenerate by re-running the audit pack against a fresh checkout — from a checkout of
the `repo-deep-dive` pack (not from `bench-test`), using the pack's documented
command:

```bash
tools/publish_audit.py --repo <repo> --branch <default> --sha <full-sha> \
    --source-dir <dir with findings.json + FINDINGS.md> [--dry-run]
```

Re-running the audit against a newer commit produces a **new** run folder (e.g.
`docs/audits/repo-deep-dive/<new-run>/`). Do not overwrite this one: it records what
was true at `4ecee97`. See the pack's `runbooks/POST_AUDIT_PIPELINE.md` for the full
sequence.

The decision to keep these as a frozen, commit-pinned snapshot is recorded in
[`docs/DECISIONS.md`](../../../DECISIONS.md) (ADR-004, finding `INV-P3-003`).
