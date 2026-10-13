# Design decisions (ADR log)

Short records for audit findings that are **decisions about posture**, not defects to
patch. Each one states the context, the decision and its status, the consequences,
and what would change the decision. Where a control does **not** exist, that is
written down rather than implied.

Scope: the control plane (`server/benchd.py`) and the committed audit artifacts
under `docs/audits/`.

---

## ADR-001 — No encryption at rest for the database, artifacts or backups (DR-P2-004)

**Context.** `benchd` keeps everything in one local SQLite file —
`server/data/benchd.sqlite` by default (`BENCHD_DB` overrides it). Run summaries,
the full per-run inventory (DIMM/GPU serials, MAC/IP addresses, hostname), phases,
logs, events, 4 Hz telemetry, and attached artifacts (base64 `content` in the
`artifacts` table) all live in that file. It runs in WAL mode
(`PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;`). There is **no encryption**:
no SQLCipher, no application-level encryption of artifact content, and no encrypted
backup step. `server/data/` is git-ignored so the file is never committed, but the
file itself is plaintext on disk. OS-level encryption is only *reported*: the
inventory reads BitLocker status (`bench/Get-EnvInfo.ps1`) for display, it is not
enforced.

**Threat model — who has host access.** The realistic actor is anyone with access to
the host filesystem: root in the lab LXC, anyone who can read the Proxmox storage
backing that container, or whoever holds a stolen disk/backup. The shared operator
token grants API access, not the database file. The database holds hardware
identifiers and inventory but no credentials — tokens and keys live in config files
(`server/benchd.conf`, `/etc/benchd.conf`, `~/.config/bench-agent.json`), not in the
database.

**Options.**
- **LUKS / OS-level full-disk encryption** on the host or the volume backing the
  container (and BitLocker on Windows runners). No code change; the stdlib-only
  rule is untouched.
- **SQLCipher** — encrypts the SQLite file itself, but needs a third-party library,
  which the control plane forbids (stdlib only, see `AGENTS.md`).
- **Application-level** — encrypt artifact blobs before storing them; needs key
  management inside `benchd` and does not cover the rest of the tables.

**Decision / status.** **Deferred.** Recommend **OS-level disk encryption** (LUKS on
the Proxmox storage; BitLocker on Windows runners) as the deployment control for the
lab. No encryption exists in the code today — do not describe the database or its
backups as encrypted.

**Consequences.** Data at rest is readable by anyone with host/filesystem access; a
disk or backup leak exposes the inventory. In exchange, the stdlib-only rule is
preserved and there is no in-app key-management burden.

**What would change the decision.** Moving the control plane off a single trusted
lab host; storing credentials or PII in the database; a compliance requirement for
encryption at rest; or handing the database/backups to a third party — then adopt
LUKS/BitLocker, or SQLCipher if a dependency becomes acceptable.

---

## ADR-002 — Hard deletes, with the command log as the audit trail (DATA-P3-003)

**Context.** Deletion is hard. `DELETE /api/v1/runs/<id>` removes the run row and its
`phases`, `telemetry`, `events`, `artifacts` and `logs`;
`DELETE /api/v1/runners/<id>` removes the runner plus its `commands` and `schedules`.
Retention (`retain_old_data`) deletes the per-sample telemetry/logs/events of
finished runs older than `BENCHD_RETENTION_DAYS` (default 30; `0` keeps all), and the
ephemeral-runner reaper deletes one-shot runners. There is no `deleted_at` column, no
tombstone, and no restore path — a deleted run simply disappears from History,
Trends, Compare and Fleet.

The audit trail for privileged actions is the **`commands` table**. Every
operator-issued command is recorded with `id`, `runner_id`, `run_id`, `kind`,
`payload`, `status`, `created`/`sent`/`acked` timestamps and `result`. The recorded
kinds are `start`, `stop`, `update`, `fan_curve`, `power_limit`, `restart`,
`shutdown`, `reboot`, `exec`, `shell`, `update_toolkit`, `iso_fetch`, `iso_write`,
`iso_write_slot`. So the **action** is retained even when the run it acted on is
deleted. Note the limit: the trail identifies the runner, action and outcome, but not
the individual operator — operators share one join token (per-user operator logins
are listed as future work in `docs/FUTURE_WORK.md`).

