# Changelog

All notable changes to **bench-test** are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Releases are cut from git tags (`vX.Y.Z`) by `.github/workflows/release.yml`;
see [`docs/RELEASING.md`](docs/RELEASING.md) for how a release is prepared.
This file was added during remediation, so it does not reconstruct releases that
predate it — start from the tag you are reading.

## [Unreleased]

### Security

Security hardening is **in progress** and **not yet released**. Nothing below is
claimed as fixed: the entries describe work being prepared in remediation waves
on the remediation branch, tracked against the 2026-10-13 deep-dive audit of
`bench-test` at commit `b916709`, whose verdict at that commit was **NO-GO**.
Each item stays under this heading until it is merged, verified at the release
commit, and moved to a version section.

In-progress remediation workstreams:

- **Credential split and fail-closed auth** — stop using the shared
  `BENCHD_TOKEN` as both the runner join token and the operator bearer, stop
  baking it into public app bundles and the live ISO, and refuse to start (or bind
  loopback only) when no token is configured. Audit: `CHAIN-P0-001`,
  `SEC-P0-001`, `FINAL-P0-001`, `SEC-P1-001`, `FINAL-P1-001`.
- **Runner identity** — require proof of ownership for `machine_id` adoption so
  `POST /api/v1/register` cannot hand out another runner's API key. Audit:
  `API-P1-001`, `CHAIN-P1-001`, `FINAL-P1-002`.
- **Supply-chain integrity** — pin and verify third-party downloads
  (LibreHardwareMonitor, nvfancontrol) and make the ISO checksum verdict binding
  (fail closed on mismatch). Audit: `SC-P1-002`, `RES-P1-001`, `FINAL-P1-005`.
- **CI and release gating** — add a test / compile / lint gate and gate the
  release and build workflows on it. Audit: `CI-P1-002`, `BP-P1-001`,
  `TEST-P1-001`, `FINAL-P1-003`.
- **Data lifecycle and disaster recovery** — delete all run-scoped rows together
  (including logs), add retention for telemetry/logs/artifacts, and document a
  tested backup/restore path for `server/data/benchd.sqlite`. Audit:
  `DATA-P1-001`, `DR-P1-001`, `DR-P1-002`, `PERF-P1-001`.
- **Repository hygiene and privacy** — stop tracking generated `results/` output
  and scrub committed identifiers (usernames, absolute paths, hostnames) from the
  public repo. Audit: `HYGIENE-P1-001`, `PRIV-P1-001`, `FINAL-P1-006`.

### Added

- `docs/INCIDENT_RESPONSE.md` — what an incident is for this fleet, a P0–P3 /
  SEV1–4 taxonomy, roles, a first-30-minutes checklist, evidence handling, and a
  blameless postmortem template (audit `IR-P1-001`).
- `docs/RELEASING.md` — the intended release-notes and changelog flow.
- `CHANGELOG.md` — this file (audit `REL-P1-001`).

### Changed

- Nothing yet.

### Fixed

- Nothing yet.
