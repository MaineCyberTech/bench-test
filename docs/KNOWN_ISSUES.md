# Known issues & breaking changes

Open **P0 / P1 / P2** findings from the 2026-10-13 deep-dive audit of this
repository (run `bench-test-20261013-0819-full-main-b916709`, pinned to commit
`b916709`, VERSION `1.11.3`). The status shown is the deep-dive register's status
and is not re-verified here; the items are being remediated in the current PR
stack. Severity: **P0** = release-blocking, **P1** = high, **P2** = medium.

## Breaking changes

None recorded yet. Any change to the HTTP API, the token/auth model, the database
schema or the control-plane command kinds must add an entry here (with a migration
note) in the same change, so release notes have something to seed from.

## Known issues

### P0

- **CHAIN-P0-001** — Single shared secret is both the runner join token and the operator bearer, and it is baked into public app bundles / live ISOs -> anyone who downloads a release gets fleet-wide remote code execution (exec/shell) on every runner Status: Open (2026-10-13 deep-dive).
- **EXEC-P0-001** — Release gate = NO-GO: 3 unresolved P0s, all about one shared operator secret baked into public artifacts Status: Open (2026-10-13 deep-dive).
- **FINAL-P0-001** — One shared operator/join token is baked into public app bundles and the live ISO, so any release download yields fleet-wide remote code execution (merged across 10 domains) Status: Open (2026-10-13 deep-dive).
- **FINAL-P0-002** — A write-capable GitHub PAT is baked into the publicly downloadable live ISO, and runners auto-pull code from main, turning repo write into fleet code execution (merged across 4 domains) Status: Open (2026-10-13 deep-dive).
- **SC-P0-001** — GitHub PAT with Contents:Read/Write is baked into the publicly-downloadable live ISO Status: Open (2026-10-13 deep-dive).
- **SEC-P0-001** — Operator token (and a repo-write GitHub PAT) baked into public release artifacts Status: Open (2026-10-13 deep-dive).

### P1

