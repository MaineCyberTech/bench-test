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
import collections
import csv
import hashlib
import io
import json
import os
import queue
import re
import secrets
import socket
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
# DATA-P2-003 / DR-P2-002 / EVOL-P2-003: the schema marker stored in SQLite's
# PRAGMA user_version. Bump it whenever a change to SCHEMA below needs the code
# to notice an older (upgrade in place) or newer (refuse to start) database.
# There is deliberately no migration framework: one integer, checked on startup.
# v2 adds runners.key_issued (ACM-P2-001/002: per-runner key age for --key-ttl-days).
SCHEMA_VERSION = 2
WEB_DIR = os.path.join(HERE, "web")
DEFAULT_DB = os.path.join(HERE, "data", "benchd.sqlite")

# A request body larger than this is refused before it is read, so a declared-but-
# never-sent Content-Length cannot pin a handler thread and its memory. Artifacts
# (telemetry CSV, sensors dump, a screenshot) post separately and fit comfortably.
MAX_BODY = 24 * 1024 * 1024

# ADMIN-P2-002: destructive commands are gated in the browser only, so a script or
# API client could power-cycle a machine or overwrite a disk in one request. These
# kinds now require the caller to echo an explicit confirmation in the payload.
DESTRUCTIVE_COMMANDS = ("shutdown", "reboot", "iso_write", "iso_write_slot")

# ADMIN-P2-004 / ADMIN-P2-006: exec/shell is fleet-wide remote code execution with
# no approval path. An operator may now constrain it with --exec-allowlist /
# --exec-denylist (both unset by default = allow all, as before).
EXEC_COMMANDS = ("exec", "shell")

# FILE-P2-002: an artifact upload had no size, count or name validation. These are
# the defaults; --artifact-max-bytes / --artifact-quota override them.
DEFAULT_ARTIFACT_MAX_BYTES = 25 * 1024 * 1024
DEFAULT_ARTIFACT_QUOTA = 200

# RES-P3-003: bound how long a handler thread may block on the client socket.
# Without it, a request that declares a Content-Length but never sends the body
# leaves rfile.read() (and its thread) waiting forever. Applies per socket
# operation, so a long-lived SSE writer is unaffected.
REQUEST_TIMEOUT_S = 30

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
            return self.auth_failures

    def rate_limit(self):
        with self.lock:
            self.rate_limited += 1

    def uptime(self):
        return time.time() - self.started


# OBS-P2-001 / IR-P2-001 / NOTIF-P2-001: optional, disabled-by-default alert
# webhook. When a URL is configured (--alert-webhook / $BENCHD_ALERT_WEBHOOK) the
# control plane POSTs a small JSON alert out of band on a failed run finish, a
# failing /ready probe, a burst of auth failures, or a capacity threshold. Each
# alert is delivered from a daemon thread with a short timeout and is rate-limited
# per kind, so a slow or dead webhook can never block, slow or alter a request.
ALERT_RATE_WINDOW = 300.0   # at most one alert per kind per 5 minutes
ALERT_TIMEOUT_S = 5.0       # urllib timeout for one webhook POST
ALERT_AUTH_BURST = 10       # failed auth attempts that count as a burst


class Alerter:
    """Out-of-band alert webhook (disabled unless a URL is set).

    ``fire`` is safe to call from any request path: it only takes a short lock,
    consults a per-kind rate limit, and hands the actual POST to a daemon thread,
    so an unreachable webhook cannot slow or fail a request.
    """

    def __init__(self, url=None, window=ALERT_RATE_WINDOW, timeout=ALERT_TIMEOUT_S,
                 db_bytes_limit=0):
        self.url = url or None
        self.window = float(window)
        self.timeout = float(timeout)
        self.db_bytes_limit = int(db_bytes_limit or 0)
        self.lock = threading.Lock()
        self.last = {}          # kind -> ts of the last delivered alert
        self.counts = {}        # kind -> alerts delivered
        self.suppressed = 0     # alerts dropped by the rate limit
        self.failures = 0       # webhook POSTs that raised

    @property
    def enabled(self):
        return bool(self.url)

    def fire(self, kind, **fields):
        """Deliver one alert of `kind` from a daemon thread; rate-limited.

        Returns True when an alert was queued, False when the webhook is disabled
        or the kind is still inside its rate-limit window. Never raises.
        """
        if not self.url:
            return False
        t = now()
        with self.lock:
            if t - self.last.get(kind, 0.0) < self.window:
                self.suppressed += 1
                return False
            self.last[kind] = t
            self.counts[kind] = self.counts.get(kind, 0) + 1
        payload = {"alert": kind, "ts": round(t, 3)}
        payload.update(fields)
        try:
            threading.Thread(target=self._post, args=(payload,),
                             name="benchd-alert", daemon=True).start()
        except Exception:
            pass
        return True

    def _post(self, payload):
        """POST one alert; only ever runs on the daemon thread, never raises."""
        try:
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                self.url, data=body, method="POST",
                headers={"Content-Type": "application/json", "User-Agent": "benchd"})
            with urllib.request.urlopen(req, timeout=self.timeout):
                pass
        except Exception as exc:
            with self.lock:
                self.failures += 1
            log_json("warning", "alert webhook failed", alert=payload.get("alert"),
                     error="%s: %s" % (type(exc).__name__, exc))

    def totals(self):
        with self.lock:
            return {"fired": sum(self.counts.values()),
                    "suppressed": self.suppressed, "failed": self.failures}


# OBS-P2-004: /health is a cheap liveness probe. Its expensive fields (per-table
# row counts, db size and the cached "latest main" commit) are computed off the
# request path and refreshed by a daemon thread, so a liveness probe can never
# block on a SQLite lock or on the GitHub compare's 8s timeout.
_HEALTH_LOCK = threading.Lock()
_HEALTH = {"capacity": None, "runners": 0, "latest": "", "git": "", "version": "",
           "version_source": ""}
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
        version_source = server_version_source()
        latest = latest_git() if with_network else _HEALTH.get("latest", "")
    except Exception:
        return
    with _HEALTH_LOCK:
        _HEALTH.update({"capacity": cap, "runners": runners, "git": git,
                        "version": version, "version_source": version_source,
                        "latest": latest})
    # OBS-P2-001 / NOTIF-P2-001: a capacity threshold fires an out-of-band alert.
    # Checked here (off the request path) so a probe never pays for it; the
    # per-kind rate limit keeps a full disk from spamming the webhook.
    a = getattr(store, "alerter", None)
    if a is not None and a.db_bytes_limit and cap.get("db_bytes", 0) > a.db_bytes_limit:
        a.fire("capacity", db_bytes=cap.get("db_bytes"), limit=a.db_bytes_limit)


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


class BadInput(Exception):
    """A write request carried a client value of the wrong shape.

    API-P3-001 / DATA-P3-001: raised by the write handlers (a non-list
    ``samples``, a non-string ``kind``, ...) so the dispatcher can answer a
    generic 400 instead of letting a TypeError/AttributeError surface as a 500.
    """