**Decision / status.** **Accepted — keep hard deletes for storage hygiene.** The
`commands` table is the audit trail for privileged actions; there is no soft-delete
layer.

**Trade-off.** A deleted run's rows are unrecoverable from the database; only the
runner-side `results/` copy remains, and it can be re-imported with
`server/import_results.py`. Without a tombstone you cannot prove after the fact that a
record existed or undo an accidental delete. The compensating controls are that
privileged actions stay in `commands` and every run's durable copy lives under
`results/` on the runner.

**What would change the decision.** A requirement to retain deleted run history for
compliance/forensics, or a need to restore an accidental delete — then add a
`deleted_at`/tombstone plus a soft-delete/restore path (and/or a periodic
`sqlite3 .backup`).

---

## ADR-003 — Single-node, single-process control plane (ARCH-P2-001)

**Context.** `benchd` is one process on one node: a stdlib `ThreadingHTTPServer`, a
single local SQLite file, and an **in-process** pub/sub `Bus` (a set of
`queue.Queue` subscribers) feeding SSE. The scheduler/reaper/retention loop is a
daemon thread inside the same process. State is not replicated. If the process or the
node dies, the fleet UI, live stream, scheduling and command queue all stop. Runners
**fail soft**: they keep benchmarking locally and report again once the server is
back.

**Existing mitigations.**
- **Backup** — the runner-side `results/` directories are the durable copy of every
  run; `server/import_results.py` re-imports them into a fresh database.
- **Retention** — per-sample tables of finished runs are swept after
  `BENCHD_RETENTION_DAYS` (default 30; `0` keeps all); run rows, summaries and
  artifacts are never deleted by retention.
- **Reapers** — `reap_stale_runs` fails runs whose runner stopped heartbeating,
  `reap_ephemeral_runners` drops one-shot registrations, and `reap_stale_commands`
  fails commands that were never acked — so state never sticks.
- **Bounded SSE** — each subscriber queue is capped (`queue.Queue(maxsize=1000)`) and
  the bus drops for a slow consumer rather than growing unbounded; the stream uses
  `Connection: close` and a 15 s keepalive.
- **SQLite WAL** for crash-tolerant, concurrent reads.

**Decision / status.** **Accepted — a single lab node.** The deployment target is an
unprivileged Proxmox LXC with `onboot=1`. Runners keep working offline, so the
control plane is a convenience plane, not a data-loss boundary.

**Consequences.** A node or process failure interrupts the live view, scheduling and
remote control until it restarts; there is no HA and a control-plane update briefly
restarts the service. In exchange the deployment is dependency-free and runs on the
lab box and the live USB alike.

**What would change the decision.** Multiple concurrent operators or regions, a need
for HA / zero-downtime control-plane updates, more than one node of writers, or a
requirement that a node failure not interrupt scheduling — then move to a shared or
replicated store (e.g. Postgres), an external queue/bus, and a leader-elected
scheduler.

---

## ADR-004 — Committed audit artifacts are a pinned, dated snapshot (INV-P3-003)

**Context.** `docs/audits/repo-deep-dive/20261007-1731-main-4ecee97/` holds a
published audit run against `main` @ `4ecee979cea2e09292446262b0a84a1cece8187d`
(short `4ecee97`). The artifacts (`findings.json`, the lens write-up,
`EXECUTIVE_SUMMARY.md`, the registers, `RELEASE_GATE.md`, `roadmap.md`/`patch_plan.md`,
`audit_manifest.json`) are a point-in-time snapshot. They are pinned to the commit
audited, not to current `HEAD`, and **this repository contains no tooling that
regenerates them** — they were produced by the separate
`repo-deep-dive-full-hardening-audit-pack` (v1.5.0, profile
`focused-security-supply-chain-ci`) via its `tools/publish_audit.py`.

**Decision / status.** **Accepted — keep them as a dated, commit-pinned historical
record.** Provenance and regeneration are documented in the
[README beside the artifacts](audits/repo-deep-dive/20261007-1731-main-4ecee97/README.md).

**Consequences.** The artifacts describe the repository as of `4ecee97` and will
drift from later commits; readers must check the commit before treating a finding as
current. Keeping them frozen is what makes them usable as baseline evidence.

**What would change the decision.** A need for a live/reproducible audit — then add
the audit pack as a submodule or a CI job that regenerates and diffs the run against
the committed copy.
