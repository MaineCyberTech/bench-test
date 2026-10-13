#!/usr/bin/env python3
"""benchd - bench-test control plane (single pane of glass).

A dependency-free HTTP + SSE server that:

  * lets runners (the headless agent, and the GUI/TUI dashboards) register and
    stream live phase + telemetry data,
  * queues operator commands (start/stop a run, update the toolkit, set the fan
    curve / power limit) for runners to pick up,
  * schedules runs (every N seconds, or daily at a wall-clock time),
  * serves a self-contained web dashboard with operator login over history, live
    runs, trends and side-by-side comparisons.

Auth model
  operators   the shared operator token (BENCHD_TOKEN), as a bearer (API/CLI) or
              ?token= (web UI/SSE)
  runners     per-runner API keys, issued at registration and used as a bearer
              thereafter
  joiners     the registration-only join token (BENCHD_JOIN_TOKEN): accepted by
              POST /register alone, so a public release can ship one-click
              onboarding without shipping the operator token

Storage is SQLite (stdlib); nothing outside the Python standard library is
required, so it runs on the Proxmox lab box and on the live USB alike.

    python3 server/benchd.py --host 0.0.0.0 --port 8722

Config (first wins): --token/--join-token flags, $BENCHD_TOKEN/$BENCHD_JOIN_TOKEN,
server/benchd.conf, ~/.config/benchd.conf, /etc/benchd.conf   (key=value).
  BENCHD_TOKEN=...              operator API bearer (never bake into a release)
  BENCHD_JOIN_TOKEN=...         registration-only token (safe to bake)
  BENCHD_CORS_ORIGIN=...        one exact cross-origin dashboard (optional;
                                same-origin is always allowed, never a wildcard)
"""
import argparse
import base64
import csv
import io
import json
import math
import os
import queue
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
# Wire format this control plane speaks. Runners advertise theirs; a mismatch is
# flagged in the fleet view instead of silently producing wrong data.
PROTOCOL = 1
WEB_DIR = os.path.join(HERE, "web")
DEFAULT_DB = os.path.join(HERE, "data", "benchd.sqlite")

# A request body larger than this is refused before it is read, so a declared-but-
# never-sent Content-Length cannot pin a handler thread and its memory. Artifacts
# (telemetry CSV, sensors dump, a screenshot) post separately and fit comfortably.
MAX_BODY = 24 * 1024 * 1024

# Per-request array caps: the byte cap alone still let one POST carry an unbounded
# number of samples or log lines (SQLite rows and SSE payloads grow with it).
MAX_SAMPLES = 5000
MAX_LOG_LINES = 2000

# SEC-P2-003: in-process, per-client-IP abuse limits. Failed auth (401) attempts
# and /api/v1/register calls each get a small token bucket (default 10/minute);
# the operator token is never throttled. The counters live in memory only, so a
# restart clears them, and they are attached to the HTTP server instance so a
# test run that spins up several servers does not carry state between them.
AUTH_RATE_LIMIT = 10
AUTH_RATE_WINDOW = 60.0
REGISTER_RATE_LIMIT = 10
REGISTER_RATE_WINDOW = 60.0
RATE_LIMITS = {"auth": (AUTH_RATE_LIMIT, AUTH_RATE_WINDOW),
               "register": (REGISTER_RATE_LIMIT, REGISTER_RATE_WINDOW)}