class SchemaVersionError(RuntimeError):
    """The database on disk was written by a newer build than this one.

    Raised on startup so the process exits non-zero rather than reading (and
    possibly corrupting) a schema it does not understand.
    """


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
    """Null out physically impossible percentage readings, in place."""
    for k in _PCT_0_100:
        v = s.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and not (0 <= v <= 100):
            s[k] = None
    return s

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
  machine_id TEXT, ephemeral INTEGER DEFAULT 0, instance TEXT,
  key_issued REAL
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, runner_id TEXT, host TEXT, label TEXT, profile TEXT,
  started REAL, ended REAL, status TEXT, exit_code INTEGER,
  platform TEXT, gpu TEXT, driver TEXT, torch TEXT, summary TEXT, system TEXT,
  preset TEXT, stats TEXT
);
CREATE TABLE IF NOT EXISTS phases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT REFERENCES runs(id) ON DELETE CASCADE, seq INTEGER, grp TEXT,
  label TEXT, status TEXT, t0 REAL, t1 REAL, detail TEXT
);
CREATE TABLE IF NOT EXISTS telemetry (
  run_id TEXT REFERENCES runs(id) ON DELETE CASCADE, ts REAL, elapsed REAL,
  temp_c REAL, fan_pct REAL, power_w REAL,
  gpu_util REAL, gpu_fan_rpm REAL, cpu_util REAL, ram_used REAL,
  sm_mhz REAL, extra TEXT
);
CREATE TABLE IF NOT EXISTS logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT REFERENCES runs(id) ON DELETE CASCADE, ts REAL, stream TEXT, line TEXT
);
CREATE TABLE IF NOT EXISTS commands (
  id TEXT PRIMARY KEY, runner_id TEXT,
  run_id TEXT REFERENCES runs(id) ON DELETE CASCADE, kind TEXT, payload TEXT,
  status TEXT, created REAL, sent REAL, acked REAL, result TEXT,
  attempts INTEGER
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT REFERENCES runs(id) ON DELETE CASCADE, ts REAL, level TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT REFERENCES runs(id) ON DELETE CASCADE, name TEXT, kind TEXT,
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
CREATE INDEX IF NOT EXISTS idx_runs_status_started ON runs(status, started);
CREATE INDEX IF NOT EXISTS idx_runs_runner ON runs(runner_id);
CREATE INDEX IF NOT EXISTS idx_phases_run ON phases(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_tele_run ON telemetry(run_id, ts);
CREATE INDEX IF NOT EXISTS idx_cmd_runner ON commands(runner_id, status);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, ts);
CREATE INDEX IF NOT EXISTS idx_logs_run ON logs(run_id, id);
CREATE INDEX IF NOT EXISTS idx_logs_run_ts ON logs(run_id, ts);
CREATE INDEX IF NOT EXISTS idx_sched_next ON schedules(enabled, next_run);
"""

# Every table that references a run by run_id. Deleting a run clears all of them
# in one transaction, so nothing is left orphaned and readable by run id.
RUN_DEPENDENT_TABLES = ("phases", "telemetry", "logs", "events", "artifacts", "commands")


# PRIV-P2-005: a per-runner API key is only ever stored as its sha256 digest.
# What a client presents (body or bearer) is hashed before it is compared, so a
# stolen database cannot be replayed as a credential.
_KEY_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def key_digest(key):
    """The sha256 hex digest of a per-runner API key."""
    return hashlib.sha256(str(key).encode("utf-8")).hexdigest()


def is_key_digest(value):
    """True when value is already a stored digest (64 lowercase hex chars)."""
    return bool(value) and bool(_KEY_DIGEST_RE.match(str(value)))


# --------------------------------------------------------------------- store --
class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.lock = threading.RLock()
        self.path = path
        # PRIV-P2-005: plaintext keys minted in THIS process, so an operator that
        # re-registers a machine_id it just created still gets the same key back.
        # Never persisted - the database only ever holds key_digest(key).
        self.key_plain = {}
        # OBS-P2-001 / IR-P2-001 / NOTIF-P2-001: the alert webhook lives with the
        # store so the background /health refresher (which owns the capacity
        # numbers) and the request path share one rate-limited sender. Disabled
        # until main()/a test installs a URL.
        self.alerter = Alerter()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            # DATA-P2-002: enforce the REFERENCES in SCHEMA on this connection.
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")
            fresh = not self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone()
            if not fresh:
                try:
                    self._check_schema_version()
                except SchemaVersionError:
                    self.conn.close()
                    raise
            self.conn.executescript(SCHEMA)
            self._migrate()
            self.conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
            self.conn.commit()
            self._ensure_phase_index()
        # OBS-P2-004: prime the cheap /health cache and start its refresher.
        start_health_refresher(self)

    def _schema_version(self):
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def _check_schema_version(self):
        """DATA-P2-003 / DR-P2-002 / EVOL-P2-003: fail closed on a newer schema.

        PRAGMA user_version is the only marker (no migration framework). A newer
        database may hold columns this build does not understand, so starting
        would risk writing it back in a lossy way; an older one is upgraded in
        place and noted.
        """
        cur = self._schema_version()
        if cur > SCHEMA_VERSION:
            raise SchemaVersionError(
                "database schema version %d is newer than this build supports (%d); "
                "upgrade benchd or point --db at a matching database"
                % (cur, SCHEMA_VERSION))
        if cur < SCHEMA_VERSION:
            print("[benchd] schema version %d -> %d: upgrading in place"
                  % (cur, SCHEMA_VERSION), flush=True)

    def _ensure_phase_index(self):
        """DATA-P2-004: make (run_id, seq) unique so a replayed phase is idempotent.

        A legacy database may already hold duplicate (run_id, seq) rows; the
        index then cannot be built, and phase writes keep the old behaviour. That
        is a warning, never a startup failure.
        """
        try:
            self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_phases_run_seq "
                              "ON phases(run_id, seq)")
            self.conn.commit()
        except sqlite3.Error as exc:
            try:
                self.conn.rollback()
            except Exception:
                pass
            log_json("warning", "could not create the unique phase index; duplicate "
                     "(run_id, seq) rows exist, phase writes stay non-idempotent",
                     error=str(exc))

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
        # ACM-P2-001/002: when a per-runner key was issued, so --key-ttl-days can
        # expire it. A key of unknown age is given a fresh window on upgrade (an
        # operator can rotate it); never break an existing key by guessing.
        if "key_issued" not in cols:
            self.conn.execute("ALTER TABLE runners ADD COLUMN key_issued REAL")
            self.conn.execute("UPDATE runners SET key_issued=strftime('%s','now') "
                              "WHERE key_issued IS NULL")
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
        # RES-P3-001: how many times a fetched-but-unacked command was re-queued.
        ccols = {r[1] for r in self.conn.execute("PRAGMA table_info(commands)")}
        if "attempts" not in ccols:
            self.conn.execute("ALTER TABLE commands ADD COLUMN attempts INTEGER")
        # PRIV-P2-005: replace any plaintext api_key left by an older build with
        # its digest, so the runner's existing key keeps authenticating.
        for r in self.conn.execute("SELECT id, api_key FROM runners "
                                   "WHERE api_key IS NOT NULL AND api_key!=''").fetchall():
            if not is_key_digest(r["api_key"]):
                self.conn.execute("UPDATE runners SET api_key=? WHERE id=?",
                                  (key_digest(r["api_key"]), r["id"]))

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
                invalidate_run_cache(run_id)
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
# PERF-P2-002: bound the realtime fan-out. A hard cap on concurrent SSE clients
# keeps a burst of dashboards from spawning unbounded handler threads, and
# telemetry (the high-rate event) is coalesced per subscriber so a slow reader
# can never make the server buffer whole batches without limit.
MAX_SSE_SUBSCRIBERS = 32
SSE_QUEUE_MAX = 1000
TELEMETRY_BATCH_MAX = 1000


class BusFull(Exception):
    """Raised by Bus.subscribe() when MAX_SSE_SUBSCRIBERS is already reached."""


def _merge_telemetry(a, b):
    """Coalesce two pending telemetry batches into one event.

    Same run: concatenate the samples (bounded) and sum the counts. A different
    run cannot be merged, so the newest batch wins - the older one is counted as
    dropped rather than queued.
    """
    da, db = a.get("data") or {}, b.get("data") or {}
    if da.get("run_id") != db.get("run_id"):
        return b
    samples = list(da.get("samples") or []) + list(db.get("samples") or [])
    merged = dict(db)
    merged["samples"] = samples[-TELEMETRY_BATCH_MAX:]
    merged["sample"] = db.get("sample", da.get("sample"))
    merged["n"] = (da.get("n") or 0) + (db.get("n") or 0)
    return {"event": "telemetry", "data": merged}


class Subscriber:
    """One SSE client's mailbox.

    Non-telemetry events queue in order (bounded, oldest dropped on overflow);
    telemetry is kept in a single pending slot and merged on arrival, so at most
    one telemetry batch is ever pending per subscriber. Drops are counted.
    """

    def __init__(self, maxsize=SSE_QUEUE_MAX):
        self.cv = threading.Condition()
        self.events = collections.deque()
        self.maxsize = maxsize
        self.telemetry = None
        self.dropped = 0

    def offer(self, ev):
        """Queue one event; return the number dropped or merged (0 or 1)."""
        with self.cv:
            if ev.get("event") == "telemetry":
                if self.telemetry is None:
                    self.telemetry = ev
                    self.cv.notify()
                    return 0
                self.telemetry = _merge_telemetry(self.telemetry, ev)
                self.dropped += 1
                self.cv.notify()
                return 1
            if len(self.events) >= self.maxsize:
                self.events.popleft()
                self.dropped += 1
                self.cv.notify()
                return 1
            self.events.append(ev)
            self.cv.notify()
            return 0

    def get(self, timeout):
        """Next event, or raise queue.Empty after `timeout` seconds."""
        deadline = time.time() + timeout
        with self.cv:
            while self.telemetry is None and not self.events:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise queue.Empty()
                self.cv.wait(remaining)
            if self.telemetry is not None:
                ev, self.telemetry = self.telemetry, None
                return ev
            return self.events.popleft()

    def pending(self):
        with self.cv:
            return len(self.events) + (1 if self.telemetry is not None else 0)


class Bus:
    def __init__(self, max_subscribers=MAX_SSE_SUBSCRIBERS):
        self.lock = threading.Lock()
        self.subs = set()
        self.max_subscribers = max_subscribers
        self.dropped_lock = threading.Lock()
        self.dropped = 0

    def subscribe(self):
        s = Subscriber()
        with self.lock:
            if len(self.subs) >= self.max_subscribers:
                raise BusFull("subscriber limit (%d) reached" % self.max_subscribers)
            self.subs.add(s)
        return s

    def unsubscribe(self, s):
        with self.lock:
            self.subs.discard(s)

    def publish(self, ev):
        with self.lock:
            subs = list(self.subs)
        dropped = 0
        for s in subs:
            dropped += s.offer(ev)
        if dropped:
            with self.dropped_lock:
                self.dropped += dropped

    def dropped_count(self):
        with self.dropped_lock:
            return self.dropped

    def subscriber_count(self):
        with self.lock:
            return len(self.subs)


# ------------------------------------------------------------------ helpers ---
def now():
    return time.time()


def online(last_seen):
    return (now() - (last_seen or 0)) < ONLINE_WINDOW


_SERVER_GIT = {"v": None}
_SERVER_VER = {"v": None, "src": None}


def _stamp_text(repo, rel):
    """A deploy stamp file as a dict ({} when absent or unreadable).

    ``.bench-version`` is JSON (``{"version", "commit", ...}``); a plain-text
    stamp such as ``.deployed-commit`` is a single token. One helper so the
    commit and the release resolve from the same stamp.
    """
    try:
        with open(os.path.join(repo, rel), encoding="utf-8") as fh:
            txt = fh.read().strip()
    except OSError:
        return {}
    if not txt:
        return {}
    if txt.startswith("{"):
        try:
            obj = json.loads(txt)
            return obj if isinstance(obj, dict) else {}
        except ValueError:
            return {}
    tok = txt.split()[0]
    return {"commit": tok, "version": tok}


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
                val = str(_stamp_text(repo, rel).get("commit") or "").strip()
                if val:
                    break
        _SERVER_GIT["v"] = val
    return _SERVER_GIT["v"]


def _read_server_version(repo):
    """(version, source) for a control-plane checkout or a copy-deploy.

    A copy-deployed server has no .git and the release workflow may not have
    shipped the repo-root ``VERSION`` next to it, so the release also resolves
    from the same ``.bench-version`` stamp ``server_git`` reads for the commit
    (the stamp a copy/app deploy bakes). Mirrors ``bench/benchreport.py:
    bench_version``; a failure sentinel is never echoed as a release, and with
    neither source the caller reports ``0.0.0+unknown``.
    """
    try:
        with open(os.path.join(repo, "VERSION"), encoding="utf-8") as fh:
            val = fh.read().strip().splitlines()[0].strip()
        if val:
            return val, "VERSION"
    except (OSError, IndexError):
        pass
    val = str(_stamp_text(repo, ".bench-version").get("version") or "").strip()
    if val.endswith(("+unknown", "+invalid")):
        val = ""
    return (val, ".bench-version") if val else ("", "")


def server_version(root=None):
    """The control plane's release version, from ``VERSION`` or a deploy stamp.

    The server is deployed as a copy (no .git), so the version is what identifies
    a deployment; ``root`` lets a caller (or a test) point at a copy. With
    neither a ``VERSION`` file nor a ``.bench-version`` stamp the answer is
    ``0.0.0+unknown`` - never an invented release (OBS-P3-003).
    """
    if root is not None:
        val, _src = _read_server_version(root)
        return val or "0.0.0+unknown"
    if _SERVER_VER["v"] is None:
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        val, src = _read_server_version(repo)
        _SERVER_VER["v"] = val or "0.0.0+unknown"
        _SERVER_VER["src"] = src or "none"
    return _SERVER_VER["v"]


def server_version_source():
    """Where server_version() resolved from: 'VERSION', '.bench-version' or 'none'."""
    server_version()
    return _SERVER_VER["src"]


REPO_SLUG = os.environ.get("BENCH_REPO_SLUG") or "MaineCyberTech/bench-test"
GH_API = os.environ.get("BENCH_GH_API") or "https://api.github.com"
_REF = {"v": None, "t": 0.0, "pending": False}
REF_TTL = 120.0
# PERF-P2-004: the GitHub ref/compare lookups must never run on a request thread.
# Their urllib timeout is 8s, four times the 2s runner heartbeat, so a slow or
# offline GitHub used to stall heartbeats and liveness probes. Results are cached
# with a TTL and refreshed by a single background daemon; a request with an empty
# cache answers "unknown" (or the server's own commit) immediately instead of
# waiting. BENCH_OFFLINE=1 still skips the network entirely.
COMPARE_TTL = 600.0
_CMP = {}                 # (base, head) -> {"v": (status, files), "t": ts}
_CMP_PENDING = set()      # keys queued or in flight for the background worker
_CMP_LOCK = threading.Lock()
_VERSION_Q = queue.Queue()
_VERSION_THREAD = {"t": None}


def _compare_fetch(base, head):
    """Blocking GitHub compare. Only ever called from the background worker."""
    status, files = "", []
    if os.environ.get("BENCH_OFFLINE") != "1":
        try:
            url = "%s/repos/%s/compare/%s...%s" % (GH_API, REPO_SLUG, base, head)
            req = urllib.request.Request(url, headers={"User-Agent": "benchd",
                                                       "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=8) as r:
                j = json.loads(r.read().decode())
            status = str(j.get("status") or "")
            files = [str(f.get("filename") or "") for f in (j.get("files") or [])]
        except Exception:
            status, files = "", []
    return status, files


def _latest_fetch():
    """Blocking GitHub `commits/main`. Only ever called from the worker."""
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
    return val


def _version_worker():
    """Single daemon thread: the only place the request path's GitHub data is
    fetched, so no request ever blocks on it (PERF-P2-004)."""
    while True:
        kind, base, head = _VERSION_Q.get()
        try:
            if kind == "ref":
                val = _latest_fetch()
                # server_git() can shell out on its first call; keep it off the
                # lock so a request thread is never stuck behind it.
                git = server_git()
                with _CMP_LOCK:
                    _REF["v"] = val or _REF.get("v") or git
                    _REF["t"] = time.time()
                    _REF["pending"] = False
            else:
                v = _compare_fetch(base, head)
                with _CMP_LOCK:
                    if len(_CMP) > 500:
                        _CMP.clear()
                    _CMP[(base, head)] = {"v": v, "t": time.time()}
                    _CMP_PENDING.discard((base, head))
        except Exception:
            with _CMP_LOCK:
                _CMP_PENDING.discard((base, head))
                if kind == "ref":
                    _REF["pending"] = False
        finally:
            _VERSION_Q.task_done()


def _kick_version(kind, base=None, head=None):
    """Queue a background refresh, starting the worker lazily."""
    t = _VERSION_THREAD["t"]
    if t is None or not t.is_alive():
        with _CMP_LOCK:
            t = _VERSION_THREAD["t"]
            if t is None or not t.is_alive():
                t = threading.Thread(target=_version_worker, name="benchd-version",
                                     daemon=True)
                _VERSION_THREAD["t"] = t
                t.start()
    _VERSION_Q.put((kind, base, head))


def compare_detail(base, head):
    """(status, files) for GitHub compare `base...head` - never blocks.

    status is ahead|behind|diverged|identical|'' (unknown); `files` are the changed
    paths GitHub reports (possibly truncated, [] when unknown) - enough to tell a
    docs-only difference from a real one. A cached result is served (stale ones
    are refreshed in the background); an unseen pair answers ("", []) at once and
    queues the lookup, so a request never waits on GitHub.
    """
    if not base or not head:
        return "", []
    if base == head:
        return "identical", []
    key = (base, head)
    with _CMP_LOCK:
        ent = _CMP.get(key)
        stale = ent is None or (time.time() - ent["t"]) >= COMPARE_TTL
        need = stale and key not in _CMP_PENDING
        if need:
            _CMP_PENDING.add(key)
    if need:
        _kick_version("compare", base, head)
    return ent["v"] if ent is not None else ("", [])


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
    """Newest commit on main (cached) — what a runner should be running.

    Never blocks a request: a fresh cached tip is returned, otherwise the
    server's own commit is the cheap fallback while a background refresh is
    queued (PERF-P2-004). Set BENCH_OFFLINE=1 to skip the lookup entirely.
    """
    t = time.time()
    v = _REF.get("v")
    if v and not force and (t - _REF.get("t", 0.0)) < REF_TTL:
        return v
    with _CMP_LOCK:
        pending = _REF.get("pending", False)
        if not pending:
            _REF["pending"] = True
    if not pending:
        _kick_version("ref")
    return v or server_git()


ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def safe_id(v):
    """Ids supplied by clients must be opaque tokens, never HTML-bearing text."""
    return bool(v) and bool(ID_RE.match(str(v)))


# API-P3-001 / DATA-P3-001: client-supplied fields are checked for shape before
# they reach SQLite. A wrong type is a BadInput (-> 400), never a 500.
def field_str(b, key, default=None):
    """A string field, or `default` when absent; a non-string is a 400."""
    v = b.get(key, default)
    if v is None:
        return default
    if isinstance(v, str):
        return v
    raise BadInput("'%s' must be a string" % key)


def field_list(b, key):
    """A list field (default []); anything else is a 400."""
    v = b.get(key)
    if v is None:
        return []
    if not isinstance(v, list):
        raise BadInput("'%s' must be a list" % key)
    return v


def field_dict(b, key):
    """An object field (default {}); anything else is a 400."""
    v = b.get(key)
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise BadInput("'%s' must be an object" % key)
    return v


def safe_filename(n):
    """Sanitise a name used in a Content-Disposition header."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(n or "artifact"))[:120]


def bad_artifact_name(name):
    """FILE-P2-002: True when an artifact name is empty or a path, not a bare name.

    Rejects an empty/whitespace name, an absolute path, any directory separator
    (``/`` or ``\\``) and any ``..`` segment, so an uploaded name can never escape
    the per-run namespace it is stored and downloaded under.
    """
    if not isinstance(name, str) or not name.strip():
        return True
    if name in (".", "..") or "/" in name or "\\" in name or ".." in name:
        return True
    return os.path.isabs(name)


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


# OBS-P2-001 / IR-P2-001: a finished run is a failure when its status says so, or
# when the summary carries a failing verdict. The run status is the primary signal
# (the GUI/agent report "failed"); the full-system soak verdict and a string
# ``verdict`` field are secondary, so a "clean: false" soak also alerts.
FAILED_RUN_STATUSES = ("failed", "error", "aborted", "timeout", "cancelled", "canceled")


def run_failed(status, summary):
    """True when a finished run counts as a failure (status or verdict)."""
    if str(status or "").lower() in FAILED_RUN_STATUSES:
        return True
    if isinstance(summary, dict):
        if dotted(summary, "fullsoak.result.verdict.clean") is False:
            return True
        v = summary.get("verdict")
        if isinstance(v, str) and v.lower() in ("failed", "fail", "unclean", "not-clean"):
            return True
    return False


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
        if t is not None and isinstance(w, (int, float)):
            pts.append((float(t), float(w)))
    if len(pts) >= 2:
        j = 0.0
        for (t0, w0), (t1, w1) in zip(pts, pts[1:]):
            dt = t1 - t0
            if dt > 0:
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


# PERF-P2-001: the per-run detail, stats and telemetry/CSV endpoints each load and
# re-parse every telemetry row on the request thread. The parsed results are cached
# per run, keyed by (run_id, telemetry_generation, kind): every telemetry write
# bumps the run's in-memory generation, so a repeat request reuses the parsed value
# with no extra DB read until new telemetry arrives. The generation is an
# equivalent, cheaper invalidation than a row count, and cached objects are
# returned unchanged, so responses stay byte-identical.
_STATS_CACHE = {}
_STATS_CACHE_LOCK = threading.Lock()
STATS_CACHE_MAX = 64
_TELE_GEN = {}          # run_id -> telemetry generation (bumped on every write)


def clear_stats_cache():
    with _STATS_CACHE_LOCK:
        _STATS_CACHE.clear()
        _TELE_GEN.clear()


def bump_run_cache(run_id):
    """Mark a run's cached artifacts stale: new telemetry arrived for it."""
    with _STATS_CACHE_LOCK:
        _TELE_GEN[run_id] = _TELE_GEN.get(run_id, 0) + 1
        for k in [k for k in _STATS_CACHE if k[0] == run_id]:
            _STATS_CACHE.pop(k, None)


def invalidate_run_cache(run_id):
    """Drop a run's cached artifacts and its telemetry generation (on delete)."""
    with _STATS_CACHE_LOCK:
        _TELE_GEN.pop(run_id, None)
        for k in [k for k in _STATS_CACHE if k[0] == run_id]:
            _STATS_CACHE.pop(k, None)


def _run_gen(run_id):
    with _STATS_CACHE_LOCK:
        return _TELE_GEN.get(run_id, 0)


def _cache_get(key):
    with _STATS_CACHE_LOCK:
        return _STATS_CACHE.get(key)


def _cache_put(key, value):
    with _STATS_CACHE_LOCK:
        if len(_STATS_CACHE) >= STATS_CACHE_MAX:
            _STATS_CACHE.clear()
        _STATS_CACHE[key] = value


def compute_stats_cached(store, run_id):
    """compute_stats(store, run_id) behind a (run_id, generation) cache.

    Only runs with samples are cached: an empty run's stored-stats fallback is a
    single cheap row read, and caching it would risk serving a stale empty result
    after retention materialises the stats. A telemetry write bumps the run's
    generation, so the next request naturally misses the cache.
    """
    key = (run_id, _run_gen(run_id), "stats")
    hit = _cache_get(key)
    if hit is not None:
        return hit
    st = compute_stats(store, run_id)
    if st.get("samples"):
        _cache_put(key, st)
    return st


def compute_stats(store, run_id):
    """Per-field min/avg/max/p95/p99 plus derived metrics for a run's telemetry.

    Module level, not a handler method, so the retention sweep can materialise a
    run's stats before its samples are deleted.
    """
    rows = store.q("SELECT * FROM telemetry WHERE run_id=? ORDER BY ts", (run_id,))
    series = {}
    for r in rows:
        for k, v in tele_public(r).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
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
class Handler(BaseHTTPRequestHandler):
    server_version = "benchd/2.0"
    protocol_version = "HTTP/1.0"
    store = None
    bus = None
    token = None
    join_token = None
    cors_origin = None
    web_dir = WEB_DIR
    # ADMIN-P2-004 / ADMIN-P2-006: optional exec/shell command gates. Both None
    # (the default) keeps the previous allow-all behaviour; set by main() from
    # --exec-allowlist / --exec-denylist (or the matching config keys).
    exec_allowlist = None          # compiled regex: only a match is accepted
    exec_denylist = None           # compiled regex: any match is rejected
    # ACM-P2-001 / ACM-P2-002: per-runner key lifetime in days; 0 = never expires.
    key_ttl_days = 0
    # FILE-P2-002: artifact upload limits (bytes per artifact, count per run).
    artifact_max_bytes = DEFAULT_ARTIFACT_MAX_BYTES
    artifact_quota = DEFAULT_ARTIFACT_QUOTA
    # RES-P3-003: per-connection socket timeout (StreamRequestHandler applies it).
    timeout = REQUEST_TIMEOUT_S

    # -- plumbing ----------------------------------------------------------
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

    def _alert(self, kind, **fields):
        """Fire an out-of-band alert (OBS-P2-001), if one is configured.

        Best-effort and non-blocking: the alert is queued to a daemon thread by the
        Alerter, and a failure to deliver never touches the request path.
        """
        a = getattr(self.store, "alerter", None)
        if a is not None:
            try:
                a.fire(kind, **fields)
            except Exception:
                pass

    def _metrics_obj(self):
        m = getattr(self.server, "_bench_metrics", None)
        if m is None:
            m = Metrics()
            self.server._bench_metrics = m
        return m

    def _send(self, code, obj=None, raw=None, ctype="application/json", headers=None,
              cache="no-store"):
        self._metrics_obj().status(code)
        body = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # PERF-P3-001: API responses are never cached; static assets pass their
        # own short-lived policy (see _static).
        self.send_header("Cache-Control", cache)
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
            n = self._metrics_obj().auth_failure()
            allowed, retry = self._limited("auth")
            try:
                if self.store is not None:
                    self.store.audit("anonymous", "auth_fail", target=self.path)
            except Exception:
                # OBS-P2-003: an audit write must never be silently lost.
                self._log("error", "audit write failed", traceback=traceback.format_exc())
            # OBS-P2-001 / IR-P2-001: a burst of auth failures is alertable. The
            # per-kind rate limit keeps this to one alert per window.
            if n >= ALERT_AUTH_BURST:
                self._alert("auth_failures", count=n)
            if not allowed:
                self._metrics_obj().rate_limit()
                return self._err(429, "too many requests", {"Retry-After": str(retry)})
        return self._err(code, msg)

    def _actor(self, q=None):
        """Who is making this request, for the audit trail."""
        if self.token is None:
            return "operator"
        if self._is_operator(q):
            return "operator"
        if self._is_join():
            return "join"
        r = self._runner_from_bearer()
        if r:
            return "runner:%s" % r["id"]
        return "anonymous"

    def _capacity(self):
        """Cached row counts + on-disk size (OBS-P2-004).

        The DB read happens off the request path (see refresh_health); this only
        serves the snapshot, so /health never blocks on SQLite.
        """
        return health_snapshot()["capacity"]

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        # a declared-but-huge body used to be read in full, so an authenticated
        # client could pin a thread and its memory with one request
        if n > MAX_BODY:
            raise ValueError("body too large (%d > %d)" % (n, MAX_BODY))
        try:
            raw = self.rfile.read(n).decode("utf-8", "replace")
        except socket.timeout:
            # RES-P3-003: the client declared a body it never sent; the socket
            # timeout frees this handler thread instead of pinning it forever.
            raise BadInput("request body not received")
        if not raw.strip():
            return {}
        try:
            obj = json.loads(raw)
        except Exception:
            # API-P2-001: a malformed body must be a 400, not a silent {}
            raise BadJSON()
        if not isinstance(obj, dict):
            raise BadJSON()
        return obj

    # -- auth --------------------------------------------------------------
    def _bearer(self):
        h = self.headers.get("Authorization", "")
        return h[7:] if h.startswith("Bearer ") else None

    def _runner_from_bearer(self):
        b = self._bearer()
        if not b:
            return None
        # PRIV-P2-005: only the digest is stored, so hash what was presented.
        return self.store.one("SELECT * FROM runners WHERE api_key=?", (key_digest(b),))

    def _is_operator(self, q=None):
        if self.token is None:
            return True
        b = self._bearer()
        if b and secrets.compare_digest(b, self.token):
            return True
        qtok = q.get("token", [None])[0] if q else None
        if qtok and secrets.compare_digest(str(qtok), str(self.token)):
            return True
        return False

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
        if self.token is None:
            return True
        b = self._bearer()
        if b and secrets.compare_digest(b, self.token):
            return True
        r = self._runner_from_bearer()
        if r and (rid is None or r["id"] == rid):
            return True
        return False

    def _caller(self):
        """(is_operator, runner_row_or_None) for the current request."""
        if self.token is None:
            return True, None
        b = self._bearer()
        if b and secrets.compare_digest(b, self.token):
            return True, None
        return False, self._runner_from_bearer()

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

    def _key_expired(self, r):
        """ACM-P2-001 / ACM-P2-002: has this runner's key outlived --key-ttl-days?

        Only consulted when a TTL is configured (0 = never expires). A row with no
        recorded issue time (a legacy key) is treated as expired so a TTL fails
        closed; the migration stamps such rows on upgrade.
        """
        ttl = self.key_ttl_days or 0
        if ttl <= 0 or not r:
            return False
        try:
            issued = float(r.get("key_issued") or 0)
        except (TypeError, ValueError):
            issued = 0.0
        return (now() - issued) > ttl * 86400.0

    def _expired_bearer(self):
        """The runner row for this bearer when its key has expired, else None."""
        if not self.key_ttl_days:
            return None
        r = self._runner_from_bearer()
        return r if (r and self._key_expired(r)) else None

    def _expired_deny(self, r):
        """The 401 for an expired runner key (ACM-P2-001 / ACM-P2-002)."""
        return self._deny(401, "api key expired (older than %d day(s)); rotate it "
                          "with POST /api/v1/runners/%s/rotate"
                          % (self.key_ttl_days, r["id"]))

    def _exec_rejected(self, cmd):
        """ADMIN-P2-004 / ADMIN-P2-006: the reason an exec/shell cmd is refused.

        Returns None when the command is acceptable. The denylist always wins; the
        allowlist (when set) requires a match. Both unset keeps allow-all.
        """
        text = cmd if isinstance(cmd, str) else ""
        if self.exec_denylist is not None and self.exec_denylist.search(text):
            return "command matches the exec denylist"
        if self.exec_allowlist is not None and not self.exec_allowlist.search(text):
            return "command does not match the exec allowlist"
        return None

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
                                    "version_source": snap["version_source"],
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
            if path == "/api/v1/stream":
                if not self._is_operator(q):
                    return self._deny(401, "unauthorized")
                return self._stream()
            m = re.match(r"^/api/v1/runners/([^/]+)/commands$", path)
            if m:
                exp = self._expired_bearer()
                if exp:
                    return self._expired_deny(exp)
                if not self._may_act_as(m.group(1)):
                    return self._err(403, "not your runner")
                return self._runner_commands(m.group(1))
            if not self._is_operator(q):
                return self._deny(401, "unauthorized")
            try:
                return self._api_get(path, q)
            except Exception as exc:
                # OBS-P2-003: record the failure with its traceback; the client
                # still gets a short, generic 500 body (never the traceback).
                self._log("error", "api_get failed", route=path,
                          error="%s: %s" % (type(exc).__name__, exc),
                          traceback=traceback.format_exc())
                return self._err(500, "internal error")
        return self._static(path)

    def do_DELETE(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        if not path.startswith("/api/"):
            return self._err(404, "not found")
        # FILE-P3-002: one artifact of a run can be removed by that run's owner
        # (its runner api_key) or the operator. Handled before the operator-only
        # gate below, so a runner bearer is not refused before ownership is read.
        m = re.match(r"^/api/v1/runs/([^/]+)/artifact/([^/]+)$", path)
        if m:
            return self._delete_artifact(m.group(1), m.group(2), q)
        if not self._is_operator(q):
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
            op = self._is_operator(q)
            if not op:
                # SEC-P2-003: registration is throttled per client IP, but a
                # successful operator request is never limited.
                over = self._throttled("register")
                if over:
                    return over
            allowed = op or self._runner_ok() or self._is_join()
        else:
            allowed = self._is_operator(q) or self._runner_ok()
        if not allowed:
            return self._deny(401, "unauthorized")
        try:
            body = self._body()
        except BadJSON:
            return self._err(400, "invalid json")
        except BadInput as exc:
            self._log("warning", "bad request body", route=path, error=str(exc))
            return self._err(400, "invalid request")
        except ValueError as exc:
            return self._err(413, str(exc))
        try:
            return self._api_post(path, q, body)
        except BadInput as exc:
            # API-P3-001 / DATA-P3-001: malformed client input is a 400 with a
            # generic body - never the exception type or message.
            self._log("warning", "invalid request", route=path, error=str(exc))
            return self._err(400, "invalid request")
        except (TypeError, ValueError, KeyError, AttributeError,
                sqlite3.InterfaceError) as exc:
            # A client value of the wrong shape must not surface as a 500.
            self._log("warning", "invalid input", route=path,
                      error="%s: %s" % (type(exc).__name__, exc))
            return self._err(400, "invalid request")
        except Exception as exc:
            # OBS-P2-003: log the traceback server-side; the client still gets a
            # short, generic 500 body without it.
            self._log("error", "api_post failed", route=path,
                      error="%s: %s" % (type(exc).__name__, exc),
                      traceback=traceback.format_exc())
            return self._err(500, "internal error")

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
            # OBS-P2-001 / IR-P2-001: a failing readiness probe is alertable.
            self._alert("readiness", error=db_err)
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
        try:
            expired = S.one("SELECT COUNT(*) c FROM commands WHERE status='expired'")["c"]
        except Exception:
            expired = 0
        # OBS-P2-001: alert webhook counters (all zero while the webhook is off).
        al = getattr(S, "alerter", None)
        al_tot = al.totals() if al is not None else {"fired": 0, "suppressed": 0, "failed": 0}
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
            "# HELP benchd_commands_expired_total Commands dropped after a re-queue was never acked.",
            "# TYPE benchd_commands_expired_total counter",
            "benchd_commands_expired_total %d" % expired,
            "# HELP benchd_sse_subscribers Active SSE subscribers.",
            "# TYPE benchd_sse_subscribers gauge",
            "benchd_sse_subscribers %d" % self.bus.subscriber_count(),
            "# HELP benchd_sse_dropped_events_total SSE events dropped or merged by"
            " bounded-queue / telemetry coalescing.",
            "# TYPE benchd_sse_dropped_events_total counter",
            "benchd_sse_dropped_events_total %d" % self.bus.dropped_count(),
            "# HELP benchd_alerts_fired_total Alert webhook deliveries.",
            "# TYPE benchd_alerts_fired_total counter",
            "benchd_alerts_fired_total %d" % al_tot["fired"],
            "# HELP benchd_alerts_suppressed_total Alerts dropped by the per-kind rate limit.",
            "# TYPE benchd_alerts_suppressed_total counter",
            "benchd_alerts_suppressed_total %d" % al_tot["suppressed"],
            "# HELP benchd_alerts_failed_total Alert webhook POSTs that failed.",
            "# TYPE benchd_alerts_failed_total counter",
            "benchd_alerts_failed_total %d" % al_tot["failed"],
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
                            "version": server_version(),
                            "version_source": server_version_source(),
                            "protocol": PROTOCOL,
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
        # PERF-P2-001: cache the parsed rows; a repeat request with the same
        # telemetry generation reuses them and only re-applies the (cheap)
        # downsampling.
        key = (run_id, _run_gen(run_id), "tele")
        rows = _cache_get(key)
        if rows is None:
            raw = self.store.q("SELECT * FROM telemetry WHERE run_id=? ORDER BY ts", (run_id,))
            rows = [tele_public(r) for r in raw]
            _cache_put(key, rows)
        if len(rows) > maxn:
            step = len(rows) / float(maxn)
            picked, i = [], 0.0
            while int(i) < len(rows):
                picked.append(rows[int(i)])
                i += step
            if picked and picked[-1] is not rows[-1]:
                picked.append(rows[-1])
            rows = picked
        return self._send(200, {"run_id": run_id, "rows": rows})

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
        # PERF-P2-001: served from the per-run cache; only a changed row count
        # (new telemetry) re-parses the rows.
        st = compute_stats_cached(self.store, run_id)
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
        # PERF-P2-001: the CSV bytes are cached by telemetry generation; repeat
        # downloads return the exact same bytes without re-reading or re-formatting.
        key = (run_id, _run_gen(run_id), "csv")
        raw = _cache_get(key)
        if raw is None:
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
            raw = buf.getvalue().encode()
            _cache_put(key, raw)
        return self._send(200, raw=raw, ctype="text/csv",
                          headers={"Content-Disposition":
                                   'attachment; filename="%s-telemetry.csv"' % safe_filename(run_id)})

    def _artifact_download(self, run_id, name):
        a = self.store.one("SELECT * FROM artifacts WHERE run_id=? AND name=?", (run_id, name))
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

    def _delete_artifact(self, run_id, name, q=None):
        """FILE-P3-002: remove one attached artifact (owner or operator only).

        404 when the run or the artifact is absent, 403 when the caller is a
        runner that does not own the run; the deletion is audited.
        """
        S = self.store
        op, r = self._caller()
        if not (op or r):
            return self._deny(401, "unauthorized")
        row = S.one("SELECT runner_id FROM runs WHERE id=?", (run_id,))
        if not row:
            return self._err(404, "no such run")
        if not (op or (r and row["runner_id"] == r["id"])):
            return self._err(403, "not your run")
        a = S.one("SELECT id FROM artifacts WHERE run_id=? AND name=?", (run_id, name))
        if not a:
            return self._err(404, "no such artifact")
        S.ex("DELETE FROM artifacts WHERE run_id=? AND name=?", (run_id, name))
        S.audit(self._actor(q), "artifact_delete", target=name, detail=run_id)
        self.bus.publish({"event": "artifact",
                          "data": {"run_id": run_id, "name": name, "deleted": True}})
        return self._send(200, {"ok": True})

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
        try:
            q = self.bus.subscribe()
        except BusFull:
            # PERF-P2-002: bounded fan-out - the (N+1)th live client is refused.
            return self._err(503, "too many subscribers")
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
                    ev = q.get(timeout=15)
                    self._sse(ev.get("event", "message"), ev.get("data", {}))
                except Exception:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except Exception:
            # OBS-P2-003: a broken SSE client is logged, not swallowed.
            self._log("error", "sse stream failed", traceback=traceback.format_exc())
        finally:
            self.bus.unsubscribe(q)

    def _sse(self, event, data):
        self.wfile.write(("event: %s\ndata: %s\n\n" % (event, json.dumps(data))).encode())
        self.wfile.flush()

    # -- POST api ----------------------------------------------------------
    def _api_post(self, path, q, b):
        S = self.store
        # a writer may only touch its own run (404 unknown, 403 someone else's)
        mrun = re.match(r"^/api/v1/runs/([^/]+)/(?:phase|telemetry|event|log|artifact|finish)$",
                        path)
        if mrun and not self._may_write_run(mrun.group(1)):
            exists = S.one("SELECT id FROM runs WHERE id=?", (mrun.group(1),))
            return self._err(404 if not exists else 403,
                             "no such run" if not exists else "not your run")
        if path == "/api/v1/register":
            return self._register(b)
        m = re.match(r"^/api/v1/runners/([^/]+)/heartbeat$", path)
        if m:
            rid = m.group(1)
            exp = self._expired_bearer()
            if exp and exp["id"] == rid:
                return self._expired_deny(exp)
            if not self._may_act_as(rid):
                return self._err(403, "not your runner")
            if not S.one("SELECT id FROM runners WHERE id=?", (rid,)):
                return self._err(404, "no such runner")
            fields, args = ["last_seen=?"], [now()]
            for k in ("status", "current_run", "version", "git"):
                if k in b:
                    fields.append("%s=?" % k)
                    args.append(field_str(b, k))
            if "capabilities" in b:
                fields.append("capabilities=?")
                args.append(json.dumps(field_dict(b, "capabilities")))
            args.append(rid)
            S.ex("UPDATE runners SET %s WHERE id=?" % ",".join(fields), args)
            r = runner_public(S.one("SELECT * FROM runners WHERE id=?", (rid,)))
            if r:
                self.bus.publish({"event": "runner", "data": r})
            pending = S.one("SELECT COUNT(*) c FROM commands WHERE runner_id=? AND status='queued'",
                            (rid,))["c"]
            return self._send(200, {"ok": True, "commands": pending, "server_time": now()})
        m = re.match(r"^/api/v1/runners/([^/]+)/rotate$", path)
        if m:
            return self._rotate_key(m.group(1), q)
        m = re.match(r"^/api/v1/runners/([^/]+)/commands$", path)
        if m:
            if not self._is_operator(q):
                return self._err(403, "operator only")
            return self._issue_command(m.group(1), b)
        m = re.match(r"^/api/v1/runners/([^/]+)/commands/([^/]+)/ack$", path)
        if m:
            rid, cid = m.group(1), m.group(2)
            exp = self._expired_bearer()
            if exp and exp["id"] == rid:
                return self._expired_deny(exp)
            if not self._may_act_as(rid):
                return self._err(403, "not your command")
            cmd = S.one("SELECT runner_id FROM commands WHERE id=?", (cid,))
            if not cmd or cmd["runner_id"] != rid:
                return self._err(404, "no such command")
            status = field_str(b, "status", "done") or "done"
            result = field_dict(b, "result")
            S.ex("UPDATE commands SET status=?, acked=?, result=? WHERE id=?",
                 (status, now(), json.dumps(result), cid))
            self.bus.publish({"event": "command", "data": {"id": cid, "runner_id": rid,
                                                           "status": status,
                                                           "result": result}})
            return self._send(200, {"ok": True})
        if path == "/api/v1/schedules":
            if not self._is_operator(q):
                return self._err(403, "operator only")
            return self._create_schedule(b)
        if path == "/api/v1/runs":
            op, r = self._caller()
            rid = field_str(b, "runner_id")
            if not op:
                if not r:
                    return self._deny(401, "unauthorized")
                if rid and rid != r["id"]:
                    return self._err(403, "not your runner")
                rid = r["id"]
            run_id = b.get("id") or str(uuid.uuid4())
            if not safe_id(run_id):
                return self._err(400, "bad run id")
            profile = field_dict(b, "profile")
            system = field_dict(b, "system")
            S.ex("INSERT INTO runs(id,runner_id,host,label,profile,started,status,platform,gpu,"
                 "driver,torch,system,preset) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (run_id, rid, field_str(b, "host"), field_str(b, "label"), json.dumps(profile),
                  b.get("started") or now(), "running", field_str(b, "platform"),
                  field_str(b, "gpu"), field_str(b, "driver"), field_str(b, "torch"),
                  json.dumps(system), field_str(b, "preset") or profile.get("preset")))
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
            grp = field_str(b, "group")
            label = field_str(b, "label")
            status = field_str(b, "status", "running") or "running"
            detail = field_str(b, "detail")
            # DATA-P2-004: (run_id, seq) is unique, so a replayed phase replaces
            # its row atomically instead of racing a SELECT-then-INSERT/UPDATE.
            # t0 is carried over from the existing row when the replay omits it.
            S.ex("INSERT OR REPLACE INTO phases(run_id,seq,grp,label,status,t0,t1,detail) "
                 "VALUES(?,?,?,?,?,"
                 "COALESCE(?, (SELECT t0 FROM phases WHERE run_id=? AND seq=?), ?),?,?)",
                 (run_id, seq, grp, label, status,
                  b.get("t0"), run_id, seq, now(), b.get("t1"), detail))
            self.bus.publish({"event": "phase", "data": {
                "run_id": run_id, "seq": seq, "group": grp, "label": label,
                "status": status, "detail": detail}})
            return self._send(200, {"ok": True})
        m = re.match(r"^/api/v1/runs/([^/]+)/telemetry$", path)
        if m:
            run_id = m.group(1)
            samples = field_list(b, "samples")
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
                    if isinstance(s.get("ts"), (int, float)):
                        ts = float(s["ts"])
                    named.append(dict(s))
                elif isinstance(s, (list, tuple)):
                    d = dict(zip(SAMPLE_FIELDS,
                                 (list(s) + [None] * len(SAMPLE_FIELDS))[:len(SAMPLE_FIELDS)]))
                    sanitise_sample(d)
                    core = [d.get(f) for f in SAMPLE_FIELDS]
                    extra = {}
                    named.append(dict(d))
                else:
                    raise BadInput("each sample must be an object or list")
                rows.append([run_id, ts or now()] + core + [json.dumps(extra) if extra else None])
            with S.lock:
                S.conn.executemany(
                    "INSERT INTO telemetry(run_id,ts,elapsed,temp_c,fan_pct,power_w,gpu_util,"
                    "gpu_fan_rpm,cpu_util,ram_used,sm_mhz,extra) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                S.conn.commit()
            # PERF-P2-001: new telemetry invalidates the run's cached stats/CSV/rows.
            bump_run_cache(run_id)
            if named:
                # publish the WHOLE batch: a runner posts telemetry in chunks, and
                # sending only the last sample made the live web view undersample
                # (one point per chunk instead of per second)
                self.bus.publish({"event": "telemetry",
                                  "data": {"run_id": run_id, "sample": named[-1],
                                           "samples": named[-1000:], "n": len(rows)}})
            return self._send(200, {"ok": True, "accepted": len(rows)})
        m = re.match(r"^/api/v1/runs/([^/]+)/event$", path)
        if m:
            level = field_str(b, "level", "info") or "info"
            message = field_str(b, "message", "") or ""
            S.ex("INSERT INTO events(run_id,ts,level,message) VALUES(?,?,?,?)",
                 (m.group(1), b.get("ts") or now(), level, message))
            self.bus.publish({"event": "event", "data": {"run_id": m.group(1),
                                                         "level": level,
                                                         "message": message}})
            return self._send(200, {"ok": True})
        m = re.match(r"^/api/v1/runs/([^/]+)/log$", path)
        if m:
            run_id = m.group(1)
            lines = field_list(b, "lines") or ([b["line"]] if b.get("line") else [])
            lines = [str(x)[:4000] for x in lines]
            stream = field_str(b, "stream", "out") or "out"
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
            # FILE-P2-002: a name is now required and must be a bare filename, and
            # the upload is bounded per artifact and per run.
            name = field_str(b, "name")
            if bad_artifact_name(name):
                return self._err(400, "invalid artifact name")
            kind = field_str(b, "kind", "json") or "json"
            content = field_str(b, "content", "") or ""
            claimed = b.get("sha256")
            if claimed is not None and not isinstance(claimed, str):
                raise BadInput("'sha256' must be a string")
            existing = S.one("SELECT COUNT(*) c FROM artifacts WHERE run_id=?",
                             (run_id,))["c"]
            if existing >= self.artifact_quota:
                return self._err(413, "artifact quota reached for this run "
                                 "(%d)" % self.artifact_quota)
            try:
                raw = base64.b64decode(content, validate=True) if content else b""
            except Exception:
                return self._err(400, "invalid base64 content")
            if len(raw) > self.artifact_max_bytes:
                return self._err(413, "artifact too large (%d > %d bytes)"
                                 % (len(raw), self.artifact_max_bytes))
            # FILE-P3-001: the client's sha256 was stored unverified. Recompute it
            # from the bytes we actually received, refuse a mismatch, and persist
            # (so the download serves) the value we computed.
            digest = hashlib.sha256(raw).hexdigest()
            if claimed and not secrets.compare_digest(claimed.lower(), digest):
                return self._err(400, "sha256 mismatch")
            S.ex("INSERT INTO artifacts(run_id,name,kind,size,sha256,content) VALUES(?,?,?,?,?,?)",
                 (run_id, name, kind, len(raw), digest, content))
            return self._send(200, {"ok": True, "sha256": digest, "size": len(raw)})
        m = re.match(r"^/api/v1/runs/([^/]+)/finish$", path)
        if m:
            run_id = m.group(1)
            # DATA-P3-002: a late finish must not overwrite a run the reaper (or a
            # previous finish) already closed - only a still-running run may close.
            cur = S.one("SELECT status FROM runs WHERE id=?", (run_id,))
            if not cur:
                return self._err(404, "no such run")
            if cur["status"] != "running":
                self._log("warning", "finish ignored: run not running",
                          run_id=run_id, status=cur["status"])
                return self._err(409, "run is not running")
            status = field_str(b, "status", "done") or "done"
            summary = field_dict(b, "summary")
            system = field_dict(b, "system")
            S.ex("UPDATE runs SET status=?, ended=?, exit_code=?, summary=?, platform=COALESCE(?,platform),"
                 " gpu=COALESCE(?,gpu), driver=COALESCE(?,driver), torch=COALESCE(?,torch),"
                 " system=CASE WHEN ?='{}' THEN system ELSE ? END WHERE id=?",
                 (status, b.get("ended") or now(), b.get("exit_code"),
                  json.dumps(summary), field_str(b, "platform"), field_str(b, "gpu"),
                  field_str(b, "driver"), field_str(b, "torch"), json.dumps(system),
                  json.dumps(system), run_id))
            run = S.one("SELECT runner_id FROM runs WHERE id=?", (run_id,))
            if run and run["runner_id"]:
                S.ex("UPDATE runners SET status='idle', current_run=NULL WHERE id=?",
                     (run["runner_id"],))
            run = run_public(S.one("SELECT * FROM runs WHERE id=?", (run_id,)))
            if run:
                self.bus.publish({"event": "run", "data": run})
            # OBS-P2-001 / IR-P2-001: a run finishing failed (or with a failing
            # verdict) raises an out-of-band alert.
            if run_failed(status, summary):
                self._alert("run_failed", run_id=run_id, status=status,
                            exit_code=b.get("exit_code"))
            return self._send(200, {"ok": True})
        return self._err(404, "not found")

    def _register(self, b):
        S = self.store
        rid = field_str(b, "runner_id")
        if rid and not safe_id(rid):
            rid = None
        # PRIV-P2-005: `key` is the plaintext the caller presented (or None);
        # what is stored below is always key_digest(key).
        key = field_str(b, "api_key")
        kind = field_str(b, "kind", "agent") or "agent"
        mid = (field_str(b, "machine_id") or "").strip() or None
        inst = (field_str(b, "instance") or "").strip()
        existing = None
        rotate = False
        if rid:
            existing = S.one("SELECT * FROM runners WHERE id=?", (rid,))
            # ownership may be proved by the body's api_key or by the bearer that
            # authenticated this request; either must match THIS row's key
            proof = key or self._bearer()
            if existing and not (proof and secrets.compare_digest(key_digest(proof), str(existing["api_key"]))) \
                    and not self._is_operator():
                # An explicit runner_id is only honoured by its own key or an
                # operator. Any valid runner bearer passes the POST gate, so
                # accepting it blind let one runner take over another's row - and
                # the response then handed it that runner's api_key. Adoption by
                # machine_id below is unaffected (that is how a live stick with no
                # persisted config reclaims its own row).
                existing = None
                rid = None
            elif existing and key and existing["api_key"] != key_digest(key):
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
                if proof and secrets.compare_digest(key_digest(proof), str(cand["api_key"])):
                    existing = cand
                elif self._is_operator():
                    existing = cand
                elif not online(cand["last_seen"]):
                    existing = dict(cand)
                    rotate = True
        if existing:
            rid = existing["id"]
            proof = key or self._bearer()
            if proof and secrets.compare_digest(key_digest(proof), str(existing["api_key"])):
                key = proof                       # the caller's own key, handed back
            elif rotate:
                key = secrets.token_urlsafe(32)   # reclaim an offline row with a new key
            elif S.key_plain.get(rid):
                # An operator re-registering a row this process just created: the
                # plaintext is still in memory, so the same key is returned and
                # the caller is not surprised by a rotation.
                key = S.key_plain[rid]
            else:
                key = secrets.token_urlsafe(32)
        else:
            rid = rid or str(uuid.uuid4())
            key = secrets.token_urlsafe(32)
        S.key_plain[rid] = key
        S.ex("INSERT INTO runners(id,name,os,kind,version,capabilities,first_seen,last_seen,"
             "status,current_run,addr,git,api_key,machine_id,ephemeral,instance,key_issued) "
             "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
             "ON CONFLICT(id) DO UPDATE SET name=excluded.name, os=excluded.os, kind=excluded.kind, "
             "version=excluded.version, capabilities=excluded.capabilities, last_seen=excluded.last_seen, "
             "status=excluded.status, addr=excluded.addr, git=excluded.git, "
             "machine_id=excluded.machine_id, ephemeral=excluded.ephemeral, instance=excluded.instance, "
             "key_issued=excluded.key_issued",
             (rid, field_str(b, "name"), field_str(b, "os"), kind, field_str(b, "version"),
              json.dumps(field_dict(b, "capabilities")), now(), now(),
              field_str(b, "status", "idle") or "idle", field_str(b, "current_run"),
              self.client_address[0], field_str(b, "git"), key_digest(key), mid,
              1 if b.get("ephemeral") else 0, inst, now()))
        r = runner_public(S.one("SELECT * FROM runners WHERE id=?", (rid,)))
        self.bus.publish({"event": "runner", "data": r})
        S.audit(self._actor(), "register", target=rid,
                detail="%s/%s" % (kind, inst) if inst else kind)
        return self._send(200, {"runner_id": rid, "api_key": key, "poll_interval": 2.0,
                                "telemetry_interval": 1.0, "server_time": now()})

    def _issue_command(self, rid, b):
        S = self.store
        kind = b.get("kind")
        if not isinstance(kind, str) or kind not in (
                "start", "stop", "update", "fan_curve", "power_limit", "restart",
                "shutdown", "reboot", "exec", "shell",
                "update_toolkit", "iso_fetch", "iso_write", "iso_write_slot"):
            return self._err(400, "unknown command kind")
        payload = field_dict(b, "payload")
        # ADMIN-P2-002: a destructive command must carry an explicit confirmation,
        # so a script or API client cannot power-cycle a machine or overwrite a disk
        # in one unconfirmed request. The browser's confirm() dialog is unchanged.
        if kind in DESTRUCTIVE_COMMANDS and payload.get("confirm") is not True:
            S.audit(self._actor(), "command_denied", target=rid,
                    detail="%s: payload.confirm!=true" % kind)
            return self._err(400, "destructive command '%s' requires "
                             "payload.confirm=true" % kind)
        # ADMIN-P2-004 / ADMIN-P2-006: an optional operator allow/deny list can
        # constrain fleet-wide exec/shell. A rejection is a 403 and is audited.
        if kind in EXEC_COMMANDS:
            reason = self._exec_rejected(payload.get("cmd"))
            if reason:
                S.audit(self._actor(), "command_denied", target=rid,
                        detail="%s: %s" % (kind, reason))
                return self._err(403, reason)
        cid = str(uuid.uuid4())
        # RES-P3-001: attempts counts how often a fetched-but-unacked command was
        # re-queued; 0 here, bumped by the stale-command reaper.
        S.ex("INSERT INTO commands(id,runner_id,kind,payload,status,created,attempts) "
             "VALUES(?,?,?,?,?,?,?)",
             (cid, rid, kind, json.dumps(payload), "queued", now(), 0))
        self.bus.publish({"event": "command", "data": {"id": cid, "runner_id": rid,
                                                       "kind": kind, "status": "queued"}})
        S.audit(self._actor(), "command", target=rid, detail=kind)
        return self._send(200, {"id": cid, "status": "queued"})

    def _rotate_key(self, rid, q=None):
        """ACM-P2-001 / ACM-P2-002: mint a new api_key for one runner.

        The operator or the runner itself may rotate. Only the digest is stored,
        so the old key stops authenticating the instant this returns; the new
        plaintext is handed back once (the caller must persist it). Audited.
        """
        S = self.store
        if not self._may_act_as(rid):
            return self._err(403, "not your runner")
        if not S.one("SELECT id FROM runners WHERE id=?", (rid,)):
            return self._err(404, "no such runner")
        key = secrets.token_urlsafe(32)
        S.ex("UPDATE runners SET api_key=?, key_issued=? WHERE id=?",
             (key_digest(key), now(), rid))
        S.key_plain[rid] = key
        S.audit(self._actor(q), "key_rotate", target=rid,
                detail="ttl_days=%d" % (self.key_ttl_days or 0))
        self.bus.publish({"event": "runner", "data": runner_public(
            S.one("SELECT * FROM runners WHERE id=?", (rid,)))})
        return self._send(200, {"runner_id": rid, "api_key": key,
                                "key_ttl_days": self.key_ttl_days or 0})

    def _create_schedule(self, b):
        S = self.store
        rid = field_str(b, "runner_id")
        if not rid or not S.one("SELECT id FROM runners WHERE id=?", (rid,)):
            return self._err(400, "runner_id required")
        kind = field_str(b, "kind", "every") or "every"
        every_s = int_arg({"e": [b.get("every_s") or 0]}, "e", 0, hi=30 * 86400)
        at_time = field_str(b, "at_time") or "03:00"
        label = field_str(b, "label") or "scheduled run"
        sid = str(uuid.uuid4())
        nxt = (next_daily(at_time) if kind == "daily"
               else now() + max(60, every_s))
        S.ex("INSERT INTO schedules(id,runner_id,label,profile,kind,every_s,at_time,next_run,"
             "enabled,last_run,created,last_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
             (sid, rid, label, json.dumps(field_dict(b, "profile")),
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
        # PERF-P3-001: static assets get validators and a short cache instead of
        # no-store; the API keeps no-store (see _send's default).
        try:
            st = os.stat(full)
            etag = '"%x-%x"' % (int(st.st_mtime), st.st_size)
            lastmod = self.date_time_string(int(st.st_mtime))
        except OSError:
            etag, lastmod = "", ""
        validators = {}
        if etag:
            validators["ETag"] = etag
        if lastmod:
            validators["Last-Modified"] = lastmod
        if etag and self.headers.get("If-None-Match") == etag:
            return self._send(304, raw=b"", ctype=ctype, cache="public, max-age=60",
                              headers=validators)
        with open(full, "rb") as fb:
            return self._send(200, raw=fb.read(), ctype=ctype, cache="public, max-age=60",
                              headers=validators)


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
        store.ex("INSERT INTO commands(id,runner_id,kind,payload,status,created,attempts) "
                 "VALUES(?,?,?,?,?,?,?)",
                 (cmd, s["runner_id"], "start", s["profile"] or "{}", "queued", now(), 0))
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
RETENTION_DAYS = float(os.environ.get("BENCHD_RETENTION_DAYS") or 30)
RETENTION_EVERY_S = 3600.0        # a full sweep does not need to run every tick
_RETENTION_LAST = [0.0]
# FILE-P3-002: artifacts are pruned by the same sweep/window but from their own
# throttle slot, so retain_old_data's per-sample behaviour (and its test) is
# untouched.
_ARTIFACT_RETENTION_LAST = [0.0]


def retain_old_data(store, force=False):
    """Delete telemetry/logs/events of finished runs older than RETENTION_DAYS.

    Running runs are never touched, and neither are the run rows themselves: each
    one keeps its stats (min/avg/max/p95/p99 and the derived metrics), materialised
    here just before its samples go, so only the rows that grow with run length and
    sample rate are dropped.
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
            store.ex("UPDATE runs SET stats=? WHERE id=?", (json.dumps(st), r["id"]))
        for table in ("telemetry", "logs", "events"):
            # the table names are literals here, never caller input
            cur = store.ex("DELETE FROM %s WHERE run_id=?" % table, (r["id"],))
            if cur.rowcount and cur.rowcount > 0:
                gone += cur.rowcount
    if gone:
        print("[benchd] retention: removed %d row(s) older than %.0f days"
              % (gone, days), flush=True)
    return gone


def prune_old_artifacts(store, force=False):
    """Delete artifacts of finished runs older than RETENTION_DAYS.

    FILE-P3-002: attached artifacts (a report, a manifest, a telemetry CSV) are
    bulky and were never pruned, so they grew the database without bound. They go
    under the same window and sweep as telemetry/logs/events; the run row, its
    phases and its summary survive (History, Trends, Compare and Fleet read
    those). Running runs are never touched, and ``BENCHD_RETENTION_DAYS=0`` keeps
    everything.
    """
    days = RETENTION_DAYS
    if days <= 0:
        return 0
    t = now()
    if not force and t - _ARTIFACT_RETENTION_LAST[0] < RETENTION_EVERY_S:
        return 0
    _ARTIFACT_RETENTION_LAST[0] = t
    cutoff = t - days * 86400
    old = store.q("SELECT id FROM runs WHERE status != 'running' AND "
                  "COALESCE(ended, started) < ?", (cutoff,))
    gone = 0
    for r in old:
        cur = store.ex("DELETE FROM artifacts WHERE run_id=?", (r["id"],))
        if cur.rowcount and cur.rowcount > 0:
            gone += cur.rowcount
    if gone:
        print("[benchd] retention: removed %d artifact row(s) older than %.0f days"
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
            continue  # runner still heartbeating
        store.ex("UPDATE runs SET status='failed', ended=? WHERE id=?", (t, r["id"]))
        if r["runner_id"]:
            store.ex("UPDATE runners SET status='idle', current_run=NULL WHERE id=?",
                     (r["runner_id"],))
        bus.publish({"event": "run",
                     "data": run_public(store.one("SELECT * FROM runs WHERE id=?", (r["id"],)))})
        # OBS-P2-001 / IR-P2-001: a run the reaper failed is still a failed run.
        a = getattr(store, "alerter", None)
        if a is not None:
            a.fire("run_failed", run_id=r["id"], status="failed", reason="reaped")


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
    """Re-queue a fetched-but-unacked command once, then expire it.

    RES-P3-001: the queue is at-most-once - `_runner_commands` marks a command
    'sent' as it hands it out, so a runner that died (or dropped it) leaves it
    invisible. A first timeout puts it back in 'queued' (bumping `attempts`) so
    the next poll delivers it again; a second timeout marks it 'expired'.

    A legacy row with no `attempts` value (inserted before this column existed)
    keeps the old single-shot behaviour and is marked 'failed'.
    """
    t = now()
    for c in store.q("SELECT id, runner_id, kind, sent, payload, attempts "
                     "FROM commands WHERE status='sent'"):
        if not c["sent"] or (t - c["sent"]) <= ack_timeout(c["kind"], c["payload"]):
            continue
        attempts = c["attempts"]
        if attempts is None:
            status = "failed"
            store.ex("UPDATE commands SET status='failed', result=? WHERE id=?",
                     (json.dumps({"error": "no ack from the runner (dropped?)"}), c["id"]))
        elif attempts < 1:
            status = "queued"
            store.ex("UPDATE commands SET status='queued', attempts=?, sent=NULL, "
                     "result=NULL WHERE id=?", (attempts + 1, c["id"]))
        else:
            status = "expired"
            store.ex("UPDATE commands SET status='expired', result=? WHERE id=?",
                     (json.dumps({"error": "no ack after re-queue (expired)"}), c["id"]))
        bus.publish({"event": "command", "data": {"id": c["id"], "runner_id": c["runner_id"],
                                                  "kind": c["kind"], "status": status}})


def scheduler_loop(store, bus, stop):
    """Fire due schedules by queueing a start command to the target runner."""
    while not stop.is_set():
        try:
            scheduler_tick(store, bus)
            reap_stale_runs(store, bus)
            reap_ephemeral_runners(store, bus)
            reap_stale_commands(store, bus)
            retain_old_data(store)
            prune_old_artifacts(store)
        except Exception as e:
            print("[benchd] scheduler error: %s" % e, flush=True)
        stop.wait(10.0)


# -------------------------------------------------------------------- config --
def find_conf_value(key):
    for cand in (os.path.join(HERE, "benchd.conf"), os.path.expanduser("~/.config/benchd.conf"),
                 "/etc/benchd.conf"):
        if os.path.isfile(cand):
            for ln in open(cand):
                ln = ln.strip()
                if ln.startswith(key + "="):
                    return ln.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def find_token(cli_token):
    return (cli_token or os.environ.get("BENCHD_TOKEN")
            or find_conf_value("BENCHD_TOKEN"))


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


def find_alert_webhook(cli_url):
    """The alert webhook URL; None keeps alerting disabled (the default).

    OBS-P2-001 / IR-P2-001 / NOTIF-P2-001: an optional out-of-band alert target.
    Nothing is sent unless this resolves to a URL.
    """
    return (cli_url or os.environ.get("BENCHD_ALERT_WEBHOOK")
            or find_conf_value("BENCHD_ALERT_WEBHOOK"))


def find_alert_db_bytes(cli_val):
    """The database-size alert threshold in bytes; 0 (the default) disables it."""
    raw = (cli_val if cli_val is not None
           else os.environ.get("BENCHD_ALERT_DB_BYTES")
           or find_conf_value("BENCHD_ALERT_DB_BYTES"))
    try:
        return max(0, int(str(raw).strip()))
    except (TypeError, ValueError):
        return 0


def find_exec_regex(cli_val, key):
    """ADMIN-P2-004/006: the raw --exec-allowlist / --exec-denylist pattern.

    CLI flag, then $<key>, then the config file; None/'' means unset (allow all
    for the allowlist, no denial for the denylist).
    """
    return (cli_val if cli_val is not None
            else os.environ.get(key) or find_conf_value(key))


def find_key_ttl_days(cli_val):
    """ACM-P2-001/002: per-runner key lifetime in days; 0 (the default) = never."""
    raw = (cli_val if cli_val is not None
           else os.environ.get("BENCHD_KEY_TTL_DAYS")
           or find_conf_value("BENCHD_KEY_TTL_DAYS"))
    try:
        return max(0, int(str(raw).strip()))
    except (TypeError, ValueError):
        return 0


def find_artifact_max_bytes(cli_val):
    """FILE-P2-002: per-artifact size cap in bytes (default 25 MiB)."""
    raw = (cli_val if cli_val is not None
           else os.environ.get("BENCHD_ARTIFACT_MAX_BYTES")
           or find_conf_value("BENCHD_ARTIFACT_MAX_BYTES"))
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_ARTIFACT_MAX_BYTES
    return v if v > 0 else DEFAULT_ARTIFACT_MAX_BYTES


def find_artifact_quota(cli_val):
    """FILE-P2-002: max artifacts per run (default 200)."""
    raw = (cli_val if cli_val is not None
           else os.environ.get("BENCHD_ARTIFACT_QUOTA")
           or find_conf_value("BENCHD_ARTIFACT_QUOTA"))
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_ARTIFACT_QUOTA
    return v if v > 0 else DEFAULT_ARTIFACT_QUOTA


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
    ap.add_argument("--alert-webhook", default=None,
                    help="optional URL: POST a JSON alert on a failed run, a failing "
                         "/ready, a burst of auth failures, or a capacity threshold; "
                         "default is none (alerting disabled)")
    ap.add_argument("--alert-db-bytes", default=None,
                    help="database-size alert threshold in bytes (with "
                         "--alert-webhook); default 0 disables the capacity alert")
    ap.add_argument("--exec-allowlist", default=None,
                    help="ADMIN-P2-004/006: a regex; when set, only exec/shell "
                         "commands matching it are accepted (default: allow all)")
    ap.add_argument("--exec-denylist", default=None,
                    help="ADMIN-P2-004/006: a regex; any exec/shell command matching "
                         "it is refused with 403 (default: none)")
    ap.add_argument("--key-ttl-days", default=None,
                    help="ACM-P2-001/002: reject a runner api_key older than N days "
                         "with 401; 0 (default) never expires")
    ap.add_argument("--artifact-max-bytes", default=None,
                    help="FILE-P2-002: per-artifact size cap in bytes (default 25 MiB)")
    ap.add_argument("--artifact-quota", default=None,
                    help="FILE-P2-002: maximum artifacts per run (default 200)")
    ap.add_argument("--web", default=WEB_DIR)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--allow-anonymous", action="store_true",
                    help="run with no BENCHD_TOKEN (lab/dev only: anyone on the "
                         "network can register and control runners)")
    a = ap.parse_args()


    token = find_token(a.token)
    join_token = find_join_token(a.join_token)
    cors_origin = find_cors_origin(a.cors_origin)
    if token is None and not (a.allow_anonymous
                              or os.environ.get("BENCHD_ALLOW_ANONYMOUS") == "1"):
        print("[benchd] refusing to start: no BENCHD_TOKEN set. Set one (--token, "
              "$BENCHD_TOKEN, server/benchd.conf) or pass --allow-anonymous for a "
              "local/dev instance.", flush=True)
        return 2
    try:
        store = Store(a.db)
    except SchemaVersionError as exc:
        # DATA-P2-003 / DR-P2-002 / EVOL-P2-003: fail closed, do not start.
        print("[benchd] refusing to start: %s" % exc, flush=True)
        return 3
    Handler.store, Handler.bus, Handler.token, Handler.web_dir = store, Bus(), token, a.web
    Handler.join_token = join_token
    Handler.cors_origin = cors_origin
    # ADMIN-P2-004 / ADMIN-P2-006: optional exec/shell allow/deny lists. An invalid
    # regex is a startup error (fail closed), never a silently ignored restriction.
    allow_raw = find_exec_regex(a.exec_allowlist, "BENCHD_EXEC_ALLOWLIST")
    deny_raw = find_exec_regex(a.exec_denylist, "BENCHD_EXEC_DENYLIST")
    try:
        Handler.exec_allowlist = re.compile(allow_raw) if allow_raw else None
        Handler.exec_denylist = re.compile(deny_raw) if deny_raw else None
    except re.error as exc:
        print("[benchd] refusing to start: invalid exec allow/deny regex: %s" % exc,
              flush=True)
        return 2
    # ACM-P2-001 / ACM-P2-002: per-runner key lifetime.
    Handler.key_ttl_days = find_key_ttl_days(a.key_ttl_days)
    # FILE-P2-002: artifact size cap and per-run quota.
    Handler.artifact_max_bytes = find_artifact_max_bytes(a.artifact_max_bytes)
    Handler.artifact_quota = find_artifact_quota(a.artifact_quota)
    # OBS-P2-001 / IR-P2-001 / NOTIF-P2-001: install the optional alert webhook.
    alert_url = find_alert_webhook(a.alert_webhook)
    store.alerter = Alerter(alert_url, db_bytes_limit=find_alert_db_bytes(a.alert_db_bytes))
    httpd = ThreadingHTTPServer((a.host, a.port), Handler)
    httpd.quiet = a.quiet
    httpd.daemon_threads = True

    stop = threading.Event()
    threading.Thread(target=scheduler_loop, args=(store, Handler.bus, stop), daemon=True).start()

    # OBS-P3-003 / CI-P3-001: record which source the version resolved from, so a
    # copy-deployed server (no .git) can tie a release tag to a build.
    print("[benchd] version %s (source: %s)  commit %s"
          % (server_version(), server_version_source(), server_git() or "unknown"),
          flush=True)
    print("[benchd] listening on http://%s:%d  db=%s  auth=%s"
          % (a.host, a.port, a.db, "token+login" if token else "OPEN (no token)"), flush=True)
    if join_token:
        print("[benchd] join token enabled: POST /api/v1/register accepts it "
              "(registration only)", flush=True)
    if alert_url:
        print("[benchd] alert webhook enabled (db_bytes limit %d)"
              % store.alerter.db_bytes_limit, flush=True)
    if Handler.exec_allowlist is not None or Handler.exec_denylist is not None:
        print("[benchd] exec gate: allowlist=%s denylist=%s"
              % (allow_raw or "(none)", deny_raw or "(none)"), flush=True)
    if Handler.key_ttl_days:
        print("[benchd] runner key TTL: %d day(s)" % Handler.key_ttl_days, flush=True)
    print("[benchd] artifact limits: %d bytes/artifact, %d/run"
          % (Handler.artifact_max_bytes, Handler.artifact_quota), flush=True)
    if not token:
        print("[benchd] WARNING: no BENCHD_TOKEN set - anyone can register/control", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
