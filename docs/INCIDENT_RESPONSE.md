# Incident response — bench-test fleet & releases

This runbook is for **bench-test** operators: the people who run the control
plane (`server/benchd.py`), the runners (workstation agents, the GUI/TUI
dashboards, and booted **live USB** sticks), and the published artifacts (app
bundles and live ISOs).

It exists because the control plane is **not a passive dashboard**: an operator
token can run `exec`/`shell` on a runner, write a raw block device
(`iso_write`/`iso_write_slot`), and `reboot`/`shutdown` machines (see
[`CONTROL_PLANE.md`](CONTROL_PLANE.md)). A leaked credential or a wrong command
is therefore a fleet incident, not a UI mistake. It is also the case today that
the shared `BENCHD_TOKEN` is **both** the runner join token and the operator
bearer, and it is baked into public app bundles and the live ISO — so treat any
distributed copy as compromised (audit `CHAIN-P0-001`, `SEC-P0-001`).

## 1. What counts as an incident here

Any of these starts this process:

- **Credential exposure** — `BENCHD_TOKEN` (from `server/benchd.conf`,
  `/etc/benchd.conf`, `live/private/`, or a downloaded app bundle / ISO), a
  per-runner API key (`~/.config/bench-agent.json`), or the GitHub upload token
  baked into an ISO is leaked, shared, or found in a public artifact.
- **Unauthorized operator action** — a command you did not issue: `exec`/`shell`,
  `iso_write`/`iso_write_slot`, `reboot`, `shutdown`, `update`/`update_toolkit`,
  or a delete of a run/runner.
- **Control-plane loss** — `benchd` is down, the host is gone, or the SQLite
  database (`server/data/benchd.sqlite`) is lost, corrupted, or disk-full.
- **A runner running unexpected code** — an agent/dashboard is executing a commit
  you did not expect, or a live stick booted an image you did not build.
- **Unsafe host impact** — a benchmark run evicts/OOMs other workloads, leaves a
  large temp file, or a soak drives a machine to a thermal/hardware fault.
- **Data exposure** — a public artifact (`results/`, `examples/`, a release, a
  bundle, an ISO) carries hostnames, usernames, serials, MACs/IPs, or tokens.
- **Release integrity** — a published ISO/bundle fails its checksum, or a tag
  points at a commit you did not intend to ship.

## 2. Severity taxonomy

The audit vocabulary is **P0–P3**; map it to an incident severity (SEV) so the
two line up. When in doubt, start one level higher and downgrade with evidence.

| SEV | Audit | Meaning | Examples | Target first response |
|---|---|---|---|---|
| **SEV1** | P0 | Fleet-wide compromise, or a reachable path to it | Operator token leaked / baked into a public release; `exec`/`iso_write`/`reboot` reachable by an outsider; repo-write credential in a public ISO | Immediately — page the operator on call |
| **SEV2** | P1 | Serious, contained to one host or one credential tier | A single runner key taken over via `/register`; one host running unknown code; control-plane DB loss; no backup/restore path exercised | Same working day |
| **SEV3** | P2 | Degraded but contained; no confirmed compromise | Missing retention / unbounded SQLite growth; no audit trail for privileged actions; monitoring/alerting gaps | Next planned window |
| **SEV4** | P3 | Documentation, hygiene, or process only | Missing runbook; stale docs; version-string drift | Backlog, scheduled |

## 3. Roles

Name people to **roles**, not to tools. On a small team one person can hold
several hats, but the incident commander must be one person.

| Role | Owns | In an incident |
|---|---|---|
| **Operator** | The control plane: `server/benchd.py`, its token (`server/benchd.conf` / `/etc/benchd.conf`), the SQLite DB (`server/data/`), and the command surface | Rotates the token, stops/rebinds `benchd`, revokes runners, issues or blocks commands |
| **Runner owner** | One or more machines and/or live USB sticks: the agent (`bench/agent.py`), dashboards, and their per-runner keys | Takes affected runners off the fleet, preserves their local state, re-enrolls cleanly |
| **Release owner** | `VERSION`, tags, app bundles, live ISOs, and the CI that builds them | Freezes publishing, rotates the CI-baked secrets, rebuilds/re-releases, records the affected versions |
| **Incident commander (IC)** | The response itself | Decides severity, assigns tasks, approves containment/recovery, owns comms and the postmortem |
| **Comms lead** (larger incidents) | One channel, one message | Keeps status updates factual and on a fixed cadence |
| **Scribe** (larger incidents) | The timeline | Timestamps every action and decision as it happens |

## 4. First 30 minutes checklist

Work top to bottom; record times as you go (the postmortem needs them).

1. **Declare.** Pick a severity from the table above, name an incident commander,
   and open one comms channel. Do not investigate alone in silence.