- **ACM-P1-001** — Shared join token is the full operator credential and is baked into public releases Status: Open (2026-10-13 deep-dive).
- **ACM-P1-002** — machine_id adoption hands out another runner's API key Status: Open (2026-10-13 deep-dive).
- **ADMIN-P1-001** — Shared all-powerful operator token is distributed to every runner Status: Open (2026-10-13 deep-dive).
- **ADMIN-P1-002** — Runner identity takeover via machine_id Status: Open (2026-10-13 deep-dive).
- **AI-P1-001** — Test suite is documented as mandatory but no CI gate runs it, so an agent can land a red change on main Status: Open (2026-10-13 deep-dive).
- **API-P1-001** — POST /api/v1/register gives away another runner's api_key via unproven machine_id adoption Status: Open (2026-10-13 deep-dive).
- **ARCH-P1-001** — Single shared token is both the join credential and an all-powerful operator credential, and is shipped in public artifacts Status: Open (2026-10-13 deep-dive).
- **BP-P1-001** — No test/CI workflow exists, so no required status check can gate merges to main Status: Open (2026-10-13 deep-dive).
- **BP-P1-002** — Release workflow writes directly to main and cuts tags with no approval or documented bypass Status: Open (2026-10-13 deep-dive).
- **CHAIN-P1-001** — Runner identity takeover via /register machine_id adoption: any authenticated runner can claim another runner's row and is handed that row's api_key (the runner_id path was fixed, the machine_id path was not) Status: Open (2026-10-13 deep-dive).
- **CHAIN-P1-002** — A Contents:Read/Write GitHub PAT is baked into the public live ISO and every runner auto-pulls code from main at startup -> repo write access becomes code execution on the whole fleet (CI-to-production / supply-chain chain) Status: Open (2026-10-13 deep-dive).
- **CI-P1-001** — Build workflows bake live credentials into public release artifacts Status: Open (2026-10-13 deep-dive).
- **CI-P1-002** — No test/lint/secret-scan CI gate; nothing runs on pull_request Status: Open (2026-10-13 deep-dive).
- **CTR-P1-001** — Control-plane and upload bearer tokens are baked into the publicly downloadable live ISO image Status: Open (2026-10-13 deep-dive).
- **DATA-P1-001** — DELETE /api/v1/runs/<id> does not delete the run's logs; they stay orphaned in the DB and remain readable Status: Open (2026-10-13 deep-dive).
- **DR-P1-001** — No backup of the control-plane SQLite database, which is the single system of record Status: Open (2026-10-13 deep-dive).
- **DR-P1-002** — Restore is never exercised: no restore script, runbook, or test; the only import path cannot rebuild the DB Status: Open (2026-10-13 deep-dive).
- **EXEC-P1-001** — Cross-domain convergence: a single shared BENCHD_TOKEN serves both the low-trust join tier and the highest-privilege operator tier Status: Open (2026-10-13 deep-dive).
- **EXEC-P1-002** — Runner identity takeover via POST /register machine_id adoption is unfixed and gives an attacker another runner's API key Status: Open (2026-10-13 deep-dive).
- **EXEC-P1-003** — No CI test/quality gate: main merges and auto-releases land with zero automated verification Status: Open (2026-10-13 deep-dive).
- **EXEC-P1-004** — Disaster recovery is absent: the single SQLite system of record has no backup, no restore, and no exercise Status: Open (2026-10-13 deep-dive).
- **FINAL-P1-001** — Control plane fails open: with no BENCHD_TOKEN it serves the full operator API unauthenticated while bound to 0.0.0.0 (reproduced across 4 domains) Status: Open (2026-10-13 deep-dive).
- **FINAL-P1-002** — Any enrolled runner can take over another runner's identity via unproven machine_id adoption and be handed that runner's api_key (merged across 5 domains) Status: Open (2026-10-13 deep-dive).
- **FINAL-P1-003** — No CI test/lint/secret gate, while the release workflow self-modifies main and auto-tags with no approval (merged across 5 domains) Status: Open (2026-10-13 deep-dive).
- **FINAL-P1-004** — Data lifecycle is unmanaged: run delete leaves logs behind (reproduced), telemetry/artifacts have no retention, and there is no backup, restore, or incident plan (merged across 6 domains) Status: Open (2026-10-13 deep-dive).
- **FINAL-P1-005** — The most destructive operation fails open on integrity checks, and third-party code is fetched and executed without verification; no SBOM or license inventory exists (merged across 4 domains) Status: Open (2026-10-13 deep-dive).
- **FINAL-P1-006** — ~10,000 generated result files (~55 MB, with machine PII) are committed to a public repo despite a results/ ignore rule (merged across 6 domains) Status: Open (2026-10-13 deep-dive).
- **HYGIENE-P1-001** — 9,555 generated result files (~55 MB) committed under results/ Status: Open (2026-10-13 deep-dive).
- **INFRA-P1-001** — benchd fails open: with no BENCHD_TOKEN it serves the full operator API unauthenticated (reproduced) Status: Open (2026-10-13 deep-dive).
- **INFRA-P1-002** — The shared operator token is baked into publicly downloadable app bundles and the live ISO, and it is the same token that grants operator control Status: Open (2026-10-13 deep-dive).
- **INV-P1-001** — Shared control-plane token baked into public releases Status: Open (2026-10-13 deep-dive).
- **IR-P1-001** — No incident-response documentation, severity taxonomy, roles, or postmortem process exists anywhere in the repository Status: Open (2026-10-13 deep-dive).
- **PERF-P1-001** — Unbounded SQLite growth: telemetry/logs/events/artifacts have no retention, cap, or capacity signal Status: Open (2026-10-13 deep-dive).
- **PERF-P1-002** — Web dashboard re-fetches 300 full run rows and an unbounded /trends scan on every refresh tick Status: Open (2026-10-13 deep-dive).
- **PRIV-P1-001** — Unredacted hostnames and absolute home paths (usernames) committed to the public repository Status: Open (2026-10-13 deep-dive).
- **REL-P1-001** — No CHANGELOG and no release notes are generated for version tags; release.yml only cuts a bare tag Status: Open (2026-10-13 deep-dive).
- **RES-P1-001** — iso_fetch returns ok=True even when SHA256SUMS verification fails, so a bad ISO is accepted for a destructive write Status: Open (2026-10-13 deep-dive).
- **SBOM-P1-001** — No SBOM is produced for either shipped artifact set (PyInstaller app bundles and the live ISO) Status: Open (2026-10-13 deep-dive).
- **SBOM-P1-002** — Third-party binary LibreHardwareMonitor is redistributed in release bundles with no tracked license/attribution (MPL-2.0 compliance gap) Status: Open (2026-10-13 deep-dive).
- **SC-P1-001** — Control-plane operator token is baked into public app bundles Status: Open (2026-10-13 deep-dive).
- **SC-P1-002** — Third-party binaries are downloaded and executed without integrity verification (LibreHardwareMonitor 'latest', nvfancontrol) Status: Open (2026-10-13 deep-dive).
- **SEARCH-P1-001** — Deleting a run does not delete its console logs; they stay retrievable by run id Status: Open (2026-10-13 deep-dive).
- **SEC-P1-001** — Fail-open authentication when no token is configured Status: Open (2026-10-13 deep-dive).
- **SEC-P1-002** — /api/v1/register discloses other runners' API keys Status: Open (2026-10-13 deep-dive).
- **SECRET-P1-001** — Shared operator/join token is baked into public app release bundles Status: Open (2026-10-13 deep-dive).
- **SECRET-P1-002** — Write-capable GitHub PAT is baked into the public live-USB ISO Status: Open (2026-10-13 deep-dive).
- **TEST-P1-001** — No CI job runs the test suite or any compile/lint gate Status: Open (2026-10-13 deep-dive).