# OBS-P2-002: one JSON object per line on stderr, so an operator (or a log
# shipper) can filter by level or join every line of a request by its
# request_id. Kept tiny and dependency-free; `extras` is a free-form object
# (route, error, traceback, run_id, ...).
def log_json(level, msg, request_id=None, **extras):
    rec = {"ts": round(time.time(), 3), "level": level, "msg": str(msg),
           "request_id": request_id}
    if extras:
        rec["extras"] = extras
    try:
        sys.stderr.write(json.dumps(rec, default=str) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


# OBS-P2-005: in-process counters for a stdlib Prometheus text endpoint. They are
# attached to the HTTP server (like the rate limiter), so a test run that spins up
# several servers does not carry state between them.
class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.started = time.time()
        self.requests = {"1xx": 0, "2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0}
        self.auth_failures = 0
        self.rate_limited = 0

    def status(self, code):
        cls = "%dxx" % (int(code) // 100)
        with self.lock:
            if cls in self.requests:
                self.requests[cls] += 1

    def auth_failure(self):
        with self.lock:
            self.auth_failures += 1

    def rate_limit(self):
        with self.lock:
            self.rate_limited += 1

    def uptime(self):
        return time.time() - self.started


# OBS-P2-004: /health is a cheap liveness probe. Its expensive fields (per-table
# row counts, db size and the cached "latest main" commit) are computed off the
# request path and refreshed by a daemon thread, so a liveness probe can never
# block on a SQLite lock or on the GitHub compare's 8s timeout.
_HEALTH_LOCK = threading.Lock()
_HEALTH = {"capacity": None, "runners": 0, "latest": "", "git": "", "version": ""}
_ACTIVE_STORE = {"s": None}
_HEALTH_THREAD = {"t": None}
HEALTH_REFRESH_S = 5.0


def _health_capacity(store):
    rows = {}
    for t in ("runs", "telemetry", "logs", "events", "artifacts"):
        rows[t] = store.one("SELECT COUNT(*) c FROM %s" % t)["c"]
    db_bytes = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            db_bytes += os.path.getsize((store.path or "") + suffix)
        except OSError:
            pass
    return {"rows": rows, "db_bytes": db_bytes, "retention_days": RETENTION_DAYS}


def refresh_health(store=None, with_network=True):
    """Recompute the cached /health fields. Best-effort: a store closed by test
    teardown (or a transient DB error) must not spam the log."""
    store = store or _ACTIVE_STORE["s"]
    if store is None:
        return
    try:
        cap = _health_capacity(store)
        runners = store.one("SELECT COUNT(*) c FROM runners")["c"]
        git = server_git()
        version = server_version()
        latest = latest_git() if with_network else _HEALTH.get("latest", "")
    except Exception:
        return
    with _HEALTH_LOCK:
        _HEALTH.update({"capacity": cap, "runners": runners, "git": git,
                        "version": version, "latest": latest})


def health_snapshot():
    with _HEALTH_LOCK:
        snap = dict(_HEALTH)
    if snap.get("capacity") is None:
        snap["capacity"] = {"rows": {}, "db_bytes": 0, "retention_days": RETENTION_DAYS}
    return snap


def _health_loop():
    while True:
        time.sleep(HEALTH_REFRESH_S)
        refresh_health()


def start_health_refresher(store):
    """Prime the /health cache (no network) and keep it fresh in the background."""
    _ACTIVE_STORE["s"] = store
    refresh_health(store, with_network=False)
    t = _HEALTH_THREAD["t"]
    if t is None or not t.is_alive():
        t = threading.Thread(target=_health_loop, name="benchd-health", daemon=True)
        _HEALTH_THREAD["t"] = t
        t.start()


class BadJSON(Exception):
    """A write request body was present but not a JSON object."""


class TokenBucket:
    """Tiny token bucket: `capacity` tokens refilled evenly over `window` seconds."""

    def __init__(self, capacity, window):
        self.capacity = float(capacity)
        self.rate = float(capacity) / float(window)
        self.tokens = float(capacity)
        self.ts = time.time()

    def take(self):
        now = time.time()
        self.tokens = min(self.capacity, self.tokens + (now - self.ts) * self.rate)
        self.ts = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False

    def retry_after(self):
        if self.tokens >= 1.0 or self.rate <= 0:
            return 0
        return int((1.0 - self.tokens) / self.rate) + 1


class RateLimiter:
    """Per-IP token buckets, keyed by kind ('auth' / 'register')."""

    def __init__(self, limits=None):
        self.limits = dict(limits or RATE_LIMITS)
        self.lock = threading.Lock()
        self.buckets = {}

    def check(self, ip, kind):
        """Return (allowed, retry_after_seconds) for this ip + bucket kind."""
        with self.lock:
            if len(self.buckets) > 10000:
                self.buckets.clear()
            b = self.buckets.get((ip, kind))
            if b is None:
                limit, window = self.limits.get(kind, (10, 60.0))
                b = TokenBucket(limit, window)
                self.buckets[(ip, kind)] = b
            return b.take(), b.retry_after()


SAMPLE_FIELDS = ["elapsed", "temp_c", "fan_pct", "power_w", "gpu_util",
                 "gpu_fan_rpm", "cpu_util", "ram_used", "sm_mhz"]

# Fields whose domain really is 0..100. nvidia-smi intermittently reports a
# nonsense fan speed (a live stick run recorded fan_pct 413 and 137 in one window),
# and because a chart scales each series to its own max, a single spike flattens
# the whole fan trace. Out-of-range values are DROPPED rather than clamped:
# clamping to 100 would invent a plausible-looking reading. mem_util and the
# encoder percentages are deliberately not bounded - some drivers legitimately
# report them above 100.
_PCT_0_100 = ("fan_pct", "gpu_util", "cpu_util")


def sanitise_sample(s):
    """Null out physically impossible percentage readings, in place.

    Non-finite values (NaN/Infinity from a divide or a bad sensor) are nulled too:
    json.dumps would otherwise emit the bare literals NaN/Infinity, which are not
    valid JSON and make the dashboard's JSON.parse throw.
    """
    for k, v in list(s.items()):
        if isinstance(v, float) and not math.isfinite(v):
            s[k] = None
    for k in _PCT_0_100:
        v = s.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and not (0 <= v <= 100):
            s[k] = None
    return s


def _json_safe(obj):
    """Recursively replace non-finite floats so a payload can be serialised.

    Python's json.dumps would happily emit NaN/Infinity (invalid JSON that a
    browser's JSON.parse rejects); this is the fallback for objects that did not
    come through sanitise_sample (legacy rows, derived stats).
    """
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _json_dumps(obj):
    """json.dumps that can never emit NaN/Infinity/other invalid JSON."""
    try:
        return json.dumps(obj, allow_nan=False)
    except (ValueError, TypeError):
        return json.dumps(_json_safe(obj), allow_nan=False, default=str)

METRICS = [
    ("gpu_matmul_tflops.fp16", "GPU matmul fp16 (TFLOPS)"),
    ("gpu_matmul_tflops.bf16", "GPU matmul bf16 (TFLOPS)"),
    ("gpu_soak_tflops", "GPU soak (TFLOPS)"),
    ("cpu_matmul_gflops.fp32", "CPU matmul fp32 (GFLOPS)"),
    ("cpu_soak_gflops", "CPU soak (GFLOPS)"),
    ("ram_soak_gbps", "RAM soak (GB/s)"),
    ("disk_seqwrite_mbps", "Disk seq write (MB/s)"),
    ("net.download_mbps", "Network download (Mbps)"),
    ("net.upload_mbps", "Network upload (Mbps)"),
    ("gpu_soak_telemetry.temp_c.2", "GPU soak peak temp (C)"),
    ("fullsoak.result.verdict.clean", "Full-system soak clean (0/1)"),
]

ONLINE_WINDOW = 15.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS runners (
  id TEXT PRIMARY KEY, name TEXT, os TEXT, kind TEXT, version TEXT,
  capabilities TEXT, first_seen REAL, last_seen REAL, status TEXT,
  current_run TEXT, addr TEXT, git TEXT, api_key TEXT,
  machine_id TEXT, ephemeral INTEGER DEFAULT 0, instance TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, runner_id TEXT, host TEXT, label TEXT, profile TEXT,
  started REAL, ended REAL, status TEXT, exit_code INTEGER,
  platform TEXT, gpu TEXT, driver TEXT, torch TEXT, summary TEXT, system TEXT,
  preset TEXT, stats TEXT
);
CREATE TABLE IF NOT EXISTS phases (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, seq INTEGER, grp TEXT,
  label TEXT, status TEXT, t0 REAL, t1 REAL, detail TEXT
);
CREATE TABLE IF NOT EXISTS telemetry (
  run_id TEXT, ts REAL, elapsed REAL, temp_c REAL, fan_pct REAL, power_w REAL,
  gpu_util REAL, gpu_fan_rpm REAL, cpu_util REAL, ram_used REAL,
  sm_mhz REAL, extra TEXT
);
CREATE TABLE IF NOT EXISTS logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts REAL, stream TEXT, line TEXT
);
CREATE TABLE IF NOT EXISTS commands (
  id TEXT PRIMARY KEY, runner_id TEXT, run_id TEXT, kind TEXT, payload TEXT,
  status TEXT, created REAL, sent REAL, acked REAL, result TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts REAL, level TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, name TEXT, kind TEXT,
  size INTEGER, sha256 TEXT, content TEXT
);
CREATE TABLE IF NOT EXISTS schedules (
  id TEXT PRIMARY KEY, runner_id TEXT, label TEXT, profile TEXT, kind TEXT,
  every_s INTEGER, at_time TEXT, next_run REAL, enabled INTEGER,
  last_run REAL, created REAL, last_status TEXT
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, actor TEXT, action TEXT,
  target TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started);
CREATE INDEX IF NOT EXISTS idx_runs_runner ON runs(runner_id);
CREATE INDEX IF NOT EXISTS idx_phases_run ON phases(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_tele_run ON telemetry(run_id, ts);
CREATE INDEX IF NOT EXISTS idx_cmd_runner ON commands(runner_id, status);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, ts);
CREATE INDEX IF NOT EXISTS idx_logs_run ON logs(run_id, id);
CREATE INDEX IF NOT EXISTS idx_sched_next ON schedules(enabled, next_run);
-- the host filter / trend list scan this
CREATE INDEX IF NOT EXISTS idx_runs_host ON runs(host);
"""

# Every table that references a run by run_id. Deleting a run clears all of them
# in one transaction, so nothing is left orphaned and readable by run id.
RUN_DEPENDENT_TABLES = ("phases", "telemetry", "logs", "events", "artifacts", "commands")


# --------------------------------------------------------------------- store --
class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.lock = threading.RLock()
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.executescript("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")
            self.conn.executescript(SCHEMA)
            self._migrate()
            self.conn.commit()
        # OBS-P2-004: prime the cheap /health cache and start its refresher.
        start_health_refresher(self)

    def _migrate(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(runners)")}
        if "api_key" not in cols:
            self.conn.execute("ALTER TABLE runners ADD COLUMN api_key TEXT")
        if "machine_id" not in cols:
            self.conn.execute("ALTER TABLE runners ADD COLUMN machine_id TEXT")
        if "ephemeral" not in cols:
            self.conn.execute("ALTER TABLE runners ADD COLUMN ephemeral INTEGER DEFAULT 0")
        if "instance" not in cols:
            self.conn.execute("ALTER TABLE runners ADD COLUMN instance TEXT")
        tcols = {r[1] for r in self.conn.execute("PRAGMA table_info(telemetry)")}
        if "extra" not in tcols:
            self.conn.execute("ALTER TABLE telemetry ADD COLUMN extra TEXT")
        rcols = {r[1] for r in self.conn.execute("PRAGMA table_info(runs)")}
        if "system" not in rcols:
            self.conn.execute("ALTER TABLE runs ADD COLUMN system TEXT")
        if "preset" not in rcols:
            self.conn.execute("ALTER TABLE runs ADD COLUMN preset TEXT")
        if "stats" not in rcols:
            self.conn.execute("ALTER TABLE runs ADD COLUMN stats TEXT")
        # A retried artifact upload used to leave the old copy in place (the
        # download then served stale content) and phases were SELECT-then-INSERT
        # with no uniqueness, so a race could double them. Collapse any existing
        # duplicates to the newest row, then enforce the invariant with unique
        # indexes the upload paths can use as ON CONFLICT targets.
        self.conn.execute("DELETE FROM artifacts WHERE id NOT IN "
                          "(SELECT MAX(id) FROM artifacts GROUP BY run_id, name)")
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_run_name "
                          "ON artifacts(run_id, name)")
        self.conn.execute("DELETE FROM phases WHERE id NOT IN "
                          "(SELECT MAX(id) FROM phases GROUP BY run_id, seq)")
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_phases_run_seq "
                          "ON phases(run_id, seq)")
        # runners.api_key is looked up on every authenticated request (and the
        # machine_id+kind+instance adoption on every register); both were full
        # table scans. The unique index also enforces one key per runner. A
        # legacy database can carry blank/duplicate keys (which never
        # authenticated unambiguously): clear the extras first so the index can
        # be created - the runner re-registers and gets a fresh key.
        self.conn.execute("UPDATE runners SET api_key=NULL WHERE api_key=''")
        dups = [r[0] for r in self.conn.execute(
            "SELECT id FROM runners WHERE api_key IS NOT NULL AND id NOT IN "
            "(SELECT MIN(id) FROM runners WHERE api_key IS NOT NULL GROUP BY api_key)")]
        for rid in dups:
            self.conn.execute("UPDATE runners SET api_key=NULL WHERE id=?", (rid,))
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_runners_api_key "
                          "ON runners(api_key)")
        # after the ALTERs above: machine_id is a migrated column, so the index
        # cannot live in SCHEMA (a legacy table would not have it yet)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_runners_machine "
                          "ON runners(machine_id)")

    def ex(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def delete_run(self, run_id):
        """Delete a run and every row that belongs to it, in one transaction.

        Previously each table was cleared with its own committed statement; the
        set is now atomic, so a deleted run can never leave telemetry/logs/events/
        artifacts/phases/commands behind to be read back by run id.
        """
        with self.lock:
            try:
                for t in RUN_DEPENDENT_TABLES:
                    self.conn.execute("DELETE FROM %s WHERE run_id=?" % t, (run_id,))
                cur = self.conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
                self.conn.commit()
                return cur.rowcount
            except Exception:
                self.conn.rollback()
                raise

    def delete_runner(self, runner_id):
        """Delete a runner and everything it owns.

        SEARCH-P2-002 / PRIV-P2-003: dropping only the runner row left its runs
        (and their telemetry, logs, artifacts, phases, events and commands)
        orphaned but still readable by run id. Each run goes through delete_run so
        the same dependent-row set is cleared, then the runner's own commands and
        schedules, then the runner row.
        """
        for r in self.q("SELECT id FROM runs WHERE runner_id=?", (runner_id,)):
            self.delete_run(r["id"])
        self.ex("DELETE FROM commands WHERE runner_id=?", (runner_id,))
        self.ex("DELETE FROM schedules WHERE runner_id=?", (runner_id,))
        return self.ex("DELETE FROM runners WHERE id=?", (runner_id,)).rowcount

    def audit(self, actor, action, target=None, detail=None):
        """Append one row to the audit trail (ACM-P2-003/005, ADMIN-P2-001, ...)."""
        self.ex("INSERT INTO audit(ts,actor,action,target,detail) VALUES(?,?,?,?,?)",
                (now(), actor, action, target, detail))


# ----------------------------------------------------------------------- bus --
# Wakes a stream whose subscriber queue overflowed; the handler then closes the
# connection so the browser's EventSource reconnects and refetches.
_SSE_OVERFLOW = object()


class Bus:
    """Fan-out of live events to SSE subscribers.

    Bounded on every axis: concurrent streams are capped, each subscriber queue
    is capped, telemetry is coalesced to one event per run per tick, and an
    event is serialised once rather than once per subscriber. A subscriber that
    still falls behind is closed (not silently starved): its queue is cleared
    and it is woken with a sentinel, so the client reconnects and refreshes.
    """

    MAX_SUBS = 64               # concurrent SSE streams
    QUEUE_MAX = 1000            # events a single subscriber may fall behind by
    TELEMETRY_FLUSH_S = 0.5     # telemetry cadence: at most 2 events/s per run

    def __init__(self, flush_s=None):
        self.lock = threading.Lock()
        self.subs = set()
        self._pending = {}          # run_id -> {"samples": [...], "n": int}
        self._runner_sigs = {}      # runner id -> fields that last went out
        self._stop = threading.Event()
        if flush_s is not None:
            self.TELEMETRY_FLUSH_S = flush_s
        threading.Thread(target=self._telemetry_loop, daemon=True,
                         name="benchd-sse-flush").start()

    def subscribe(self):
        """A new subscriber queue, or None when the stream cap is reached."""
        q = queue.Queue(maxsize=self.QUEUE_MAX)
        with self.lock:
            if len(self.subs) >= self.MAX_SUBS:
                return None
            self.subs.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subs.discard(q)

    @staticmethod
    def _frame(ev):
        """One SSE frame, serialised once and shared by every subscriber."""
        return ("event: %s\ndata: %s\n\n"
                % (ev.get("event", "message"), _json_dumps(ev.get("data", {})))).encode()

    def publish(self, ev):
        frame = self._frame(ev)
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(frame)
            except queue.Full:
                self._overflow(q)

    def _overflow(self, q):
        """Detach a subscriber that cannot keep up and wake it to close.

        Dropping events silently left a stalled dashboard showing stale data
        with no way to recover; closing the stream makes EventSource reconnect,
        and the reconnect refetches the run's buffer.
        """
        with self.lock:
            if q not in self.subs:
                return
            self.subs.discard(q)
        try:
            while True:
                q.get_nowait()
        except queue.Empty:
            pass
        try:
            q.put_nowait(_SSE_OVERFLOW)
        except Exception:
            pass

    def publish_telemetry(self, run_id, samples, n=None):
        """Coalesce a run's live samples into one event per tick.

        A runner posts telemetry in batches (the importer in 200-sample
        chunks); emitting one event per batch serialised and fanned out every
        one. Samples accumulate and the flusher emits a single event per run
        per tick carrying every sample since the last one.
        """
        if not samples:
            return
        with self.lock:
            p = self._pending.setdefault(run_id, {"samples": [], "n": 0})
            p["samples"].extend(samples)
            if len(p["samples"]) > self.QUEUE_MAX:
                p["samples"] = p["samples"][-self.QUEUE_MAX:]
            p["n"] += n if n is not None else len(samples)

    def flush_telemetry(self):
        with self.lock:
            pending, self._pending = self._pending, {}
        for run_id, p in pending.items():
            if not p["samples"]:
                continue
            self.publish({"event": "telemetry",
                          "data": {"run_id": run_id, "sample": p["samples"][-1],
                                   "samples": p["samples"], "n": p["n"]}})

    def _telemetry_loop(self):
        while not self._stop.wait(self.TELEMETRY_FLUSH_S):
            try:
                self.flush_telemetry()
            except Exception:
                pass

    @staticmethod
    def _runner_sig(r):
        # last_seen/online move on every heartbeat by design; everything else is
        # what a dashboard actually renders
        return tuple(sorted((k, _json_dumps(v)) for k, v in r.items()
                            if k not in ("last_seen", "online")))

    def publish_runner(self, r):
        """Publish a runner record, but only the fields that changed.

        The heartbeat runs every 2 s and used to re-send the whole row
        (capabilities, git, inventory) to every subscriber. The full record goes
        out when anything other than liveness changes; otherwise a slim update
        carries last_seen so online/offline still moves.
        """
        if not r:
            return
        sig = self._runner_sig(r)
        with self.lock:
            changed = self._runner_sigs.get(r.get("id")) != sig
            if changed:
                self._runner_sigs[r.get("id")] = sig
        if changed:
            self.publish({"event": "runner", "data": r})
        else:
            self.publish({"event": "runner",
                          "data": {"id": r.get("id"), "last_seen": r.get("last_seen"),
                                   "online": r.get("online"), "delta": True}})


# ------------------------------------------------------------------ helpers ---
def now():
    return time.time()


def online(last_seen):
    return (now() - (last_seen or 0)) < ONLINE_WINDOW


_SERVER_GIT = {"v": None}
_SERVER_VER = {"v": None}
# serialises POST /api/v1/server/update so two pulls cannot race the checkout
_SERVER_UPDATE_LOCK = threading.Lock()


def server_git():
    """Short HEAD of the control plane's own code ('' when unknown).

    Runners report their toolkit commit; comparing the two is what tells the
    fleet view which sticks are behind the deployed code. A copy-deployed server
    has no .git, so a stamp file written by the deploy is accepted too.
    """
    if _SERVER_GIT["v"] is None:
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        val = ""
        try:
            r = subprocess.run(["git", "-C", repo, "rev-parse", "--short", "HEAD"],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                val = r.stdout.strip()
        except Exception:
            val = ""
        if not val:
            for rel in (".deployed-commit", ".bench-version"):
                try:
                    with open(os.path.join(repo, rel), encoding="utf-8") as fh:
                        txt = fh.read().strip()
                    if rel.endswith(".json") or txt.startswith("{"):
                        val = str(json.loads(txt).get("commit") or "")
                    else:
                        val = txt.split()[0] if txt else ""
                except Exception:
                    val = ""
                if val:
                    break
        _SERVER_GIT["v"] = val
    return _SERVER_GIT["v"]


def server_version():
    """The control plane's release version, from the VERSION file next to it.

    The server is deployed as a copy (no .git), so the version is what identifies
    a deployment; ``0.0.0+unknown`` when the file was not shipped.
    """
    if _SERVER_VER["v"] is None:
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        val = ""
        try:
            with open(os.path.join(repo, "VERSION"), encoding="utf-8") as fh:
                val = fh.read().strip().splitlines()[0].strip()
        except (OSError, IndexError):
            val = ""
        _SERVER_VER["v"] = val or "0.0.0+unknown"
    return _SERVER_VER["v"]


REPO_SLUG = os.environ.get("BENCH_REPO_SLUG") or "MaineCyberTech/bench-test"
GH_API = os.environ.get("BENCH_GH_API") or "https://api.github.com"
_REF = {"v": None, "t": 0.0}
REF_TTL = 120.0
_CMP = {}
_CMP_PENDING = set()
_CMP_LOCK = threading.Lock()
_CMP_TTL = 600.0          # cached compare results are refreshed after this
COMPARE_TIMEOUT_S = 4.0   # hard cap on a background GitHub compare


def compare_detail(base, head):
    """(status, files) for GitHub compare `base...head`, from cache.

    status is ahead|behind|diverged|identical|'' (unknown); `files` are the changed
    paths GitHub reports (possibly truncated, [] when unknown) - enough to tell a
    docs-only difference from a real one.

    A cache miss returns ("", []) immediately and refreshes in the background.
    This used to block the request for up to 8 s per unknown commit pair inside
    /state, /runners and every heartbeat broadcast, so a slow GitHub stalled
    every dashboard. Callers see the refreshed value on a later request.
    """
    if not base or not head:
        return "", []
    if base == head:
        return "identical", []
    key = (base, head)
    t = time.time()
    with _CMP_LOCK:
        hit = _CMP.get(key)
        if hit and (t - hit[2]) < _CMP_TTL:
            return hit[0], hit[1]
        if key not in _CMP_PENDING:
            _CMP_PENDING.add(key)
            threading.Thread(target=_compare_fetch, args=(key,), daemon=True).start()
    return (hit[0], hit[1]) if hit else ("", [])


def _compare_fetch(key):
    base, head = key
    status, files = "", []
    if os.environ.get("BENCH_OFFLINE") != "1":
        try:
            url = "%s/repos/%s/compare/%s...%s" % (GH_API, REPO_SLUG, base, head)
            req = urllib.request.Request(url, headers={"User-Agent": "benchd",
                                                       "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=COMPARE_TIMEOUT_S) as r:
                j = json.loads(r.read().decode())
            status = str(j.get("status") or "")
            files = [str(f.get("filename") or "") for f in (j.get("files") or [])]
        except Exception:
            status, files = "", []
    with _CMP_LOCK:
        if len(_CMP) > 500:
            _CMP.clear()
        _CMP[key] = (status, files, time.time())
        _CMP_PENDING.discard(key)


def compare_commits(base, head):
    """GitHub compare `base...head` -> ahead|behind|diverged|identical|'' (unknown).

    `ahead` means head is newer than base, i.e. the runner (base) is behind.
    """
    return compare_detail(base, head)[0]


# a runner only needs an update when the difference includes something it runs.
# The control plane is deployed separately and the test suite is not run by
# runners, so those do not count; bench/, live/, packaging/ and tools/ do.
IGNORED_UPDATE_PATHS = ("docs/", ".github/", "examples/", "results/", "server/", "tests/")
IGNORED_UPDATE_SUFFIXES = (".md",)


def update_matters(files):
    """True when a commit range touches something a runner actually runs.

    Docs-only (or CI-only, or example/result data) changes should not light up
    every runner's "update available" badge. An empty/unknown file list counts as
    significant, so a real update is never hidden.
    """
    if not files:
        return True
    for f in files:
        if not f:
            continue
        if f.endswith(IGNORED_UPDATE_SUFFIXES):
            continue
        if f.startswith(IGNORED_UPDATE_PATHS):
            continue
        return True
    return False


def latest_git(force=False):
    """Newest commit on main (GitHub, cached) — what a runner should be running.

    Falls back to this server's own commit when GitHub is unreachable. Set
    BENCH_OFFLINE=1 to skip the lookup entirely (tests).
    """
    t = time.time()
    if not force and _REF["v"] and (t - _REF["t"]) < REF_TTL:
        return _REF["v"]
    val = ""
    if os.environ.get("BENCH_OFFLINE") != "1":
        try:
            url = "%s/repos/%s/commits/main" % (GH_API, REPO_SLUG)
            req = urllib.request.Request(url, headers={"User-Agent": "benchd",
                                                       "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=8) as r:
                val = str(json.loads(r.read().decode()).get("sha") or "")[:7]
        except Exception:
            val = ""
    _REF["v"], _REF["t"] = (val or server_git()), t
    return _REF["v"]


ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def safe_id(v):
    """Ids supplied by clients must be opaque tokens, never HTML-bearing text."""
    return bool(v) and bool(ID_RE.match(str(v)))


def safe_filename(n):
    """Sanitise a name used in a Content-Disposition header."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(n or "artifact"))[:120]


def int_arg(q, key, default, lo=0, hi=100000):
    """Query-string int with a sane fallback (never raises on garbage)."""
    try:
        v = int(str(q.get(key, [default])[0]))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(v, hi))


def dotted(obj, path):
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except Exception:
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def runner_public(r):
    if r is None:
        return None
    r = dict(r)
    r.pop("api_key", None)
    r["online"] = online(r.get("last_seen"))
    r["ephemeral"] = bool(r.get("ephemeral"))
    mid = r.get("machine_id") or ""
    r["uid"] = mid.split("-", 1)[-1][:8] if mid else ""
    # A display label that tells instances of the same host apart
    # (e.g. the live USB's tty1 / ttyS0 dashboards).
    label = r.get("name") or "?"
    if (r.get("instance") or "").strip():
        label += " (" + r["instance"].strip() + ")"
    r["label"] = label
    try:
        r["capabilities"] = json.loads(r.get("capabilities") or "{}")
    except Exception:
        r["capabilities"] = {}
    # is this runner's toolkit behind the deployed code?
    tk = r["capabilities"].get("toolkit") or {}
    rc = str(tk.get("commit") or r.get("git") or "").strip()
    ref = latest_git()
    r["toolkit_commit"] = rc
    r["toolkit_mode"] = tk.get("mode") or ("git" if r.get("git") else "")
    r["latest_commit"] = ref
    # ordering matters: a runner ahead of the cached tip, or one that *is* the
    # tip, must not be flagged (a plain != wrongly flagged both)
    st, files = compare_detail(rc, ref)
    r["toolkit_status"] = st or ("differs" if (rc and ref and rc != ref) else "")
    behind = st == "ahead" or st == "diverged" or (
        not st and bool(rc and ref and rc != ref))
    # a difference that only touches docs/CI/data is not an update
    r["update_available"] = behind and (st == "diverged" or update_matters(files))
    return r


def run_public(r):
    if r is None:
        return None
    r = dict(r)
    for k in ("summary", "profile", "system"):
        if r.get(k):
            try:
                r[k] = json.loads(r[k])
            except Exception:
                pass
    return r


def tele_public(r):
    """A telemetry row as a named dict, with any extra metrics merged in."""
    if not r:
        return r
    d = {k: r.get(k) for k in SAMPLE_FIELDS}
    d["ts"] = r.get("ts")
    if r.get("extra"):
        try:
            d.update(json.loads(r["extra"]))
        except Exception:
            pass
    return d


def pct(sorted_vals, p):
    """Nearest-rank percentile of an ascending list."""
    if not sorted_vals:
        return None
    k = max(0, min(len(sorted_vals) - 1, int(round((p / 100.0) * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


def derive_stats(rows, series, stats):
    """Metrics computed from the series: energy, clock residency, throttling."""
    d = {}
    # energy (J) via trapezoid over power
    pts = []
    for r in rows:
        t = r.get("elapsed")
        w = None
        try:
            w = tele_public(r).get("power_w")
        except Exception:
            w = None
        if t is not None and isinstance(w, (int, float)) and math.isfinite(float(w)):
            try:
                t = float(t)
            except (TypeError, ValueError):
                continue
            if math.isfinite(t):
                pts.append((t, float(w)))
    if len(pts) >= 2:
        j = 0.0
        for (t0, w0), (t1, w1) in zip(pts, pts[1:]):
            dt = t1 - t0
            if math.isfinite(dt) and dt > 0:
                j += (w0 + w1) / 2.0 * dt
        d["energy_j"] = round(j, 1)
        if pts[-1][0] > 0:
            d["energy_kj_per_hour"] = round(j / 1000.0 / (pts[-1][0] / 3600.0), 2)
    # clock residency: share of samples at >=95% of the peak SM clock
    sm = series.get("sm_mhz")
    if sm:
        mx = max(sm)
        if mx > 0:
            d["clock_residency_pct"] = round(100.0 * sum(1 for x in sm if x >= 0.95 * mx) / len(sm), 1)
    # time spent throttled (non-idle slowdown reason observed)
    th = series.get("throttle")
    if th:
        d["throttled_pct"] = round(100.0 * sum(1 for x in th if x >= 1) / len(th), 1)
    # thermal headroom
    peak = (stats.get("temp_c") or {}).get("max")
    lim = (stats.get("temp_limit_c") or {}).get("max")
    if peak is not None and lim:
        d["thermal_headroom_c"] = round(lim - peak, 1)
    # power headroom vs the card's max
    pmax = (stats.get("power_max_w") or {}).get("max")
    pgpu = (stats.get("power_w") or {}).get("max")
    if pmax and pgpu:
        d["power_headroom_w"] = round(pmax - pgpu, 1)
    return d


def compute_stats(store, run_id):
    """Per-field min/avg/max/p95/p99 plus derived metrics for a run's telemetry.

    Module level, not a handler method, so the retention sweep can materialise a
    run's stats before its samples are deleted.
    """
    rows = store.q("SELECT * FROM telemetry WHERE run_id=? ORDER BY ts", (run_id,))
    series = {}
    for r in rows:
        for k, v in tele_public(r).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)):
                series.setdefault(k, []).append(float(v))
    stats = {}
    for k, vals in series.items():
        vals = sorted(vals)
        n = len(vals)
        if not n:
            continue
        mean = sum(vals) / n
        var = sum((x - mean) ** 2 for x in vals) / n
        stats[k] = {"n": n, "min": vals[0], "max": vals[-1], "avg": round(mean, 2),
                    "std": round(var ** 0.5, 3), "p50": pct(vals, 50),
                    "p95": pct(vals, 95), "p99": pct(vals, 99)}
    return {"samples": len(rows), "stats": stats, "derived": derive_stats(rows, series, stats)}



def schedule_public(s):
    s = dict(s)
    try:
        s["profile"] = json.loads(s.get("profile") or "{}")
    except Exception:
        s["profile"] = {}
    return s


def next_daily(at_time, after=None):
    after = after or time.time()
    try:
        hh, mm = [int(x) for x in at_time.split(":")]
    except Exception:
        hh, mm = 3, 0
    lt = time.localtime(after)
    cand = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
    if cand <= after:
        cand += 86400
    return cand


# ------------------------------------------------------------------ server ----
class BodyError(Exception):
    """A request body that must be refused with a specific status code."""

    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


class Handler(BaseHTTPRequestHandler):
    server_version = "benchd/2.0"
    protocol_version = "HTTP/1.0"
    # A declared-but-never-sent body would otherwise pin a handler thread (and its
    # buffer) forever: without a socket timeout rfile.read() blocks indefinitely.
    # Comfortably above the SSE keepalive interval so idle streams stay open.
    timeout = 60.0
    store = None
    bus = None
    token = None
    join_token = None
    cors_origin = None
    web_dir = WEB_DIR
    # resolved once per request (see _caller); a keep-alive connection reuses
    # the handler instance, so parse_request clears it
    _caller_cache = None

    # -- plumbing ----------------------------------------------------------
    def parse_request(self):
        self._caller_cache = None
        return super().parse_request()

    def log_message(self, fmt, *args):
        if getattr(self.server, "quiet", False):
            return
        # never log the query string: the web app passes the operator token as
        # ?token= for SSE, and that would land in journald and any proxy log
        try:
            msg = fmt % args
        except Exception:
            msg = str(fmt)
        msg = re.sub(r"(token=)[^&\s\"']+", r"\1REDACTED", msg)
        log_json("info", msg, request_id=self._request_id())

    _REQ_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")

    def _request_id(self):
        """The correlation id for this request: an inbound X-Request-ID if it is
        a sane token, else a fresh one. Stable for the life of the request and
        echoed back on every response (OBS-P2-002)."""
        rid = getattr(self, "_bench_rid", None)
        if rid:
            return rid
        headers = getattr(self, "headers", None)
        incoming = (headers.get("X-Request-ID") or "").strip() if headers else ""
        rid = incoming if (incoming and self._REQ_ID_RE.match(incoming)) else uuid.uuid4().hex
        self._bench_rid = rid
        return rid

    def _log(self, level, msg, **extras):
        """Structured log line stamped with this request's id."""
        log_json(level, msg, request_id=self._request_id(), **extras)

    def _metrics_obj(self):
        m = getattr(self.server, "_bench_metrics", None)
        if m is None:
            m = Metrics()
            self.server._bench_metrics = m
        return m

    def _send(self, code, obj=None, raw=None, ctype="application/json", headers=None):
        self._metrics_obj().status(code)
        body = raw if raw is not None else _json_dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Request-ID", self._request_id())
        # SEC-P2-002: never emit a wildcard. Only echo the request Origin when it
        # is same-origin or an explicitly configured dashboard origin.
        origin = self._origin_allowed()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            # OBS-P2-003: a client that hung up mid-response is worth a log line,
            # not a silently swallowed exception.
            self._log("error", "response write failed", traceback=traceback.format_exc())

    def _err(self, code, msg, headers=None):
        self._send(code, {"error": msg}, headers=headers)

    def _origin_allowed(self):
        """The request Origin if it may receive CORS headers, else None.

        Same-origin (the Origin host equals the request Host) is always allowed;
        a configured ``cors_origin`` matches its exact value. Anything else gets
        no Access-Control-Allow-Origin at all (not even '*'), so a foreign page
        cannot read an authenticated response.
        """
        origin = self.headers.get("Origin")
        if not origin:
            return None
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        try:
            ohost = (urlparse(origin).hostname or "").lower()
        except Exception:
            ohost = ""
        if ohost and host and ohost == host:
            return origin
        if self.cors_origin and origin == self.cors_origin:
            return origin
        return None

    # -- rate limiting (SEC-P2-003) ---------------------------------------
    def _client_ip(self):
        return self.client_address[0] if self.client_address else "?"

    def _limiter(self):
        lim = getattr(self.server, "_bench_rate", None)
        if lim is None:
            lim = RateLimiter()
            self.server._bench_rate = lim
        return lim

    def _limited(self, kind):
        """(allowed, retry_after_seconds) for this client's bucket of that kind."""
        return self._limiter().check(self._client_ip(), kind)

    def _throttled(self, kind):
        """The 429 response when a bucket is exhausted, else None."""
        allowed, retry = self._limited(kind)
        if allowed:
            return None
        self._metrics_obj().rate_limit()
        return self._err(429, "too many requests", {"Retry-After": str(retry)})

    def _deny(self, code, msg):
        """An auth denial. A 401 feeds the per-IP bucket and may become a 429."""
        if code == 401:
            self._metrics_obj().auth_failure()
            allowed, retry = self._limited("auth")
            try:
                if self.store is not None:
                    self.store.audit("anonymous", "auth_fail", target=self.path)
            except Exception:
                # OBS-P2-003: an audit write must never be silently lost.
                self._log("error", "audit write failed", traceback=traceback.format_exc())
            if not allowed:
                self._metrics_obj().rate_limit()
                return self._err(429, "too many requests", {"Retry-After": str(retry)})
        return self._err(code, msg)

    def _actor(self, q=None):
        """Who is making this request, for the audit trail."""
        if self.token is None:
            return "operator"
        if self._is_operator():
            return "operator"
        if self._is_join():
            return "join"
        r = self._runner_from_bearer()
        if r:
            return "runner:%s" % r["id"]
        return "anonymous"

    def _log_error(self, method, path, exc):
        # The caller gets a generic message; the detail stays in the server log so
        # an internal failure cannot leak types, paths or query fragments.
        print("[benchd] %s %s failed: %s: %s" % (method, path, type(exc).__name__, exc),
              flush=True)

    def _capacity(self):
        """Cached row counts + on-disk size (OBS-P2-004).

        The DB read happens off the request path (see refresh_health); this only
        serves the snapshot, so /health never blocks on SQLite.
        """
        return health_snapshot()["capacity"]

    def _body(self):
        """Parse the JSON body, raising BodyError instead of guessing.

        A malformed or truncated body used to be silently reinterpreted as {} - a
        garbage /finish then marked a run done with an empty summary.
        """
        raw_hdr = self.headers.get("Content-Length")
        if raw_hdr is None or str(raw_hdr).strip() == "":
            return {}
        try:
            n = int(raw_hdr)
        except ValueError:
            raise BodyError(400, "invalid Content-Length")
        if n < 0:
            raise BodyError(400, "invalid Content-Length")
        if n == 0:
            return {}
        if n > MAX_BODY:
            # checked before reading: a declared-but-never-sent body must not pin
            # this thread or allocate its memory
            raise BodyError(413, "body too large (%d > %d bytes)" % (n, MAX_BODY))
        try:
            raw = self.rfile.read(n)
        except Exception:
            raise BodyError(400, "body read failed")
        if len(raw) != n:
            raise BodyError(400, "truncated body (%d of %d bytes)" % (len(raw), n))
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise BodyError(400, "body is not UTF-8")
        if not text.strip():
            return {}
        try:
            obj = json.loads(text)
        except ValueError:
            raise BodyError(400, "malformed JSON body")
        if not isinstance(obj, dict):
            raise BodyError(400, "body must be a JSON object")
        return obj

    # -- auth --------------------------------------------------------------
    def _bearer(self):
        h = self.headers.get("Authorization", "")
        return h[7:] if h.startswith("Bearer ") else None

    @staticmethod
    def _token_eq(a, b):
        """Constant-time token comparison; False for anything empty/missing."""
        if not a or not b:
            return False
        try:
            return secrets.compare_digest(str(a), str(b))
        except Exception:
            return False

    def _runner_from_bearer(self):
        b = self._bearer()
        if not b:
            return None
        return self.store.one("SELECT * FROM runners WHERE api_key=?", (b,))

    def _is_operator(self):
        """The shared operator token as a bearer header only.

        ?token= is not accepted here: it is exposed in URLs, logs and proxy
        access logs, and the documented exception is only the SSE stream and
        file downloads (which cannot set headers), handled by _query_token_ok.
        """
        return self._caller()[0]

    def _query_token_ok(self, q):
        """The operator token as ?token= - SSE and downloads only."""
        if self.token is None:
            return True
        try:
            v = (q or {}).get("token", [None])[0]
        except Exception:
            v = None
        return self._token_eq(v, self.token)

    def _is_join(self):
        """The registration-only join token, accepted by POST /register alone.

        It is deliberately NOT checked by any operator gate (state, runs,
        commands, delete, schedules, exec/shell, SSE) and is not a runner
        api_key, so a public release can bake it for one-click onboarding
        without handing out fleet-wide control.
        """
        if not self.join_token:
            return False
        b = self._bearer()
        return bool(b and secrets.compare_digest(b, self.join_token))

    def _runner_ok(self, rid=None):
        op, r = self._caller()
        return bool(op or (r and (rid is None or r["id"] == rid)))

    def _caller(self):
        """(is_operator, runner_row_or_None) for the current request.

        Resolved once per request: the POST gate, _may_act_as and _may_write_run
        each used to hit the api_key lookup again (twice per write, with the
        bearer re-parsed), which is a table scan on an unindexed column.
        """
        cached = self._caller_cache
        if cached is not None:
            return cached
        if self.token is None:
            self._caller_cache = (True, None)
        elif self._token_eq(self._bearer(), self.token):
            self._caller_cache = (True, None)
        else:
            self._caller_cache = (False, self._runner_from_bearer())
        return self._caller_cache

    def _may_act_as(self, rid):
        """The operator, or the runner itself."""
        op, r = self._caller()
        return bool(op or (r and r["id"] == rid))

    def _may_write_run(self, run_id):
        """Writers may only touch their own run (operators may write any)."""
        op, r = self._caller()
        if op:
            return True
        if not r:
            return False
        row = self.store.one("SELECT runner_id FROM runs WHERE id=?", (run_id,))
        return bool(row and row["runner_id"] == r["id"])

    # -- dispatch ----------------------------------------------------------
    def do_OPTIONS(self):
        self._metrics_obj().status(204)
        self.send_response(204)
        origin = self._origin_allowed()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization,Content-Type")
        self.send_header("X-Request-ID", self._request_id())
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        if path == "/health":
            # OBS-P2-004: liveness only - every field comes from the cached
            # snapshot, so this never touches the DB or the network.
            snap = health_snapshot()
            return self._send(200, {"ok": True, "time": now(), "git": snap["git"],
                                    "version": snap["version"],
                                    "protocol": PROTOCOL,
                                    "latest": snap["latest"],
                                    "capacity": snap["capacity"],
                                    "runners": snap["runners"]})
        if path == "/ready":
            return self._ready()
        if path == "/metrics":
            return self._send(200, raw=self._metrics_text().encode(),
                              ctype="text/plain; version=0.0.4; charset=utf-8")
        if path.startswith("/api/"):
            # ?token= is allowed only where a browser cannot send a header:
            # the SSE stream (EventSource) and file downloads (link navigation).
            if path == "/api/v1/stream":
                if not (self._is_operator() or self._query_token_ok(q)):
                    return self._deny(401, "unauthorized")
                return self._stream()
            if re.match(r"^/api/v1/runs/[^/]+/(?:artifact/[^/]+|telemetry\.csv)$", path):
                if not (self._is_operator() or self._query_token_ok(q)):
                    return self._err(401, "unauthorized")
                return self._api_get(path, q)
            m = re.match(r"^/api/v1/runners/([^/]+)/commands$", path)
            if m:
                if not self._may_act_as(m.group(1)):
                    return self._err(403, "not your runner")
                return self._runner_commands(m.group(1))
            if not self._is_operator():
                return self._deny(401, "unauthorized")
            try:
                return self._api_get(path, q)
            except Exception as exc:
                self._log_error("GET", path, exc)
                return self._err(500, "internal server error")
        return self._static(path)

    def do_DELETE(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        if not path.startswith("/api/"):
            return self._err(404, "not found")
        if not self._is_operator():
            return self._deny(401, "unauthorized")
        actor = self._actor(q)
        m = re.match(r"^/api/v1/schedules/([^/]+)$", path)
        if m:
            self.store.ex("DELETE FROM schedules WHERE id=?", (m.group(1),))
            self.store.audit(actor, "schedule_delete", target=m.group(1))
            self.bus.publish({"event": "schedule", "data": {"id": m.group(1), "deleted": True}})
            return self._send(200, {"ok": True})
        m = re.match(r"^/api/v1/runs/([^/]+)$", path)
        if m:
            rid = m.group(1)
            self.store.delete_run(rid)
            self.store.audit(actor, "run_delete", target=rid)
            # a runner still pointing at the deleted run must not look busy forever
            self.store.ex("UPDATE runners SET status='idle', current_run=NULL "
                          "WHERE current_run=?", (rid,))
            self.bus.publish({"event": "run", "data": {"id": rid, "deleted": True}})
            return self._send(200, {"ok": True})
        m = re.match(r"^/api/v1/runners/([^/]+)$", path)
        if m:
            rid = m.group(1)
            self.store.delete_runner(rid)
            self.store.audit(actor, "runner_delete", target=rid)
            self.bus.publish({"event": "runner", "data": {"id": rid, "deleted": True}})
            return self._send(200, {"ok": True})
        return self._err(404, "not found")

    def do_POST(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        if not path.startswith("/api/"):
            return self._err(404, "not found")
        # The join token may register a runner and nothing else: it is accepted
        # only on /register, so it can never satisfy an operator gate elsewhere.
        if path == "/api/v1/register":
            op = self._is_operator()
            if not op:
                # SEC-P2-003: registration is throttled per client IP, but a
                # successful operator request is never limited.
                over = self._throttled("register")
                if over:
                    return over
            allowed = op or self._runner_ok() or self._is_join()
        else:
            allowed = self._is_operator() or self._runner_ok()
        if not allowed:
            return self._deny(401, "unauthorized")
        try:
            b = self._body()
        except BodyError as e:
            return self._err(e.code, e.msg)
        try:
            return self._api_post(path, q, b)
        except Exception as exc:
            self._log_error("POST", path, exc)
            return self._err(500, "internal server error")

    # -- readiness + metrics (OBS-P2-004 / OBS-P2-005) --------------------
    def _ready(self):
        """Readiness: can this instance serve? A SQLite read+write plus the
        existing git/version compare. Unlike /health this may do I/O, and it
        answers 503 when the DB is not usable."""
        db_ok, db_err = False, ""
        try:
            self.store.one("SELECT 1 AS x")
            with self.store.lock:
                self.store.conn.execute("CREATE TEMP TABLE IF NOT EXISTS _bench_ready(x INTEGER)")
                self.store.conn.execute("INSERT INTO _bench_ready(x) VALUES(1)")
                self.store.conn.execute("DROP TABLE _bench_ready")
                self.store.conn.commit()
            db_ok = True
        except Exception as exc:
            db_err = "%s: %s" % (type(exc).__name__, exc)
            self._log("error", "readiness db check failed", error=db_err,
                      traceback=traceback.format_exc())
        latest, git = latest_git(), server_git()
        compare = compare_commits(git, latest) if (git and latest) else ""
        out = {"ok": db_ok, "db": {"ok": db_ok}, "version": server_version(),
               "git": git, "latest": latest, "compare": compare,
               "protocol": PROTOCOL, "time": now()}
        if db_err:
            out["db"]["error"] = db_err
        return self._send(200 if db_ok else 503, out)

    def _metrics_text(self):
        """Prometheus text exposition (stdlib only). Counters live on the server
        instance; the DB gauges are read here, off the liveness path."""
        m = self._metrics_obj()
        with m.lock:
            reqs = dict(m.requests)
            authf, rl = m.auth_failures, m.rate_limited
        S = self.store
        try:
            audit_rows = S.one("SELECT COUNT(*) c FROM audit")["c"]
        except Exception:
            audit_rows = 0
        try:
            queued = S.one("SELECT COUNT(*) c FROM commands WHERE status='queued'")["c"]
        except Exception:
            queued = 0
        lines = [
            "# HELP benchd_uptime_seconds Seconds since the control plane started.",
            "# TYPE benchd_uptime_seconds gauge",
            "benchd_uptime_seconds %.3f" % m.uptime(),
            "# HELP benchd_requests_total HTTP requests by status class.",
            "# TYPE benchd_requests_total counter",
        ]
        for cls in ("1xx", "2xx", "3xx", "4xx", "5xx"):
            lines.append('benchd_requests_total{class="%s"} %d' % (cls, reqs.get(cls, 0)))
        lines += [
            "# HELP benchd_auth_failures_total Authentication failures (HTTP 401).",
            "# TYPE benchd_auth_failures_total counter",
            "benchd_auth_failures_total %d" % authf,
            "# HELP benchd_rate_limit_rejections_total Requests rejected by the rate limiter (HTTP 429).",
            "# TYPE benchd_rate_limit_rejections_total counter",
            "benchd_rate_limit_rejections_total %d" % rl,
            "# HELP benchd_audit_rows Rows in the operator audit trail.",
            "# TYPE benchd_audit_rows gauge",
            "benchd_audit_rows %d" % audit_rows,
            "# HELP benchd_command_queue_depth Commands queued and not yet sent to a runner.",
            "# TYPE benchd_command_queue_depth gauge",
            "benchd_command_queue_depth %d" % queued,
        ]
        return "\n".join(lines) + "\n"

    # -- GET api -----------------------------------------------------------
    def _api_get(self, path, q):
        S = self.store
        if path == "/api/v1/state":
            runners = [runner_public(r) for r in S.q("SELECT * FROM runners ORDER BY name")]
            active = [run_public(r) for r in
                      S.q("SELECT * FROM runs WHERE status='running' ORDER BY started DESC")]
            for a in active:
                a["latest"] = tele_public(S.one("SELECT * FROM telemetry WHERE run_id=? "
                                                "ORDER BY ts DESC LIMIT 1", (a["id"],)))
                a["phases"] = S.q("SELECT seq,grp,label,status,t0,t1,detail FROM phases "
                                  "WHERE run_id=? ORDER BY seq", (a["id"],))
                a["logs_tail"] = S.q("SELECT id,ts,stream,line FROM logs WHERE run_id=? "
                                     "ORDER BY id DESC LIMIT 200", (a["id"],))[::-1]
            recent = [run_public(r) for r in
                      S.q("SELECT * FROM runs ORDER BY started DESC LIMIT 25")]
            hosts = [h["host"] for h in S.q(
                "SELECT DISTINCT host FROM runs WHERE host IS NOT NULL ORDER BY host")]
            return self._send(200, {"runners": runners, "active": active, "recent": recent,
                                    "metrics": METRICS, "hosts": hosts,
                                    "server_git": server_git(), "latest_git": latest_git(),
                            "version": server_version(), "protocol": PROTOCOL,
                                    "server_time": now()})
        if path == "/api/v1/metrics":
            return self._send(200, {"metrics": METRICS})
        if path == "/api/v1/audit":
            # operator-only (the do_GET gate above), newest first
            lim = int_arg(q, "limit", 100, 1, 1000)
            rows = S.q("SELECT id,ts,actor,action,target,detail FROM audit "
                       "ORDER BY id DESC LIMIT ?", (lim,))
            return self._send(200, {"audit": rows})
        if path == "/api/v1/runners":
            return self._send(200, {"runners": [runner_public(r) for r in
                                                S.q("SELECT * FROM runners ORDER BY name")]})
        if path == "/api/v1/schedules":
            return self._send(200, {"schedules": [schedule_public(s) for s in
                                                  S.q("SELECT * FROM schedules ORDER BY created")]})
        if path == "/api/v1/runs":
            lim = int_arg(q, "limit", 100, 1, 1000)
            off = int_arg(q, "offset", 0, 0, 100000000)
            host = q.get("host", [None])[0]
            where, args = "", []
            if host:
                where, args = "WHERE host=?", [host]
            rows = [run_public(r) for r in S.q(
                "SELECT * FROM runs %s ORDER BY started DESC LIMIT ? OFFSET ?" % where,
                args + [lim, off])]
            total = S.one("SELECT COUNT(*) c FROM runs %s" % where, args)["c"]
            return self._send(200, {"runs": rows, "total": total})
        m = re.match(r"^/api/v1/runs/([^/]+)/telemetry\.csv$", path)
        if m:
            return self._telemetry_csv(m.group(1))
        m = re.match(r"^/api/v1/runs/([^/]+)/telemetry$", path)
        if m:
            return self._run_telemetry(m.group(1), q)
        m = re.match(r"^/api/v1/runs/([^/]+)/logs$", path)
        if m:
            return self._run_logs(m.group(1), q)
        m = re.match(r"^/api/v1/runs/([^/]+)/stats$", path)
        if m:
            return self._run_stats(m.group(1))
        m = re.match(r"^/api/v1/runs/([^/]+)/artifact/([^/]+)$", path)
        if m:
            return self._artifact_download(m.group(1), m.group(2))
        m = re.match(r"^/api/v1/runs/([^/]+)$", path)
        if m:
            run = S.one("SELECT * FROM runs WHERE id=?", (m.group(1),))
            if not run:
                return self._err(404, "no such run")
            run = run_public(run)
            run["phases"] = S.q("SELECT seq,grp,label,status,t0,t1,detail FROM phases "
                                "WHERE run_id=? ORDER BY seq", (m.group(1),))
            run["events"] = S.q("SELECT ts,level,message FROM events WHERE run_id=? "
                                "ORDER BY ts LIMIT 500", (m.group(1),))
            run["artifacts"] = S.q("SELECT name,kind,size,sha256 FROM artifacts "
                                   "WHERE run_id=?", (m.group(1),))
            run["logs"] = S.q("SELECT id,ts,stream,line FROM logs WHERE run_id=? "
                              "ORDER BY id LIMIT 4000", (m.group(1),))
            run["telemetry_stats"] = self._compute_stats(m.group(1))
            return self._send(200, run)
        if path == "/api/v1/trends":
            return self._trends(q)
        return self._err(404, "not found")

    def _runner_commands(self, rid):
        rows = self.store.q("SELECT id,run_id,kind,payload,status,created FROM commands "
                            "WHERE runner_id=? AND status='queued' ORDER BY created", (rid,))
        out = []
        for r in rows:
            try:
                r["payload"] = json.loads(r["payload"] or "{}")
            except Exception:
                r["payload"] = {}
            out.append(r)
        if out:
            self.store.ex("UPDATE commands SET status='sent', sent=? "
                          "WHERE runner_id=? AND status='queued'", (now(), rid))
        return self._send(200, {"commands": out})

    def _run_telemetry(self, run_id, q):
        maxn = int_arg(q, "max", 600, 1, 5000)
        rows = self.store.q("SELECT * FROM telemetry WHERE run_id=? ORDER BY ts", (run_id,))
        if len(rows) > maxn:
            step = len(rows) / float(maxn)
            picked, i = [], 0.0
            while int(i) < len(rows):
                picked.append(rows[int(i)])
                i += step
            if picked and picked[-1] is not rows[-1]:
                picked.append(rows[-1])
            rows = picked
        return self._send(200, {"run_id": run_id, "rows": [tele_public(r) for r in rows]})

    def _run_logs(self, run_id, q):
        after = int_arg(q, "after", 0, 0, 100000000)
        limit = int_arg(q, "limit", 2000, 1, 5000)
        rows = self.store.q("SELECT id,ts,stream,line FROM logs WHERE run_id=? AND id>? "
                            "ORDER BY id LIMIT ?", (run_id, after, limit))
        return self._send(200, {"run_id": run_id, "lines": rows})

    @staticmethod
    def _pct(sorted_vals, p):
        return pct(sorted_vals, p)

    def _derive(self, rows, series, stats):
        return derive_stats(rows, series, stats)

    def _compute_stats(self, run_id):
        st = compute_stats(self.store, run_id)
        if st.get("samples"):
            return st
        # Retention may have swept the samples. The copy materialised at that point
        # is what the run detail, Trends, Compare and the Fleet view read, so an old
        # run keeps its min/avg/max/p95/p99 and its derived metrics.
        row = self.store.one("SELECT stats FROM runs WHERE id=?", (run_id,))
        if row and row.get("stats"):
            try:
                return json.loads(row["stats"])
            except ValueError:
                pass
        return st


    def _run_stats(self, run_id):
        return self._send(200, dict({"run_id": run_id}, **self._compute_stats(run_id)))

    def _telemetry_csv(self, run_id):
        rows = self.store.q("SELECT ts,elapsed,temp_c,fan_pct,power_w,gpu_util,gpu_fan_rpm,"
                            "cpu_util,ram_used,sm_mhz FROM telemetry "
                            "WHERE run_id=? ORDER BY ts", (run_id,))
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["ts", "elapsed_s", "temp_c", "fan_pct", "power_w", "gpu_util_pct",
                    "gpu_fan_rpm", "cpu_util_pct", "ram_used_gib", "sm_mhz"])
        for r in rows:
            w.writerow([r[k] for k in ("ts", "elapsed", "temp_c", "fan_pct", "power_w",
                                       "gpu_util", "gpu_fan_rpm", "cpu_util", "ram_used",
                                       "sm_mhz")])
        return self._send(200, raw=buf.getvalue().encode(), ctype="text/csv",
                          headers={"Content-Disposition":
                                   'attachment; filename="%s-telemetry.csv"' % safe_filename(run_id)})

    def _artifact_download(self, run_id, name):
        a = self.store.one("SELECT * FROM artifacts WHERE run_id=? AND name=? "
                           "ORDER BY id DESC LIMIT 1", (run_id, name))
        if not a:
            return self._err(404, "no such artifact")
        ctype = {"json": "application/json", "text": "text/plain; charset=utf-8"}.get(
            a["kind"], "application/octet-stream")
        try:
            data = base64.b64decode(a["content"] or "", validate=True)
        except Exception:
            data = (a["content"] or "").encode("utf-8", "replace")  # legacy text rows
        return self._send(200, raw=data, ctype=ctype,
                          headers={"Content-Disposition":
                                   'attachment; filename="%s"' % safe_filename(a["name"])})

    def _trends(self, q):
        host = q.get("host", [None])[0]
        metrics = q.get("metric", []) or [m[0] for m in METRICS[:4]]
        rows = self.store.q("SELECT id,host,label,started,summary FROM runs "
                            "WHERE status!='running' ORDER BY started")
        series = {m: [] for m in metrics}
        for r in rows:
            if host and r["host"] != host:
                continue
            try:
                summ = json.loads(r["summary"] or "{}")
            except Exception:
                summ = {}
            for m in metrics:
                v = dotted(summ, m)
                if isinstance(v, bool):
                    v = 1 if v else 0
                if isinstance(v, (int, float)):
                    series[m].append({"t": r["started"], "v": v, "run": r["id"], "host": r["host"]})
        hosts = [h["host"] for h in self.store.q(
            "SELECT DISTINCT host FROM runs WHERE host IS NOT NULL ORDER BY host")]
        stats = {}
        for m, pts in series.items():
            vals = sorted(p["v"] for p in pts)
            if vals:
                n = len(vals)
                stats[m] = {"n": n, "min": vals[0], "max": vals[-1],
                            "avg": round(sum(vals) / n, 2),
                            "p50": self._pct(vals, 50), "p95": self._pct(vals, 95),
                            "p99": self._pct(vals, 99)}
        return self._send(200, {"hosts": hosts, "metrics": METRICS, "series": series,
                                "stats": stats})

    def _stream(self):
        q = self.bus.subscribe()
        if q is None:
            # capped: an explicit refusal beats an unbounded set of streams (and
            # EventSource retries on its own)
            return self._err(503, "too many live streams; retry shortly")
        self._metrics_obj().status(200)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Request-ID", self._request_id())
        origin = self._origin_allowed()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.end_headers()
        try:
            self._sse("snapshot", {"server_time": now()})
            while True:
                try:
                    frame = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                if frame is _SSE_OVERFLOW:
                    # this subscriber fell behind: close so the client reconnects
                    # and refetches, instead of losing events silently
                    break
                self.wfile.write(frame)
                self.wfile.flush()
        except Exception:
            # OBS-P2-003: a broken SSE client is logged, not swallowed.
            self._log("error", "sse stream failed", traceback=traceback.format_exc())
        finally:
            self.bus.unsubscribe(q)

    def _sse(self, event, data):
        self.wfile.write(("event: %s\ndata: %s\n\n" % (event, _json_dumps(data))).encode())
        self.wfile.flush()

    # -- POST api ----------------------------------------------------------
    def _api_post(self, path, q, b):
        S = self.store
        # a writer may only touch its own run, and only while it is running:
        # telemetry/phase/finish into a finished (or unknown) run is refused
        mrun = re.match(r"^/api/v1/runs/([^/]+)/(?:phase|telemetry|event|log|artifact|finish)$",
                        path)
        if mrun:
            run_id = mrun.group(1)
            row = S.one("SELECT status FROM runs WHERE id=?", (run_id,))
            if not row:
                return self._err(404, "no such run")
            if not self._may_write_run(run_id):
                return self._err(403, "not your run")
            if row["status"] != "running":
                return self._err(409, "run is %s, not running" % row["status"])
        if path == "/api/v1/register":
            return self._register(b)
        m = re.match(r"^/api/v1/runners/([^/]+)/heartbeat$", path)
        if m:
            rid = m.group(1)
            if not self._may_act_as(rid):
                return self._err(403, "not your runner")
            if not S.one("SELECT id FROM runners WHERE id=?", (rid,)):
                return self._err(404, "no such runner")
            fields, args = ["last_seen=?"], [now()]
            for k in ("status", "current_run", "capabilities", "version", "git"):
                if k in b:
                    fields.append("%s=?" % k)
                    args.append(_json_dumps(b[k]) if k == "capabilities" else b[k])
            args.append(rid)
            S.ex("UPDATE runners SET %s WHERE id=?" % ",".join(fields), args)
            r = runner_public(S.one("SELECT * FROM runners WHERE id=?", (rid,)))
            if r:
                # full record only when something changed; otherwise a slim
                # liveness update (capabilities etc. are not re-sent every 2 s)
                self.bus.publish_runner(r)
            pending = S.one("SELECT COUNT(*) c FROM commands WHERE runner_id=? AND status='queued'",
                            (rid,))["c"]
            return self._send(200, {"ok": True, "commands": pending, "server_time": now()})
        m = re.match(r"^/api/v1/server/update$", path)
        if m:
            if not self._is_operator():
                return self._err(403, "operator only")
            return self._server_update()
        m = re.match(r"^/api/v1/runners/([^/]+)/commands$", path)
        if m:
            if not self._is_operator():
                return self._err(403, "operator only")
            return self._issue_command(m.group(1), b)
        m = re.match(r"^/api/v1/runners/([^/]+)/commands/([^/]+)/ack$", path)
        if m:
            rid, cid = m.group(1), m.group(2)
            if not self._may_act_as(rid):
                return self._err(403, "not your command")
            cmd = S.one("SELECT runner_id FROM commands WHERE id=?", (cid,))
            if not cmd or cmd["runner_id"] != rid:
                return self._err(404, "no such command")
            S.ex("UPDATE commands SET status=?, acked=?, result=? WHERE id=?",
                 (b.get("status", "done"), now(), _json_dumps(b.get("result") or {}), cid))
            self.bus.publish({"event": "command", "data": {"id": cid, "runner_id": rid,
                                                           "status": b.get("status", "done"),
                                                           "result": b.get("result")}})
            return self._send(200, {"ok": True})
        if path == "/api/v1/schedules":
            if not self._is_operator():
                return self._err(403, "operator only")
            return self._create_schedule(b)
        if path == "/api/v1/runs":
            op, r = self._caller()
            rid = b.get("runner_id")
            if not op:
                if not r:
                    return self._deny(401, "unauthorized")
                if rid and rid != r["id"]:
                    return self._err(403, "not your runner")
                rid = r["id"]
            run_id = b.get("id") or str(uuid.uuid4())
            if not safe_id(run_id):
                return self._err(400, "bad run id")
            started = b.get("started")
            if started is None:
                started = now()
            else:
                # a string here used to land in a REAL column as TEXT, sorting
                # ahead of every real timestamp and never meeting the retention
                # cutoff; only a finite epoch is allowed
                try:
                    started = float(started)
                except (TypeError, ValueError):
                    return self._err(400, "bad started timestamp")
                if not math.isfinite(started) or started <= 0:
                    return self._err(400, "bad started timestamp")
            profile = b.get("profile") if isinstance(b.get("profile"), dict) else {}
            cur = S.ex("INSERT INTO runs(id,runner_id,host,label,profile,started,status,platform,"
                       "gpu,driver,torch,system,preset) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                       "ON CONFLICT(id) DO NOTHING",
                       (run_id, rid, b.get("host"), b.get("label"), _json_dumps(profile),
                        started, "running", b.get("platform"), b.get("gpu"),
                        b.get("driver"), b.get("torch"),
                        _json_dumps(b.get("system") if isinstance(b.get("system"), dict) else {}),
                        b.get("preset") or profile.get("preset")))
            if not cur.rowcount:
                # a retry of POST /runs with the same client id must be idempotent,
                # not a 500 IntegrityError
                existing = S.one("SELECT runner_id FROM runs WHERE id=?", (run_id,))
                if not op and (not existing or existing["runner_id"] != rid):
                    return self._err(409, "run id already in use")
                return self._send(200, {"run_id": run_id, "existing": True})
            if rid:
                S.ex("UPDATE runners SET status='busy', current_run=? WHERE id=?", (run_id, rid))
            run = run_public(S.one("SELECT * FROM runs WHERE id=?", (run_id,)))
            if run:
                self.bus.publish({"event": "run", "data": run})
            return self._send(200, {"run_id": run_id})
        m = re.match(r"^/api/v1/runs/([^/]+)/phase$", path)
        if m:
            run_id = m.group(1)
            seq = int_arg({"seq": [b.get("seq", 0)]}, "seq", 0, hi=100000)
            grp, label, t0 = b.get("group"), b.get("label"), b.get("t0")
            # the unique (run_id, seq) index makes this an upsert: a re-posted
            # phase updates in place, and group/label/t0 are applied too instead
            # of being silently ignored on update
            S.ex("INSERT INTO phases(run_id,seq,grp,label,status,t0,t1,detail) "
                 "VALUES(?,?,?,?,?,?,?,?) "
                 "ON CONFLICT(run_id,seq) DO UPDATE SET "
                 "grp=CASE WHEN ? THEN excluded.grp ELSE phases.grp END, "
                 "label=CASE WHEN ? THEN excluded.label ELSE phases.label END, "
                 "status=excluded.status, "
                 "t0=CASE WHEN ? THEN excluded.t0 ELSE phases.t0 END, "
                 "t1=excluded.t1, detail=excluded.detail",
                 (run_id, seq, grp, label, b.get("status", "running"),
                  t0 or now(), b.get("t1"), b.get("detail"),
                  1 if grp is not None else 0,
                  1 if label is not None else 0,
                  1 if t0 is not None else 0))
            self.bus.publish({"event": "phase", "data": {
                "run_id": run_id, "seq": seq, "group": grp, "label": label,
                "status": b.get("status", "running"), "detail": b.get("detail")}})
            return self._send(200, {"ok": True})
        m = re.match(r"^/api/v1/runs/([^/]+)/telemetry$", path)
        if m:
            run_id = m.group(1)
            samples = b.get("samples") or []
            if not isinstance(samples, list):
                return self._err(400, "samples must be a list")
            if len(samples) > MAX_SAMPLES:
                return self._err(413, "too many samples (%d > %d)" % (len(samples), MAX_SAMPLES))
            rows = []
            named = []
            for s in samples:
                ts = None
                if isinstance(s, dict):
                    s = sanitise_sample(dict(s))
                    core = [s.get(f) for f in SAMPLE_FIELDS]
                    extra = {k: v for k, v in s.items()
                             if k not in SAMPLE_FIELDS and k != "ts"
                             and isinstance(v, (int, float, str)) and not isinstance(v, bool)}
                    if isinstance(s.get("ts"), (int, float)) and math.isfinite(float(s["ts"])):
                        ts = float(s["ts"])
                    named.append(dict(s))
                else:
                    d = dict(zip(SAMPLE_FIELDS,
                                 (list(s) + [None] * len(SAMPLE_FIELDS))[:len(SAMPLE_FIELDS)]))
                    sanitise_sample(d)
                    core = [d.get(f) for f in SAMPLE_FIELDS]
                    extra = {}
                    named.append(dict(d))
                rows.append([run_id, ts or now()] + core
                            + [_json_dumps(extra) if extra else None])
            with S.lock:
                S.conn.executemany(
                    "INSERT INTO telemetry(run_id,ts,elapsed,temp_c,fan_pct,power_w,gpu_util,"
                    "gpu_fan_rpm,cpu_util,ram_used,sm_mhz,extra) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                S.conn.commit()
            if named:
                # coalesced: one event per run per tick carrying every sample
                # since the last one (a batch-per-event fan-out was unbounded)
                self.bus.publish_telemetry(run_id, named, len(rows))
            return self._send(200, {"ok": True, "accepted": len(rows)})
        m = re.match(r"^/api/v1/runs/([^/]+)/event$", path)
        if m:
            S.ex("INSERT INTO events(run_id,ts,level,message) VALUES(?,?,?,?)",
                 (m.group(1), b.get("ts") or now(), b.get("level", "info"), b.get("message", "")))
            self.bus.publish({"event": "event", "data": {"run_id": m.group(1),
                                                         "level": b.get("level", "info"),
                                                         "message": b.get("message", "")}})
            return self._send(200, {"ok": True})
        m = re.match(r"^/api/v1/runs/([^/]+)/log$", path)
        if m:
            run_id = m.group(1)
            lines = b.get("lines") or ([b["line"]] if b.get("line") else [])
            if not isinstance(lines, list):
                return self._err(400, "lines must be a list")
            if len(lines) > MAX_LOG_LINES:
                return self._err(413, "too many log lines (%d > %d)"
                                 % (len(lines), MAX_LOG_LINES))
            lines = [str(x)[:4000] for x in lines]
            stream = b.get("stream", "out")
            if lines:
                with S.lock:
                    S.conn.executemany("INSERT INTO logs(run_id,ts,stream,line) VALUES(?,?,?,?)",
                                       [(run_id, now(), stream, x) for x in lines])
                    S.conn.commit()
                self.bus.publish({"event": "log", "data": {"run_id": run_id, "stream": stream,
                                                           "lines": lines}})
            return self._send(200, {"ok": True, "n": len(lines)})
        m = re.match(r"^/api/v1/runs/([^/]+)/artifact$", path)
        if m:
            run_id = m.group(1)
            name = b.get("name", "artifact")
            content = b.get("content") or ""
            try:
                raw = base64.b64decode(content, validate=True) if content else b""
            except Exception:
                raw = b""
            # a retried upload replaces the stored copy (the unique index makes
            # this an upsert); without it the stale first copy was served
            S.ex("INSERT INTO artifacts(run_id,name,kind,size,sha256,content) VALUES(?,?,?,?,?,?) "
                 "ON CONFLICT(run_id,name) DO UPDATE SET kind=excluded.kind, size=excluded.size, "
                 "sha256=excluded.sha256, content=excluded.content",
                 (run_id, name, b.get("kind", "json"), len(raw), b.get("sha256"), content))
            return self._send(200, {"ok": True})
        m = re.match(r"^/api/v1/runs/([^/]+)/finish$", path)
        if m:
            run_id = m.group(1)
            ended = b.get("ended")
            if ended is None:
                ended = now()
            else:
                try:
                    ended = float(ended)
                except (TypeError, ValueError):
                    return self._err(400, "bad ended timestamp")
                if not math.isfinite(ended) or ended <= 0:
                    return self._err(400, "bad ended timestamp")
            system_json = _json_dumps(b.get("system") if isinstance(b.get("system"), dict) else {})
            # WHERE status='running' makes a second finish a no-op instead of
            # overwriting the first summary/ended (the status gate above already
            # rejects it; this closes the concurrent-finish race)
            cur = S.ex("UPDATE runs SET status=?, ended=?, exit_code=?, summary=?, "
                       "platform=COALESCE(?,platform), gpu=COALESCE(?,gpu), "
                       "driver=COALESCE(?,driver), torch=COALESCE(?,torch), "
                       "system=CASE WHEN ?='{}' THEN system ELSE ? END "
                       "WHERE id=? AND status='running'",
                       (b.get("status", "done"), ended, b.get("exit_code"),
                        _json_dumps(b.get("summary") if isinstance(b.get("summary"), dict) else {}),
                        b.get("platform"), b.get("gpu"), b.get("driver"), b.get("torch"),
                        system_json, system_json, run_id))
            if not cur.rowcount:
                return self._err(409, "run is not running")
            run = S.one("SELECT runner_id FROM runs WHERE id=?", (run_id,))
            if run and run["runner_id"]:
                # only clear the runner's assignment if it still names this run
                # (a newer run must not be knocked off by an old finish)
                S.ex("UPDATE runners SET status='idle', current_run=NULL "
                     "WHERE id=? AND (current_run=? OR current_run IS NULL)",
                     (run["runner_id"], run_id))
            run = run_public(S.one("SELECT * FROM runs WHERE id=?", (run_id,)))
            if run:
                self.bus.publish({"event": "run", "data": run})
            return self._send(200, {"ok": True})
        return self._err(404, "not found")

    def _register(self, b):
        S = self.store
        rid = b.get("runner_id")
        if rid and not safe_id(rid):
            rid = None
        key = b.get("api_key")
        kind = b.get("kind", "agent")
        mid = (b.get("machine_id") or "").strip() or None
        inst = (b.get("instance") or "").strip()
        existing = None
        if rid:
            existing = S.one("SELECT * FROM runners WHERE id=?", (rid,))
            # ownership may be proved by the body's api_key or by the bearer that
            # authenticated this request; either must match THIS row's key
            proof = key or self._bearer()
            if existing and not self._token_eq(proof, existing["api_key"]) \
                    and not self._is_operator():
                # An explicit runner_id is only honoured by its own key or an
                # operator. Any valid runner bearer passes the POST gate, so
                # accepting it blind let one runner take over another's row - and
                # the response then handed it that runner's api_key. Adoption by
                # machine_id below is unaffected (that is how a live stick with no
                # persisted config reclaims its own row).
                existing = None
                rid = None
            elif existing and key and not self._token_eq(key, existing["api_key"]):
                existing = None  # stale/wrong key -> treat as new
        if not existing and mid:
            # Stable identity: adopt the row this machine already owns for this
            # role *and instance*, so a re-registering runner (or an ephemeral
            # live-USB boot whose config did not persist) does not mint a
            # duplicate — while the live USB's tty1 / ttyS0 dashboards stay
            # distinct (they must, or a command would run on both).
            #
            # SEC-P1-002 / CHAIN-P1-001: adoption must never hand a runner's
            # api_key to a caller that cannot prove it owns the row. Ownership is
            # proved by the body's api_key or the request bearer matching the row
            # (the same proof the explicit-runner_id path requires above); an
            # operator is trusted. Without proof we only adopt a row that is
            # *offline* (stale last_seen) and we rotate its key, so a live runner
            # can never be taken over and an offline one recovers with a new key.
            cand = S.one("SELECT * FROM runners WHERE machine_id=? AND kind=? "
                         "AND COALESCE(instance,'')=?", (mid, kind, inst))
            if cand:
                proof = key or self._bearer()
                if proof and secrets.compare_digest(str(proof), str(cand["api_key"])):
                    existing = cand
                elif self._is_operator():
                    existing = cand
                elif not online(cand["last_seen"]):
                    existing = dict(cand)
                    existing["api_key"] = secrets.token_urlsafe(32)
        if existing:
            rid = existing["id"]
            key = existing["api_key"]
        else:
            rid = rid or str(uuid.uuid4())
            key = secrets.token_urlsafe(32)
        # api_key and current_run are part of the upsert: a re-register that
        # changed either must actually store what it returns (an operator rotation
        # used to mint a key that was never persisted, so it 401ed) and a stale
        # current_run must not survive it
        S.ex("INSERT INTO runners(id,name,os,kind,version,capabilities,first_seen,last_seen,"
             "status,current_run,addr,git,api_key,machine_id,ephemeral,instance) "
             "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
             "ON CONFLICT(id) DO UPDATE SET name=excluded.name, os=excluded.os, kind=excluded.kind, "
             "version=excluded.version, capabilities=excluded.capabilities, last_seen=excluded.last_seen, "
             "status=excluded.status, current_run=excluded.current_run, addr=excluded.addr, "
             "git=excluded.git, api_key=excluded.api_key, "
             "machine_id=excluded.machine_id, ephemeral=excluded.ephemeral, instance=excluded.instance",
             (rid, b.get("name"), b.get("os"), kind, b.get("version"),
              _json_dumps(b.get("capabilities") if isinstance(b.get("capabilities"), dict) else {}),
              now(), now(),
              b.get("status", "idle"), b.get("current_run"), self.client_address[0],
              b.get("git"), key, mid, 1 if b.get("ephemeral") else 0, inst))
        r = runner_public(S.one("SELECT * FROM runners WHERE id=?", (rid,)))
        self.bus.publish_runner(r)
        S.audit(self._actor(), "register", target=rid,
                detail="%s/%s" % (kind, inst) if inst else kind)
        return self._send(200, {"runner_id": rid, "api_key": key, "poll_interval": 2.0,
                                "telemetry_interval": 1.0, "server_time": now()})

    def _server_update(self):
        """Pull the control plane's own checkout and restart it (see server_pull).

        POST only (a GET is prefetchable/retryable) and serialised: a second
        request while a pull is in flight gets 409 instead of racing git.
        """
        if not _SERVER_UPDATE_LOCK.acquire(blocking=False):
            return self._err(409, "an update is already in progress")
        try:
            return self._send(200, server_pull())
        finally:
            _SERVER_UPDATE_LOCK.release()

    def _issue_command(self, rid, b):
        S = self.store
        if not S.one("SELECT id FROM runners WHERE id=?", (rid,)):
            return self._err(404, "no such runner")
        kind = b.get("kind")
        if kind not in ("start", "stop", "update", "fan_curve", "power_limit", "restart",
                        "shutdown", "reboot", "exec", "shell",
                        "update_toolkit", "iso_fetch", "iso_write", "iso_write_slot"):
            return self._err(400, "unknown command kind")
        cid = str(uuid.uuid4())
        S.ex("INSERT INTO commands(id,runner_id,kind,payload,status,created) VALUES(?,?,?,?,?,?)",
             (cid, rid, kind, _json_dumps(b.get("payload") or {}), "queued", now()))
        self.bus.publish({"event": "command", "data": {"id": cid, "runner_id": rid,
                                                       "kind": kind, "status": "queued"}})
        S.audit(self._actor(), "command", target=rid, detail=kind)
        return self._send(200, {"id": cid, "status": "queued"})

    def _create_schedule(self, b):
        S = self.store
        rid = b.get("runner_id")
        if not rid or not S.one("SELECT id FROM runners WHERE id=?", (rid,)):
            return self._err(400, "runner_id required")
        kind = b.get("kind", "every")
        every_s = int_arg({"e": [b.get("every_s") or 0]}, "e", 0, hi=30 * 86400)
        at_time = b.get("at_time") or "03:00"
        sid = str(uuid.uuid4())
        nxt = (next_daily(at_time) if kind == "daily"
               else now() + max(60, every_s))
        S.ex("INSERT INTO schedules(id,runner_id,label,profile,kind,every_s,at_time,next_run,"
             "enabled,last_run,created,last_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
             (sid, rid, b.get("label") or "scheduled run",
              _json_dumps(b.get("profile") if isinstance(b.get("profile"), dict) else {}),
              kind, every_s, at_time, nxt, 1 if b.get("enabled", True) else 0, None, now(), None))
        s = schedule_public(S.one("SELECT * FROM schedules WHERE id=?", (sid,)))
        self.bus.publish({"event": "schedule", "data": s})
        S.audit(self._actor(), "schedule_create", target=sid, detail=rid)
        return self._send(200, {"id": sid, "next_run": nxt})

    # -- static ------------------------------------------------------------
    def _static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        rel = path.lstrip("/")
        root = os.path.normpath(self.web_dir)
        full = os.path.normpath(os.path.join(root, rel))
        # a raw prefix test let a sibling directory whose name merely starts with
        # "web" through ("/../webX/secret" normalised inside it); compare real paths
        try:
            inside = os.path.commonpath([full, root]) == root
        except ValueError:
            inside = False
        if not inside or not os.path.isfile(full):
            return self._err(404, "not found")
        ctype = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
                 ".ico": "image/x-icon", ".png": "image/png"}.get(
            os.path.splitext(full)[1], "application/octet-stream")
        with open(full, "rb") as fb:
            return self._send(200, raw=fb.read(), ctype=ctype)


# ------------------------------------------------------------------ scheduler --
def scheduler_tick(store, bus):
    """One pass: queue a start command for every due schedule. Returns the count."""
    fired = 0
    due = store.q("SELECT * FROM schedules WHERE enabled=1 AND next_run<=?", (now(),))
    for s in due:
        every = max(60, s["every_s"] or 3600)
        rr = store.one("SELECT last_seen, status FROM runners WHERE id=?", (s["runner_id"],))
        if not rr or not online(rr["last_seen"]):
            # don't pile commands onto a runner that isn't there
            store.ex("UPDATE schedules SET next_run=?, last_status=? WHERE id=?",
                     (now() + every, "skipped: runner offline", s["id"]))
            bus.publish({"event": "schedule", "data": schedule_public(
                store.one("SELECT * FROM schedules WHERE id=?", (s["id"],)))})
            continue
        busy = rr["status"] == "busy" or bool(store.one(
            "SELECT id FROM commands WHERE runner_id=? AND kind='start' AND status='queued' "
            "LIMIT 1", (s["runner_id"],)))
        if busy:
            store.ex("UPDATE schedules SET next_run=?, last_status=? WHERE id=?",
                     (now() + every, "skipped: busy", s["id"]))
            bus.publish({"event": "schedule", "data": schedule_public(
                store.one("SELECT * FROM schedules WHERE id=?", (s["id"],)))})
            continue
        cmd = str(uuid.uuid4())
        store.ex("INSERT INTO commands(id,runner_id,kind,payload,status,created) "
                 "VALUES(?,?,?,?,?,?)",
                 (cmd, s["runner_id"], "start", s["profile"] or "{}", "queued", now()))
        nxt = (next_daily(s["at_time"]) if s["kind"] == "daily" else now() + every)
        store.ex("UPDATE schedules SET next_run=?, last_run=?, last_status=? WHERE id=?",
                 (nxt, now(), "queued", s["id"]))
        bus.publish({"event": "command", "data": {"id": cmd, "runner_id": s["runner_id"],
                                                  "kind": "start", "status": "queued"}})
        bus.publish({"event": "schedule", "data": schedule_public(
            store.one("SELECT * FROM schedules WHERE id=?", (s["id"],)))})
        fired += 1
    return fired


# How long the bulky per-sample data of a FINISHED run is kept. The run row and its
# summary survive - that is what trends and history read - so pruning the per-sample
# tables bounds the database without losing the record of a run. A 15-minute run
# writes ~4.5 MB of telemetry at 4 Hz and nothing ever pruned it.
# BENCHD_RETENTION_DAYS=0 keeps everything.
def env_float(name, default, lo=0.0):
    """Read a float from the environment without letting a typo crash the unit.

    A bare float() at import time turned `BENCHD_RETENTION_DAYS=30d` into a
    traceback on every restart (the service crash-looped); a bad value now logs
    and falls back to the default.
    """
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        v = None
    if v is None or not math.isfinite(v) or v < lo:
        print("[benchd] WARNING: ignoring bad %s=%r - using %s" % (name, raw, default),
              flush=True)
        return default
    return v


RETENTION_DAYS = env_float("BENCHD_RETENTION_DAYS", 30.0)
RETENTION_EVERY_S = 3600.0        # a full sweep does not need to run every tick
_RETENTION_LAST = [0.0]


def server_pull():
    """Pull the control plane's own checkout and hand the restart to systemd.

    Only meaningful for a git deploy; a copy deploy has no history to pull, so it says
    so rather than pretending. The restart is delayed so a response flushes before the
    process it is answering from goes away. The fetch is full (tags + ancestry, never
    --depth) so `git describe` and the fleet's commit-compare keep working; a checkout
    left shallow by an earlier deploy is healed first.
    """
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if not os.path.isdir(os.path.join(root, ".git")):
        return {"ok": False, "mode": "copy",
                "message": "copy deploy - refresh the files and restart benchd"}
    before = server_git()

    def git(*args):
        r = subprocess.run(["git", "-C", root] + list(args), capture_output=True,
                           text=True, timeout=180)
        if r.returncode != 0:
            # a failed fetch/pull used to be reported as ok: the code never looked
            # at the return code, so the message claimed a pull that never happened
            tail = [l for l in ((r.stderr or "") + (r.stdout or "")).strip().splitlines() if l]
            raise RuntimeError("git %s failed (rc=%d): %s"
                               % (" ".join(args), r.returncode, (tail[-1] if tail else "")[:160]))
        return r

    try:
        # Never fetch with --depth: it grafts the checkout down to one history-less
        # commit, which breaks `git describe` (the release version) and the fleet's
        # commit-compare/staleness ancestry walks. Earlier deploys did a depth-1
        # fetch, so heal a checkout that is already shallow before pulling.
        if os.path.exists(os.path.join(root, ".git", "shallow")):
            try:
                git("fetch", "--unshallow", "--tags", "origin")
            except RuntimeError as e:
                # a concurrent fetch can complete the repo between the check and
                # this command; "already complete" is success, not an error
                if "unshallow" not in str(e) and "complete" not in str(e):
                    raise
        git("fetch", "--tags", "origin")
        r = git("pull", "--ff-only")
    except Exception as e:
        return {"ok": False, "message": "git failed: %s" % e}
    _SERVER_GIT["v"] = None                        # re-read the commit we are now on
    after = server_git()
    restart = bool(after) and after != before
    if restart:
        # the unit is deploy-specific (a dev box may run benchd@lab, the lab box
        # runs benchd); BENCHD_UNIT overrides the default
        unit = os.environ.get("BENCHD_UNIT") or "benchd"
        if not re.match(r"^[A-Za-z0-9_.@-]+$", unit):
            unit = "benchd"
        try:
            subprocess.Popen(["bash", "-c", "sleep 1; systemctl restart %s" % unit],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except Exception:
            pass
    out = [l for l in (r.stdout or "").strip().splitlines() if l.strip()]
    return {"ok": True, "before": before, "after": after, "restarting": restart,
            "message": (out[-1] if out else "pulled")[:200]}


# Auto-update: pull our own checkout when it is behind, at most once an hour, and
# never while a run is active (the restart costs that run's live view). A copy deploy
# does nothing. BENCHD_AUTO_UPDATE=0 disables it.
AUTO_UPDATE_OFF = (os.environ.get("BENCHD_AUTO_UPDATE") or "").lower() in (
    "0", "off", "no", "false")
AUTO_UPDATE_S = env_float("BENCHD_AUTO_UPDATE_HOURS", 1.0) * 3600.0
_AUTO_UPDATE_LAST = [0.0]
# the scheduler runs auto_update on a worker thread (a 180 s git timeout must not
# stall the tick loop); this keeps two attempts from overlapping
_AUTO_UPDATE_BUSY = threading.Lock()


def auto_update(store, force=False):
    """Pull the control plane's own checkout when it is behind. Returns a dict or None."""
    if AUTO_UPDATE_OFF or AUTO_UPDATE_S <= 0:
        return None
    t = now()
    if not force and t - _AUTO_UPDATE_LAST[0] < AUTO_UPDATE_S:
        return None
    if not (latest_git() and server_git() and latest_git() != server_git()):
        _AUTO_UPDATE_LAST[0] = t        # up to date: do not re-check on every tick
        return None
    busy = store.one("SELECT COUNT(*) n FROM runs WHERE status='running'")["n"]
    if busy:
        return None                     # a run is in progress: try again next tick
    res = server_pull()
    if res.get("ok"):
        _AUTO_UPDATE_LAST[0] = t
    else:
        # do not consume the whole interval on a failed pull: retry in 5 minutes
        # (or the configured interval, if that is shorter)
        _AUTO_UPDATE_LAST[0] = t - AUTO_UPDATE_S + min(AUTO_UPDATE_S, 300.0)
    if res.get("ok") and res.get("restarting"):
        print("[benchd] auto-update: %s -> %s, restarting" % (res.get("before"), res.get("after")),
              flush=True)
    return res


def _auto_update_worker(store):
    try:
        auto_update(store)
    except Exception as e:
        print("[benchd] auto-update error: %s" % e, flush=True)
    finally:
        _AUTO_UPDATE_BUSY.release()


def retain_old_data(store, force=False):
    """Delete bulky per-run data of finished runs older than RETENTION_DAYS.

    Running runs are never touched, and neither are the run rows themselves: each
    one keeps its stats (min/avg/max/p95/p99 and the derived metrics), materialised
    here just before its samples go, so only the rows that grow with run length and
    sample rate are dropped. Artifacts hold base64 content in the same database and
    terminal commands accumulate forever, so both are swept too.
    """
    days = RETENTION_DAYS
    if days <= 0:
        return 0
    t = now()
    if not force and t - _RETENTION_LAST[0] < RETENTION_EVERY_S:
        return 0
    _RETENTION_LAST[0] = t
    cutoff = t - days * 86400
    old = store.q("SELECT id FROM runs WHERE status != 'running' AND "
                  "COALESCE(ended, started) < ?", (cutoff,))
    gone = 0
    for r in old:
        # Materialise the run's stats before its samples go: the detail page, Trends,
        # Compare and Fleet read them, and they are tiny next to the rows they sum up.
        st = compute_stats(store, r["id"])
        if st.get("samples"):
            store.ex("UPDATE runs SET stats=? WHERE id=?", (_json_dumps(st), r["id"]))
        for table in ("telemetry", "logs", "events", "artifacts"):
            # the table names are literals here, never caller input
            cur = store.ex("DELETE FROM %s WHERE run_id=?" % table, (r["id"],))
            if cur.rowcount and cur.rowcount > 0:
                gone += cur.rowcount
    # commands have no run row to outlive: prune the terminal ones by their own age
    cur = store.ex("DELETE FROM commands WHERE status IN ('done','failed') AND "
                   "COALESCE(acked, sent, created) < ?", (cutoff,))
    if cur.rowcount and cur.rowcount > 0:
        gone += cur.rowcount
    if gone:
        print("[benchd] retention: removed %d row(s) older than %.0f days"
              % (gone, days), flush=True)
    return gone


def reap_stale_runs(store, bus):
    """Fail runs whose runner stopped heartbeating (e.g. a dashboard was killed).

    Backstop so a closed/killed runner never leaves a run 'running' forever.
    """
    t = now()
    for r in store.q("SELECT id, runner_id, started FROM runs WHERE status='running'"):
        if t - (r["started"] or 0) < 60:
            continue  # too new to judge
        last = None
        if r["runner_id"]:
            rr = store.one("SELECT last_seen FROM runners WHERE id=?", (r["runner_id"],))
            last = rr["last_seen"] if rr else None
        if last is not None and (t - last) <= 45:
            # The runner is alive, so the run is fine - unless the runner is no
            # longer on it. A runner restarted mid-run (an update, say) comes back,
            # registers and goes idle, and the heartbeat rule above can never see
            # the run it left behind; worse, if it then picks up a newer run its
            # current_run points elsewhere and the old run stayed 'running' for
            # ever, blocking retention and auto-update.
            rr = store.one("SELECT status, current_run FROM runners WHERE id=?",
                           (r["runner_id"],))
            if rr and rr["current_run"] != r["id"]:
                m = store.one("SELECT MAX(ts) m FROM telemetry WHERE run_id=?", (r["id"],))
                quiet = (m or {}).get("m") or (r["started"] or t)
                if t - quiet > 60:
                    store.ex("UPDATE runs SET status='failed', ended=? WHERE id=?",
                             (t, r["id"]))
                    if (rr.get("current_run") or "") == r["id"]:
                        # only possible if the condition above is relaxed later;
                        # never leave a runner pointing at a reaped run
                        store.ex("UPDATE runners SET status='idle', current_run=NULL "
                                 "WHERE id=?", (r["runner_id"],))
                    bus.publish({"event": "run", "data": run_public(
                        store.one("SELECT * FROM runs WHERE id=?", (r["id"],)))})
                    print("[benchd] reaped orphaned run %s (runner alive, moved on)"
                          % r["id"][:8], flush=True)
            continue  # runner still heartbeating
        store.ex("UPDATE runs SET status='failed', ended=? WHERE id=?", (t, r["id"]))
        if r["runner_id"]:
            store.ex("UPDATE runners SET status='idle', current_run=NULL WHERE id=?",
                     (r["runner_id"],))
        bus.publish({"event": "run",
                     "data": run_public(store.one("SELECT * FROM runs WHERE id=?", (r["id"],)))})


def reap_ephemeral_runners(store, bus):
    """Delete one-shot runners (e.g. `--self-test`) once they stop heartbeating,
    so test registrations never accumulate in the fleet view."""
    t = now()
    for r in store.q("SELECT id, last_seen FROM runners WHERE ephemeral=1"):
        if (t - (r["last_seen"] or 0)) > 60:
            store.delete_runner(r["id"])
            bus.publish({"event": "runner", "data": {"id": r["id"], "deleted": True}})


# how long a runner may hold a command before it counts as dropped. Downloads
# and raw disk writes legitimately take minutes, so they get a real budget
# instead of the two-minute default meant for start/stop.
ACK_TIMEOUT_S = 120
SLOW_ACK_TIMEOUT_S = {
    "iso_fetch": 3 * 3600,      # multi-GB download + SHA256
    "iso_write": 3600,          # raw device write
    "iso_write_slot": 3600,     # raw slot write + grub flip
    "update_toolkit": 900,      # tarball fetch + switch
    "update": 900,
    "exec": 300,
    "shell": 300,
}


def ack_timeout(kind, payload=None):
    """Seconds a command of this kind may stay 'sent' before it is reaped."""
    limit = SLOW_ACK_TIMEOUT_S.get(kind, ACK_TIMEOUT_S)
    if kind in ("exec", "shell"):
        try:
            limit = max(limit, int(json.loads(payload or "{}").get("timeout") or 0) + 60)
        except Exception:
            pass
    return limit


def reap_stale_commands(store, bus):
    """Fail commands a runner fetched but never acked (its process died, or the
    UI thread dropped it) so they don't sit invisible in 'sent' forever."""
    t = now()
    for c in store.q("SELECT id, runner_id, kind, sent, payload FROM commands WHERE status='sent'"):
        if c["sent"] and (t - c["sent"]) > ack_timeout(c["kind"], c["payload"]):
            store.ex("UPDATE commands SET status='failed', result=? WHERE id=?",
                     (json.dumps({"error": "no ack from the runner (dropped?)"}), c["id"]))
            bus.publish({"event": "command", "data": {"id": c["id"], "runner_id": c["runner_id"],
                                                      "kind": c["kind"], "status": "failed"}})


def scheduler_loop(store, bus, stop):
    """Fire due schedules by queueing a start command to the target runner."""
    while not stop.is_set():
        try:
            scheduler_tick(store, bus)
            reap_stale_runs(store, bus)
            reap_ephemeral_runners(store, bus)
            reap_stale_commands(store, bus)
            retain_old_data(store)
            # a pull can block for the git timeout (180 s): run it off this thread
            # so schedules/reaping keep ticking. One attempt at a time.
            if not AUTO_UPDATE_OFF and _AUTO_UPDATE_BUSY.acquire(blocking=False):
                threading.Thread(target=_auto_update_worker, args=(store,),
                                 daemon=True).start()
        except Exception as e:
            print("[benchd] scheduler error: %s" % e, flush=True)
        stop.wait(10.0)


# -------------------------------------------------------------------- config --
def find_conf_value(key):
    """First key=value for `key` in the config files; inline comments stripped.

    The file handle used to be left open (one leaked descriptor per candidate that
    existed) and `BENCHD_TOKEN=abc  # rotated` kept the comment as part of the
    token.
    """
    for cand in (os.path.join(HERE, "benchd.conf"), os.path.expanduser("~/.config/benchd.conf"),
                 "/etc/benchd.conf"):
        if not os.path.isfile(cand):
            continue
        try:
            with open(cand, encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for ln in lines:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            if not ln.startswith(key + "="):
                continue
            val = ln.split("=", 1)[1].strip()
            if val[:1] in ('"', "'"):
                end = val.find(val[0], 1)
                return val[1:end] if end > 0 else val.strip(val[0])
            return val.split("#", 1)[0].strip()
    return None


def find_token(cli_token):
    return (cli_token or os.environ.get("BENCHD_TOKEN")
            or find_conf_value("BENCHD_TOKEN"))


def auth_refusal(token, allow_anonymous):
    """None when serving is allowed; an actionable message when it is refused.

    A missing or misspelled BENCHD_TOKEN used to only print a warning: the server
    then served on 0.0.0.0 with every auth check open (register, exec, shutdown),
    so one typo turned the fleet controller into an open endpoint.
    """
    if token or allow_anonymous:
        return None
    return ("no BENCHD_TOKEN configured - refusing to serve an open control plane "
            "(anyone who can reach the port could register runners and run commands). "
            "Fix: set BENCHD_TOKEN in the environment, pass --token TOKEN, or add "
            "BENCHD_TOKEN=... to server/benchd.conf. For a deliberately open local "
            "instance, pass --allow-anonymous.")


def find_join_token(cli_token):
    """The registration-only join token; None when unset (feature disabled)."""
    return (cli_token or os.environ.get("BENCHD_JOIN_TOKEN")
            or find_conf_value("BENCHD_JOIN_TOKEN"))


def find_cors_origin(cli_origin):
    """Exact dashboard origin allowed to read CORS responses; None disables it.

    Same-origin requests are always allowed (SEC-P2-002); this only adds one
    explicitly configured cross-origin dashboard.
    """
    return (cli_origin or os.environ.get("BENCHD_CORS_ORIGIN")
            or find_conf_value("BENCHD_CORS_ORIGIN"))


def main():
    ap = argparse.ArgumentParser(description="bench-test control plane")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8722)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--token", default=None, help="operator API bearer (required "
                    "unless --allow-anonymous)")
    ap.add_argument("--join-token", default=None,
                    help="registration-only join token: accepted by POST "
                         "/api/v1/register and nothing else, so it is safe to bake "
                         "into a public release")
    ap.add_argument("--cors-origin", default=None,
                    help="one exact dashboard origin allowed to read CORS "
                         "responses (same-origin is always allowed); default is "
                         "none, so no wildcard Access-Control-Allow-Origin is sent")
    ap.add_argument("--web", default=WEB_DIR)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--allow-anonymous", action="store_true",
                    help="serve without BENCHD_TOKEN (dangerous: anyone can control the fleet)")
    a = ap.parse_args()


    token = find_token(a.token)
    join_token = find_join_token(a.join_token)
    cors_origin = find_cors_origin(a.cors_origin)
    refusal = auth_refusal(token, a.allow_anonymous)
    if refusal:
        print("[benchd] %s" % refusal, file=sys.stderr, flush=True)
        return 2
    store = Store(a.db)
    Handler.store, Handler.bus, Handler.token, Handler.web_dir = store, Bus(), token, a.web
    Handler.join_token = join_token
    Handler.cors_origin = cors_origin
    httpd = ThreadingHTTPServer((a.host, a.port), Handler)
    httpd.quiet = a.quiet
    httpd.daemon_threads = True

    stop = threading.Event()
    threading.Thread(target=scheduler_loop, args=(store, Handler.bus, stop), daemon=True).start()

    print("[benchd] listening on http://%s:%d  db=%s  auth=%s"
          % (a.host, a.port, a.db, "token+login" if token else "OPEN (no token)"), flush=True)
    if join_token:
        print("[benchd] join token enabled: POST /api/v1/register accepts it "
              "(registration only)", flush=True)
    if not token:
        print("[benchd] WARNING: --allow-anonymous is set - anyone who can reach "
              "%s:%d can register/control" % (a.host, a.port), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