2. **Contain the control plane.**
   - Rotate `BENCHD_TOKEN`: generate a new value
     (`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`) and install
     it with the documented pattern from `CONTROL_PLANE.md`:
     `sudo install -m600 /dev/stdin /etc/benchd.conf`, then restart the service
     (`sudo systemctl restart benchd`, if you installed `server/benchd.service`)
     or restart the `python3 server/benchd.py …` process.
   - **Assume every distributed copy is compromised.** The old token is baked into
     public app bundles and the live ISO via the `BENCH_AGENT_CONF` secret, so
     rotating only the server is not enough: rotate the CI secret and rebuild /
     re-release the affected artifacts. Until the credential split
     (`CHAIN-P0-001`) lands, the join token and the operator bearer are the same
     secret — treat any leak as a fleet-wide one.
3. **Revoke / rotate runner keys.** Per-runner keys are issued at
   `POST /api/v1/register` and stored in `~/.config/bench-agent.json`. There is no
   key-rotation or expiry endpoint yet (audit `ACM-P2-001`), so the current lever
   is: as an operator, delete the affected runner
   (`DELETE /api/v1/runners/<id>`, which also drops its commands and schedules),
   then let the machine re-enroll for a fresh key. Do this for every runner you
   cannot account for.
4. **Take the control plane off the network.** Stop `benchd`
   (`sudo systemctl stop benchd`) or restart it bound to loopback
   (`python3 server/benchd.py --host 127.0.0.1 --port 8722`), and block port 8722
   from anything but your incident workstation. Runners fail soft and keep
   benchmarking locally, so this does not lose runs.
5. **Preserve evidence before you clean up.** Copy, read-only, off the affected
   host:
   - the database `server/data/benchd.sqlite` **together with its `-wal` and
     `-shm` sidecars** (the DB runs in WAL mode), so the copy is consistent;
   - `server/benchd.conf` and, on a stick or build host, `live/private/`;
   - the server log (`journalctl -u benchd`, or the terminal `benchd` was started
     from) and any runner console output.
   Note that a documented, tested backup/restore path is a **known gap**
   (audit `DR-P1-001`, `DR-P1-002`) — capture the files now rather than relying on
   a backup that does not exist yet.
6. **Notify.** Tell the runner owners and the release owner which machines and
   artifacts are in scope, and set the next status-update time.

## 5. Evidence handling

- **Read-only, hashed, off-host.** Copy evidence to a separate location and
  record a SHA-256 (`sha256sum <file>`) plus who copied it and when. Do not edit
  the originals.
- **One writer.** The scribe is the only person adding to the timeline; everyone
  else reports to the IC.
- **No secrets in the record.** `server/benchd.conf`, `live/private/`,
  `~/.config/bench-agent.json` and the DB contain credentials. Never commit them
  or paste them into tickets; reference the path and the credential **type**, and
  redact the value. This applies to `BENCHD_TOKEN`, per-runner `api_key`, and any
  `GITHUB_TOKEN`/upload token.
- **Keep the DB consistent.** A raw copy of `benchd.sqlite` alone can miss
  committed WAL pages; include `-wal`/`-shm` or take the copy while `benchd` is
  stopped.
- **Retain until the postmortem is closed**, then dispose per your data policy.

## 6. Blameless postmortem

Hold a postmortem for every SEV1/SEV2, and for any SEV3 you choose. It is
**blameless**: the goal is to fix the system, not the person. Assume everyone
acted reasonably with the information they had, and look for the missing guard,
signal, or instruction.

Copy this template into the incident record and fill it in:

```markdown
# Postmortem — <short title>

- Date / duration:
- Severity (SEV / audit P-level):
- Incident commander:
- Authors / reviewers:
- Affected systems (runners, control plane, artifacts):

## Summary
One paragraph a newcomer can understand: what happened and what it affected.

## Impact
Who and what was affected, for how long, and the blast radius
(how many runners, which releases/ISOs, which data).

## Detection
How we found out, how long it took, and what signal (if any) fired.
If a human had to notice it on the dashboard, say so.

## Timeline
Timestamped actions and decisions (from the scribe's record).

## Root cause and contributing factors
The technical cause plus the conditions that let it happen
(missing guard, shared credential, no test, no alert, unclear doc).

## What went well
Guards or habits that limited the damage.

## What went wrong
Gaps in detection, containment, or communication.

## Action items
| # | Action | Owner | Due | Tracking (finding / issue) |
|---|--------|-------|-----|----------------------------|
| 1 |        |       |     |                            |

## Appendix
Evidence paths, hashes, and links. Redact all secret values.
```

## 7. Related documents

- [`CONTROL_PLANE.md`](CONTROL_PLANE.md) — auth model, deployment, operating
  notes (reaper, staleness, token rotation mechanics).
- [`DATA.md`](DATA.md) — what a run stores, where it lands, and which fields are
  sensitive.
- [`RELEASING.md`](RELEASING.md) — release/tag flow and where to record operator
  actions for a release.
- `README.md` § Security and `AGENTS.md` § Security — the public-repo and
  baked-token caveats.

> Traceability: this runbook addresses audit finding **IR-P1-001**. The gaps it
> references are tracked as `CHAIN-P0-001` / `SEC-P0-001` (shared baked token),
> `ACM-P2-001` (no key rotation), and `DR-P1-001` / `DR-P1-002` (no tested
> backup/restore).
