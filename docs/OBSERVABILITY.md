# Observability

How `benchd` (the control plane) tells you what it is doing: structured logs,
request correlation, liveness vs readiness, runtime metrics, optional out-of-band
alerting, and the per-run telemetry cache. Stdlib only — no logging framework, no
metrics library, no exporter sidecar.

See also: [`CONTROL_PLANE.md`](CONTROL_PLANE.md) for the API surface.

## Structured logging

Every server-side log line is a single JSON object on **stderr**, one per line:

```json
{"ts": 1791910696.579, "level": "error", "msg": "api_get failed",
 "request_id": "1887cb9d1b7545ad84a50b44e4400584",
 "extras": {"route": "/api/v1/state", "error": "RuntimeError: boom",
            "traceback": "Traceback (most recent call last):\n..."}}
```

| Field | Meaning |
|---|---|
| `ts` | Unix epoch seconds (float) |
| `level` | `info` \| `warning` \| `error` |
| `msg` | Short human-readable event name |
| `request_id` | The correlation id of the request (see below) |
| `extras` | Free-form object: `route`, `error`, `traceback`, `run_id`, … (present only when non-empty) |

The per-request access line (`log_message`) is also emitted as JSON, with the
same shape, and the query string is still redacted so a `?token=` never reaches
the log. `--quiet` suppresses the access line only; error lines are always
emitted.

### What is logged

* **Access** — every request (method, path, status), unless `--quiet`.
* **Errors** — an unexpected exception in a request path is logged at `error`
  with the route and a full `traceback`, and the client still receives the same
  short `500` body it always did. The traceback is **never** sent to the client.
* **Readiness failures** — a failed `/ready` DB check is logged at `error` with
  the traceback.
* **Audit writes / response writes / SSE failures** — a failure in any of these
  best-effort paths is logged instead of being silently swallowed.

## Request-id contract

* Every response carries an **`X-Request-ID`** header.
* If the client sends a sane `X-Request-ID` (`[A-Za-z0-9._:-]{1,128}`), it is
  honoured and echoed back unchanged.
* Otherwise the server mints one (`uuid4().hex`) and echoes that.
* The id is stamped on every JSON log line produced while handling the request,
  so a single request can be reconstructed from the log stream by grepping for
  its `request_id`.

Clients that already have a trace id (a proxy, a runner, a test) should send it
so the control plane's logs join up with theirs.

## `/health` vs `/ready`

The two endpoints answer different questions and are deliberately different in
cost:

| | `GET /health` | `GET /ready` |
|---|---|---|
| Question | "is the process alive?" | "can this instance serve?" |
| I/O | **none** — no DB, no network | SQLite read **and** write, plus the git/version compare |
| Status | always `200` | `200` when ready, `503` when not |
| Cost | constant | may block on the DB / GitHub (8 s timeout) |

`/health` is the **liveness** probe. Its fields come from a snapshot refreshed by
a background thread (every 5 s), so a probe with a short timeout can never be
killed by a SQLite lock or an unreachable GitHub:

```json
{"ok": true, "time": 1791910696.5, "git": "b916709", "version": "1.4.2",
 "protocol": 1, "latest": "b916709",
 "capacity": {"rows": {"runs": 3, "telemetry": 120, "logs": 400,
                       "events": 2, "artifacts": 0},
              "db_bytes": 262144, "retention_days": 30},
 "runners": 2}
```

`/ready` is the **readiness** probe. It checks that the database is readable and
writable, then reports the deployed vs latest-main comparison:

```json
{"ok": true, "db": {"ok": true}, "version": "1.4.2", "git": "b916709",
 "latest": "b916709", "compare": "identical", "protocol": 1,
 "time": 1791910696.5}
```

A failing DB check yields `503` and `{"ok": false, "db": {"ok": false,
"error": "..."}}`.

Point an **uptime** probe (a short timeout) at `/health` and a **load-balancer /
orchestrator** readiness probe at `/ready`.

## Metrics

`GET /metrics` returns Prometheus text exposition (stdlib only, no dependency).
It is public so a scraper does not need the operator token.

| Metric | Type | Meaning |
|---|---|---|
| `benchd_uptime_seconds` | gauge | Seconds since this server instance started |
| `benchd_requests_total{class="1xx".."5xx"}` | counter | HTTP responses by status class |
| `benchd_auth_failures_total` | counter | Authentication failures (`401`) |
| `benchd_rate_limit_rejections_total` | counter | Requests rejected by the rate limiter (`429`) |
| `benchd_audit_rows` | gauge | Rows in the operator audit trail |
| `benchd_command_queue_depth` | gauge | Commands queued and not yet sent to a runner |
| `benchd_alerts_fired_total` | counter | Alert webhook deliveries (see below) |
| `benchd_alerts_suppressed_total` | counter | Alerts dropped by the per-kind rate limit |
| `benchd_alerts_failed_total` | counter | Alert webhook POSTs that failed |