### P2

- **ACM-P2-001** — No lifecycle for runner API keys or the operator token: no expiry, rotation, or per-key revocation; keys stored in plaintext Status: Open (2026-10-13 deep-dive).
- **ACM-P2-002** — Control plane fails open: with no BENCHD_TOKEN every request is a full operator (RCE/reboot/delete), and the shipped unit binds 0.0.0.0 Status: Open (2026-10-13 deep-dive).
- **ACM-P2-003** — No credential lifecycle (expiry/rotation/revocation); keys plaintext Status: Open (2026-10-13 deep-dive).
- **ACM-P2-004** — Fail-open when no token is set, on a 0.0.0.0 unit Status: Open (2026-10-13 deep-dive).
- **ACM-P2-005** — No audit trail / actor attribution for privileged actions Status: Open (2026-10-13 deep-dive).
- **ACM-P2-006** — Operator token in URLs + wildcard CORS Status: Open (2026-10-13 deep-dive).
- **ADMIN-P2-001** — No audit log for privileged, destructive, or export actions; actions are unattributable because the token is shared Status: Open (2026-10-13 deep-dive).
- **ADMIN-P2-002** — Destructive actions are gated only in the browser; the API enforces nothing and deletes are irreversible with no undo Status: Open (2026-10-13 deep-dive).
- **ADMIN-P2-003** — No audit trail for privileged/delete/export actions Status: Open (2026-10-13 deep-dive).
- **ADMIN-P2-004** — Destructive actions gated only in the browser; deletes irreversible Status: Open (2026-10-13 deep-dive).
- **ADMIN-P2-005** — Fail-open when no token; docs bind 0.0.0.0 Status: Open (2026-10-13 deep-dive).
- **ADMIN-P2-006** — exec/shell is fleet-wide RCE with no approval and idle output is dropped Status: Open (2026-10-13 deep-dive).
- **ADMIN-P2-007** — Operator token in URL/localStorage; no expiry, rotation, or lockout Status: Open (2026-10-13 deep-dive).
- **AI-P2-001** — No PR, contribution, review or small-batch guidance; auto-tagging on direct main pushes means no human approval gate Status: Open (2026-10-13 deep-dive).
- **AI-P2-002** — No prompt-injection / untrusted-content guidance while agents are directed to read committed run logs and results Status: Open (2026-10-13 deep-dive).
- **AI-P2-003** — Agent instruction file misstates repository shape: 'a few runs' tracked vs 10,299 tracked result files (97.6% of the repo) Status: Open (2026-10-13 deep-dive).
- **AN-P2-001** — System inventory captures stable hardware identifiers and PII with no redaction switch, and committed examples expose a real GPU serial plus MAC/IP Status: Open (2026-10-13 deep-dive).
- **AN-P2-002** — Results (including full system inventory) are auto-published to a GitHub repository with no consent prompt and no minimization step Status: Open (2026-10-13 deep-dive).
- **AN-P2-003** — Control plane stores client IP, hostname and a persistent machine_id in SQLite with no retention or deletion policy Status: Open (2026-10-13 deep-dive).
- **API-P2-001** — Write endpoints perform no body validation: malformed JSON is treated as success Status: Open (2026-10-13 deep-dive).
- **API-P2-002** — Run creation is not idempotent: a repeated client-supplied run id returns 500 and leaks the DB schema Status: Open (2026-10-13 deep-dive).
- **API-P2-003** — DELETE /api/v1/runs/<id> leaves the run's log rows orphaned; the doc contract says '+data' Status: Open (2026-10-13 deep-dive).
- **API-P2-004** — Realtime SSE fan-out silently drops events once a subscriber's queue fills (no backpressure, no metric) Status: Open (2026-10-13 deep-dive).
- **ARCH-P2-001** — Single-node control plane with local SQLite and in-memory bus (SPOF, no backup) Status: Open (2026-10-13 deep-dive).
- **ARCH-P2-002** — Realtime SSE drops events silently and cannot resume Status: Open (2026-10-13 deep-dive).
- **ARCH-P2-003** — Operator token in URLs, logged in full, and wildcard CORS Status: Open (2026-10-13 deep-dive).
- **BP-P2-001** — Secret-bearing public-release workflows run with no environment approval gate Status: Open (2026-10-13 deep-dive).
- **BP-P2-002** — No Dependabot and all third-party actions are referenced by mutable tags Status: Open (2026-10-13 deep-dive).
- **BP-P2-003** — No documented branch/release/hotfix model or break-glass process Status: Open (2026-10-13 deep-dive).
- **CHAIN-P2-001** — The operator/exec chains are effectively silent: no audit trail or alerting for command issuance, no operator identity (single shared token), and exec output is dropped when no run is active Status: Open (2026-10-13 deep-dive).
- **CI-P2-001** — Actions pinned to mutable tags, not commit SHAs Status: Open (2026-10-13 deep-dive).
- **CI-P2-002** — release.yml self-pushes to main with contents:write and no environment/reviewer gate Status: Open (2026-10-13 deep-dive).
- **CI-P2-003** — runner_labels input allows arbitrary/self-hosted runners; secrets inline in shell Status: Open (2026-10-13 deep-dive).
- **CI-P2-004** — No Dependabot/secret scanning/SBOM/CODEOWNERS; branch protection undocumented Status: Open (2026-10-13 deep-dive).
- **CTR-P2-001** — CI ISO build runs a --privileged container with the host repository bind-mounted read-write Status: Open (2026-10-13 deep-dive).
- **CTR-P2-002** — Unpinned mutable base image tag (archlinux:latest) in the build container Status: Open (2026-10-13 deep-dive).
- **CTR-P2-003** — bench-agent service runs unsandboxed with no runtime hardening profile while executing control-plane shell commands Status: Open (2026-10-13 deep-dive).
- **DATA-P2-001** — No retention or aging policy for telemetry, logs, events or artifacts - the SQLite database grows without bound Status: Open (2026-10-13 deep-dive).
- **DATA-P2-002** — Schema declares no foreign keys and never enables FK enforcement (PRAGMA foreign_keys = 0) Status: Open (2026-10-13 deep-dive).
- **DATA-P2-003** — Migrations are best-effort additive ALTERs with no schema version tracking, rollback, or drift detection Status: Open (2026-10-13 deep-dive).
- **DATA-P2-004** — Phase writes use a non-atomic check-then-insert with no UNIQUE(run_id, seq), so concurrent or replayed posts can create duplicate phase rows Status: Open (2026-10-13 deep-dive).
- **DET-P2-003** — [PORT] CRLF committed for 11 file(s) (index has CRLF) Status: Open (2026-10-13 deep-dive).
- **DET-P2-005** — [SUPPLY] 9 GitHub Action ref(s) not pinned to a commit SHA Status: Open (2026-10-13 deep-dive).
- **DOC-P2-001** — Documented test command (`python tests/run_all.py`) fails on Windows: needs `windows-curses`, which the docs never say Status: Open (2026-10-13 deep-dive).
- **DOC-P2-002** — README release/version is stale and self-contradictory (v1.1.0 vs VERSION 1.11.5 vs the 1.11.3 written directly below it) Status: Open (2026-10-13 deep-dive).
- **DOC-P2-003** — docs/PHASE_TIMINGS.md reports the same preset on the same machine as both ~11.5 min and ~14.1 min, contradicting itself Status: Open (2026-10-13 deep-dive).
- **DOC-P2-004** — LICENSE declares the repo PROPRIETARY AND CONFIDENTIAL while README instructs the public to clone and download its public releases Status: Open (2026-10-13 deep-dive).
- **DR-P2-001** — Destructive deletes purge run data irreversibly with no snapshot, soft-delete, or confirmation Status: Open (2026-10-13 deep-dive).
- **DR-P2-002** — Schema has no version record and migrations are additive-only with no rollback path (bad-migration drill impossible) Status: Open (2026-10-13 deep-dive).
- **DR-P2-003** — Secrets have no backup/recovery or rotation procedure (per-runner keys live only in the DB) Status: Open (2026-10-13 deep-dive).
- **DR-P2-004** — No encryption at rest for the database, artifacts, or (hypothetical) backups Status: Open (2026-10-13 deep-dive).
- **EVOL-P2-001** — Adding a benchmark module/phase requires editing 6+ hardcoded lists; there is no registry or module template Status: Open (2026-10-13 deep-dive).
- **EVOL-P2-002** — No CI workflow runs the test suite, so platform changes are merged unverified Status: Open (2026-10-13 deep-dive).
- **EVOL-P2-003** — Schema 'migration' is unversioned, additive-only column checks run on every startup Status: Open (2026-10-13 deep-dive).
- **EVOL-P2-004** — Authorization is a hardcoded two-tier boolean checked per handler; no role/permission model to extend Status: Open (2026-10-13 deep-dive).
- **EVOL-P2-005** — Telemetry/summary metric catalogue is duplicated across Python and JavaScript with no single source of truth Status: Open (2026-10-13 deep-dive).
- **EXEC-P2-001** — Evidence binding gap: the run is pinned to b916709 but the working tree advanced 5 commits and findings reproduce at 60ab90eb/72c18734 Status: Open (2026-10-13 deep-dive).
- **EXEC-P2-002** — Published-artifact and privacy hygiene: ~9.5k generated files are tracked on a public repo with host/user identifiers Status: Open (2026-10-13 deep-dive).
- **FEAT-P2-001** — Deleting a run orphans its logs rows Status: Open (2026-10-13 deep-dive).
- **FILE-P2-001** — iso_fetch reports ok:true and acks "done" even when the ISO checksum is missing or mismatched Status: Open (2026-10-13 deep-dive).
- **FILE-P2-002** — Artifact upload has no MIME/extension/size validation, no content-scanning hook, and no per-run quota Status: Open (2026-10-13 deep-dive).
- **FILE-P2-003** — All file downloads are authorized by one static shared token carried in the query string, with no expiry, rotation, or revocation Status: Open (2026-10-13 deep-dive).
- **HYGIENE-P2-001** — History is 94% auto-generated `bench:` commits Status: Open (2026-10-13 deep-dive).
- **HYGIENE-P2-002** — Duplicate nested `results/results/` tree Status: Open (2026-10-13 deep-dive).
- **HYGIENE-P2-003** — README release drift (v1.1.0 vs VERSION 1.11.3) Status: Open (2026-10-13 deep-dive).
- **HYGIENE-P2-004** — Stdlib-only test suite fails on Windows system Python Status: Open (2026-10-13 deep-dive).
- **INFRA-P2-001** — Documented BENCHD_DB config key is never read; DB path is hard-coded and has no backup/retention Status: Open (2026-10-13 deep-dive).
- **INFRA-P2-002** — Operator token is transported in URLs and kept in browser localStorage; no TLS/reverse proxy exists in the repo Status: Open (2026-10-13 deep-dive).
- **INFRA-P2-003** — benchd systemd unit runs as root on 0.0.0.0 with no User/CapabilityBoundingSet and no readiness probe Status: Open (2026-10-13 deep-dive).
- **INFRA-P2-004** — Deployment is an unversioned manual pct snippet that does not ship VERSION or write a commit stamp, breaking the fleet staleness comparison Status: Open (2026-10-13 deep-dive).
- **INV-P2-001** — Generated run output committed en masse Status: Open (2026-10-13 deep-dive).
- **INV-P2-002** — Recursively nested/duplicated result trees committed Status: Open (2026-10-13 deep-dive).
- **INV-P2-003** — No dependency manifest or lockfile Status: Open (2026-10-13 deep-dive).
- **INV-P2-004** — README version stale vs VERSION and tags Status: Open (2026-10-13 deep-dive).
- **IR-P2-001** — No monitoring or alerting: failures are only visible when a human watches the web dashboard Status: Open (2026-10-13 deep-dive).
- **IR-P2-002** — No backup or restore procedure for the control-plane SQLite database (all fleet history, artifacts and per-runner keys) Status: Open (2026-10-13 deep-dive).
- **IR-P2-003** — No operator audit log: privileged commands and destructive deletes carry no actor identity or durable trail Status: Open (2026-10-13 deep-dive).
- **IR-P2-004** — No data-breach or vulnerability-disclosure process, while a public repo bakes a world-readable operator token into ISO/app artifacts Status: Open (2026-10-13 deep-dive).
- **MOB-P2-001** — Primary tab bar cannot fit a phone: it is a non-wrapping flex row with no horizontal scroll Status: Open (2026-10-13 deep-dive).
- **MOB-P2-002** — Fixed multi-column grids never collapse on phones, forcing horizontal overflow or unreadable cells Status: Open (2026-10-13 deep-dive).
- **MOB-P2-003** — Interactive controls are below the 44x44px minimum touch target Status: Open (2026-10-13 deep-dive).
- **MOB-P2-004** — Header and filter bars clip or overflow on narrow screens instead of wrapping Status: Open (2026-10-13 deep-dive).
- **NOTIF-P2-001** — No out-of-band alert/notification delivery for critical health verdicts Status: Open (2026-10-13 deep-dive).
- **OBS-P2-001** — No alerting or uptime monitoring for the control plane or for failed runs Status: Open (2026-10-13 deep-dive).
- **OBS-P2-002** — No structured logging and no request IDs / correlation IDs Status: Open (2026-10-13 deep-dive).
- **OBS-P2-003** — Server-side errors are swallowed; no error tracking or stack traces Status: Open (2026-10-13 deep-dive).
- **OBS-P2-004** — /health conflates liveness and readiness and performs a DB query plus a network call Status: Open (2026-10-13 deep-dive).
- **OBS-P2-005** — No runtime/service metrics and no monitoring-of-the-monitoring Status: Open (2026-10-13 deep-dive).
- **ORCH-P2-001** — Published audit run uses two conflicting finding-ID schemes Status: Open (2026-10-13 deep-dive).
- **ORCH-P2-002** — risk_register.md is a verbatim copy of follow_up_register.md Status: Open (2026-10-13 deep-dive).
- **ORCH-P2-003** — Run INDEX and audit_manifest.json omit the deterministic lens artifacts Status: Open (2026-10-13 deep-dive).
- **PERF-P2-001** — Per-run detail, stats and CSV endpoints load and re-parse every telemetry row on the request thread Status: Open (2026-10-13 deep-dive).
- **PERF-P2-002** — Realtime path scales by unbounded threads and broadcasts whole telemetry batches to every subscriber Status: Open (2026-10-13 deep-dive).
- **PERF-P2-003** — CI rebuilds a multi-GB ISO and both app bundles on every bench/** or live/** push with no dependency caching Status: Open (2026-10-13 deep-dive).
- **PERF-P2-004** — Heartbeat/liveness path performs a blocking GitHub compare call with an 8 s timeout, longer than the runner's 6 s client timeout Status: Open (2026-10-13 deep-dive).
- **PRIV-P2-001** — Collectors capture stable hardware identifiers and PII with no redaction mode, and the uploader publishes the whole results directory to public GitHub Status: Open (2026-10-13 deep-dive).
- **PRIV-P2-002** — No attributable audit trail for privileged/admin actions (shared operator token, commands record no actor) Status: Open (2026-10-13 deep-dive).
- **PRIV-P2-003** — Deletion is incomplete: deleting a run leaves its console logs, and deleting a runner leaves its runs/telemetry/inventory Status: Open (2026-10-13 deep-dive).
- **PRIV-P2-004** — No retention or scheduled-purge policy for telemetry, logs, artifacts and the SQLite database Status: Open (2026-10-13 deep-dive).
- **PRIV-P2-005** — Per-runner API keys are stored in plaintext in SQLite and looked up by direct comparison Status: Open (2026-10-13 deep-dive).
- **REL-P2-001** — README's release/version strings are stale and self-contradictory, so the shipped docs disagree with VERSION Status: Open (2026-10-13 deep-dive).
- **REL-P2-002** — Release artifacts are tagged by workflow run number, not by version or commit, so notes cannot be tied to a build Status: Open (2026-10-13 deep-dive).
- **REL-P2-003** — Changelog input is unenforced: no PR/issue templates or CONTRIBUTING, and commit subjects are 94% auto-generated noise Status: Open (2026-10-13 deep-dive).
- **REL-P2-004** — No upgrade/rollback notes are possible: schema changes are unversioned additive ALTERs and the deploy does not ship VERSION Status: Open (2026-10-13 deep-dive).
- **REL-P2-005** — Release notes carry no generation metadata (commit/version/date), and the repo's only release-note-like artifact is a one-line verdict Status: Open (2026-10-13 deep-dive).
- **REL-P2-006** — No separate user/admin/operator/security note tracks; operator-mandated changes (token handling, exec/shell, deletes) have no release channel Status: Open (2026-10-13 deep-dive).
- **RES-P2-001** — A completed run is recorded as 'failed' and its summary lost when the final finish post fails (no retry/idempotency) Status: Open (2026-10-13 deep-dive).
- **RES-P2-002** — Backfill importer is not idempotent (re-running duplicates every run) and --reset deletes all history Status: Open (2026-10-13 deep-dive).
- **RES-P2-003** — agent 'start' has a TOCTOU race that can run two benchmark batteries concurrently on one machine Status: Open (2026-10-13 deep-dive).
- **RES-P2-004** — Documented rollback paths (A/B slot auto-revert, live toolkit self-update) are never exercised, and a bad toolkit update has no rollback Status: Open (2026-10-13 deep-dive).
- **SBOM-P2-001** — No package manifest or lockfile anywhere; dependencies are installed unpinned at runtime, so no reproducible dependency set exists to inventory Status: Open (2026-10-13 deep-dive).
- **SBOM-P2-002** — GitHub Actions dependencies are mutable major tags and there is no Dependabot, dependency-review or vulnerability scanning Status: Open (2026-10-13 deep-dive).
- **SBOM-P2-003** — No release provenance: no build attestation/signing, and app/ISO release tags are run-number based rather than commit-bound Status: Open (2026-10-13 deep-dive).
- **SBOM-P2-004** — No license policy, allow/deny list, CI license gate or exception process Status: Open (2026-10-13 deep-dive).
- **SC-P2-001** — No dependency manifest or lockfile; compute deps are pip-installed unpinned, sometimes with an extra index Status: Open (2026-10-13 deep-dive).
- **SC-P2-002** — GitHub Actions are pinned to mutable major tags and there is no dependency automation or dependency-review/license gate Status: Open (2026-10-13 deep-dive).
- **SC-P2-003** — bootstrap.py extracts a downloaded tarball without a safe extraction filter and its integrity check fails open Status: Open (2026-10-13 deep-dive).
- **SEARCH-P2-001** — No retention policy or cleanup job for logs, telemetry, artifacts or events - run data is kept forever Status: Open (2026-10-13 deep-dive).
- **SEARCH-P2-002** — Deleting a runner orphans its runs, telemetry, logs and artifacts Status: Open (2026-10-13 deep-dive).
- **SEARCH-P2-003** — Shared operator token is accepted in the URL query string and compared non-constant-time Status: Open (2026-10-13 deep-dive).
- **SEC-P2-001** — Operator token in the URL query + cleartext logs over HTTP Status: Open (2026-10-13 deep-dive).
- **SEC-P2-002** — Wildcard CORS on authenticated endpoints and SSE Status: Open (2026-10-13 deep-dive).
- **SEC-P2-003** — No rate limiting or lockout Status: Open (2026-10-13 deep-dive).
- **SEC-P2-004** — Unbounded request body (DoS) Status: Open (2026-10-13 deep-dive).
- **SECRET-P2-001** — Placeholder token 'change-me' is accepted silently; no token strength/startup validation Status: Open (2026-10-13 deep-dive).
- **SECRET-P2-002** — No secret rotation / revocation / break-glass runbook or inventory Status: Open (2026-10-13 deep-dive).
- **TEST-P2-001** — Releases, tags and public ISO/app artifacts are published with no test or smoke verification of the artifact Status: Open (2026-10-13 deep-dive).
- **TEST-P2-002** — Documented test command fails on Windows: suite needs third-party windows-curses but is described as stdlib-only Status: Open (2026-10-13 deep-dive).
- **TEST-P2-003** — The 23 PowerShell battery scripts and the web dashboard are only validated by substring assertions, never executed or linted Status: Open (2026-10-13 deep-dive).
- **USE-P2-001** — Control-plane commands report success on enqueue and never surface the runner's ack, so a rejected start/stop/exec looks successful Status: Open (2026-10-13 deep-dive).
- **USE-P2-002** — Web Start-run form has no input validation; blank/zero/negative durations and sizes are sent silently Status: Open (2026-10-13 deep-dive).
- **USE-P2-003** — GUI 'Clean up environment' deletes the cached runner interpreter with no confirmation and no in-run guard Status: Open (2026-10-13 deep-dive).
- **USE-P2-004** — Remote terminal / ad-hoc exec is only offered while a run is live, so idle runners cannot be diagnosed from the dashboard Status: Open (2026-10-13 deep-dive).
- **UX-P2-001** — Web dashboard's primary interactions are mouse-only (runner cards, sortable headers, table rows) Status: Open (2026-10-13 deep-dive).
- **UX-P2-002** — Modal dialog has no dialog semantics, no focus trap, and no focus restore Status: Open (2026-10-13 deep-dive).
- **UX-P2-003** — No ARIA anywhere in the web dashboard; tab strip and live SSE updates are not exposed to assistive tech Status: Open (2026-10-13 deep-dive).
- **UX-P2-004** — Canvas charts have no accessible alternative and their tooltips are pointer-only Status: Open (2026-10-13 deep-dive).
- **UX-P2-005** — Tk GUI suppresses the keyboard focus indicator and has no keyboard accelerators Status: Open (2026-10-13 deep-dive).

## Reporting / status

- A finding is only `verified-fixed` with an artifact captured at the fixing
  commit; until then it stays listed here.
- Security vulnerabilities are reported privately, not in a public issue — see
  [`SECURITY.md`](../SECURITY.md).
- Data-handling and retention details for the items above are in
  [`PRIVACY.md`](PRIVACY.md); field-level data is mapped in [`DATA.md`](DATA.md).