Counters are in-process and reset on restart (they are not persisted). The
request/status counters are recorded on the response path; the two DB gauges are
read when the endpoint is scraped (it is not on the liveness path).

```console
$ curl -s http://127.0.0.1:8722/metrics
# HELP benchd_uptime_seconds Seconds since the control plane started.
# TYPE benchd_uptime_seconds gauge
benchd_uptime_seconds 42.117
# HELP benchd_requests_total HTTP requests by status class.
# TYPE benchd_requests_total counter
benchd_requests_total{class="1xx"} 0
benchd_requests_total{class="2xx"} 31
benchd_requests_total{class="3xx"} 0
benchd_requests_total{class="4xx"} 2
benchd_requests_total{class="5xx"} 0
benchd_auth_failures_total 1
benchd_rate_limit_rejections_total 0
benchd_audit_rows 7
benchd_command_queue_depth 0
benchd_alerts_fired_total 1
benchd_alerts_suppressed_total 0
benchd_alerts_failed_total 0
```

## Alerting

`benchd` can POST a small JSON alert to an optional webhook so a failure reaches
an operator **out of band** (a chat relay, a pager, an automation endpoint) even
when nobody is looking at the dashboard. It is **disabled by default**: nothing is
sent unless a URL is configured.

```console
$ benchd --alert-webhook https://alerts.example/benchd \
         --alert-db-bytes $((5 * 1024 * 1024 * 1024))
```

Config (first wins): `--alert-webhook` flag, `$BENCHD_ALERT_WEBHOOK`,
`server/benchd.conf` / `~/.config/benchd.conf` / `/etc/benchd.conf`. The capacity
threshold is `--alert-db-bytes` / `$BENCHD_ALERT_DB_BYTES` (bytes; `0`, the
default, disables the capacity alert).

| Kind | Fires when |
|---|---|
| `run_failed` | a run finishes with a failed status (`failed`/`error`/`aborted`/…) or a failing verdict (`fullsoak.result.verdict.clean == false`) |
| `readiness` | a `/ready` probe fails its DB check (the probe still answers `503`) |
| `auth_failures` | failed auth attempts cross a burst threshold (10 in-process) |
| `capacity` | the database (`db_bytes`) grows past `--alert-db-bytes` |

The payload is one JSON object:

```json
{"alert": "run_failed", "ts": 1791910696.579, "run_id": "…", "status": "failed",
 "exit_code": 1}
```

Alerts are **rate-limited per kind** (at most one per 5 minutes) so a flapping
condition cannot flood the webhook; suppressed and delivered alerts are counted in
`/metrics` (`benchd_alerts_fired_total`, `benchd_alerts_suppressed_total`). Every
POST is handed to a **daemon thread** with a short timeout, so a slow, hung or
dead webhook never blocks, slows or alters the request that triggered it — the
request returns exactly as it would with alerting off. A webhook POST that raises
is logged (structured JSON) and counted in `benchd_alerts_failed_total`.

## Per-run telemetry cache

The per-run detail, `/stats`, `/telemetry` and `/telemetry.csv` endpoints all
derive their answers from the run's telemetry rows. Re-reading and re-parsing
every row on every request is wasteful for a dashboard that polls, so the parsed
results are cached per run and keyed by `(run_id, telemetry_generation, kind)`.
Every telemetry write bumps that run's in-memory generation, so a repeat request
reuses the parsed value without a database read, and the next request after new
telemetry naturally misses the cache. The cache is bounded (64 entries, cleared
when exceeded) and in-process only — a restart simply refills it. Cached objects
are returned unchanged, so responses are **byte-identical** to the uncached path.

## Tests

`tests/test_observability.py` covers this contract end to end: the response
header and honouring a supplied id, `/health` staying cheap with a dead store,
`/ready` reporting the DB and failing closed, the `/metrics` counters, and an
injected handler error being logged (with a traceback) while still returning its
normal status. `tests/test_alerts.py` covers the alert webhook (fires on a failed
finish and a readiness failure, rate-limited, dead webhook never slows the
request, counters in `/metrics`) and the per-run telemetry cache (identical
responses, refreshed after new rows). Run them with the rest of the suite:

```console
python tests/run_all.py
```
