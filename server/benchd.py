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
  operators   the shared join token, as a bearer (API/CLI) or ?token= (web UI/SSE)
  runners     per-runner API keys, issued at registration and used as a bearer
              thereafter; the join token also registers a *new* runner

Storage is SQLite (stdlib); nothing outside the Python standard library is
required, so it runs on the Proxmox lab box and on the live USB alike.

    python3 server/benchd.py --host 0.0.0.0 --port 8722

Config (first wins): --token flag, $BENCHD_TOKEN, server/benchd.conf,
~/.config/benchd.conf, /etc/benchd.conf   (key=value).
  BENCHD_TOKEN=...              join/registration token + operator API bearer
"""
import argparse
import base64
import csv
import io
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")
DEFAULT_DB = os.path.join(HERE, "data", "benchd.sqlite")

SAMPLE_FIELDS = ["elapsed", "temp_c", "fan_pct", "power_w", "gpu_util",
                 "gpu_fan_rpm", "cpu_util", "ram_used", "sm_mhz", "mem_used"]

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
    ("gpu_soak_telemetry.temp_c.1", "GPU soak peak temp (C)"),
    ("fullsoak.result.verdict.clean", "Full-system soak clean (0/1)"),
]

ONLINE_WINDOW = 15.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS runners (
  id TEXT PRIMARY KEY, name TEXT, os TEXT, kind TEXT, version TEXT,
  capabilities TEXT, first_seen REAL, last_seen REAL, status TEXT,
  current_run TEXT, addr TEXT, git TEXT, api_key TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, runner_id TEXT, host TEXT, label TEXT, profile TEXT,
  started REAL, ended REAL, status TEXT, exit_code INTEGER,
  platform TEXT, gpu TEXT, driver TEXT, torch TEXT, summary TEXT, system TEXT,
  preset TEXT
);
CREATE TABLE IF NOT EXISTS phases (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, seq INTEGER, grp TEXT,
  label TEXT, status TEXT, t0 REAL, t1 REAL, detail TEXT
);
CREATE TABLE IF NOT EXISTS telemetry (
  run_id TEXT, ts REAL, elapsed REAL, temp_c REAL, fan_pct REAL, power_w REAL,
  gpu_util REAL, gpu_fan_rpm REAL, cpu_util REAL, ram_used REAL,
  sm_mhz REAL, mem_used REAL, extra TEXT
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
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started);
CREATE INDEX IF NOT EXISTS idx_runs_runner ON runs(runner_id);
CREATE INDEX IF NOT EXISTS idx_phases_run ON phases(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_tele_run ON telemetry(run_id, ts);
CREATE INDEX IF NOT EXISTS idx_cmd_runner ON commands(runner_id, status);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, ts);
CREATE INDEX IF NOT EXISTS idx_logs_run ON logs(run_id, id);
CREATE INDEX IF NOT EXISTS idx_sched_next ON schedules(enabled, next_run);
"""


# --------------------------------------------------------------------- store --
class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.executescript("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")
            self.conn.executescript(SCHEMA)
            self._migrate()
            self.conn.commit()

    def _migrate(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(runners)")}
        if "api_key" not in cols:
            self.conn.execute("ALTER TABLE runners ADD COLUMN api_key TEXT")
        tcols = {r[1] for r in self.conn.execute("PRAGMA table_info(telemetry)")}
        if "extra" not in tcols:
            self.conn.execute("ALTER TABLE telemetry ADD COLUMN extra TEXT")
        rcols = {r[1] for r in self.conn.execute("PRAGMA table_info(runs)")}
        if "system" not in rcols:
            self.conn.execute("ALTER TABLE runs ADD COLUMN system TEXT")
        if "preset" not in rcols:
            self.conn.execute("ALTER TABLE runs ADD COLUMN preset TEXT")

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


# ----------------------------------------------------------------------- bus --
class Bus:
    def __init__(self):
        self.lock = threading.Lock()
        self.subs = set()

    def subscribe(self):
        import queue
        q = queue.Queue(maxsize=1000)
        with self.lock:
            self.subs.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subs.discard(q)

    def publish(self, ev):
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(ev)
            except Exception:
                pass


# ------------------------------------------------------------------ helpers ---
def now():
    return time.time()


def online(last_seen):
    return (now() - (last_seen or 0)) < ONLINE_WINDOW


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
    r = dict(r)
    r.pop("api_key", None)
    r["online"] = online(r.get("last_seen"))
    try:
        r["capabilities"] = json.loads(r.get("capabilities") or "{}")
    except Exception:
        r["capabilities"] = {}
    return r


def run_public(r):
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
    web_dir = WEB_DIR

    # -- plumbing ----------------------------------------------------------
    def log_message(self, fmt, *args):
        if getattr(self.server, "quiet", False):
            return
        print("[benchd] %s %s" % (time.strftime("%H:%M:%S"), fmt % args), flush=True)

    def _send(self, code, obj=None, raw=None, ctype="application/json", headers=None):
        body = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _err(self, code, msg):
        self._send(code, {"error": msg})

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode() or "{}")
        except Exception:
            return {}

    # -- auth --------------------------------------------------------------
    def _bearer(self):
        h = self.headers.get("Authorization", "")
        return h[7:] if h.startswith("Bearer ") else None

    def _runner_from_bearer(self):
        b = self._bearer()
        if not b:
            return None
        return self.store.one("SELECT * FROM runners WHERE api_key=?", (b,))

    def _is_operator(self, q=None):
        if self.token is None:
            return True
        b = self._bearer()
        if b and b == self.token:
            return True
        if q and q.get("token", [None])[0] == self.token:
            return True
        return False

    def _runner_ok(self, rid=None):
        if self.token is None:
            return True
        b = self._bearer()
        if b and b == self.token:
            return True
        r = self._runner_from_bearer()
        if r and (rid is None or r["id"] == rid):
            return True
        return False

    # -- dispatch ----------------------------------------------------------
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization,Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        if path == "/health":
            return self._send(200, {"ok": True, "time": now(),
                                    "runners": len(self.store.q("SELECT id FROM runners"))})
        if path.startswith("/api/"):
            if path == "/api/v1/stream":
                if not self._is_operator(q):
                    return self._err(401, "unauthorized")
                return self._stream()
            m = re.match(r"^/api/v1/runners/([^/]+)/commands$", path)
            if m:
                if not self._runner_ok(m.group(1)):
                    return self._err(401, "unauthorized")
                return self._runner_commands(m.group(1))
            if not self._is_operator(q):
                return self._err(401, "unauthorized")
            try:
                return self._api_get(path, q)
            except Exception as exc:
                return self._err(500, "%s: %s" % (type(exc).__name__, exc))
        return self._static(path)

    def do_DELETE(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        if not path.startswith("/api/"):
            return self._err(404, "not found")
        if not self._is_operator(q):
            return self._err(401, "unauthorized")
        m = re.match(r"^/api/v1/schedules/([^/]+)$", path)
        if m:
            self.store.ex("DELETE FROM schedules WHERE id=?", (m.group(1),))
            self.bus.publish({"event": "schedule", "data": {"id": m.group(1), "deleted": True}})
            return self._send(200, {"ok": True})
        m = re.match(r"^/api/v1/runs/([^/]+)$", path)
        if m:
            rid = m.group(1)
            for t in ("phases", "telemetry", "events", "artifacts"):
                self.store.ex("DELETE FROM %s WHERE run_id=?" % t, (rid,))
            self.store.ex("DELETE FROM runs WHERE id=?", (rid,))
            self.bus.publish({"event": "run", "data": {"id": rid, "deleted": True}})
            return self._send(200, {"ok": True})
        return self._err(404, "not found")

    def do_POST(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        if not path.startswith("/api/"):
            return self._err(404, "not found")
        if not self._is_operator(q) and not self._runner_ok():
            return self._err(401, "unauthorized")
        try:
            return self._api_post(path, q, self._body())
        except Exception as exc:
            return self._err(500, "%s: %s" % (type(exc).__name__, exc))

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
            return self._send(200, {"runners": runners, "active": active, "recent": recent,
                                    "metrics": METRICS, "server_time": now()})
        if path == "/api/v1/metrics":
            return self._send(200, {"metrics": METRICS})
        if path == "/api/v1/runners":
            return self._send(200, {"runners": [runner_public(r) for r in
                                                S.q("SELECT * FROM runners ORDER BY name")]})
        if path == "/api/v1/schedules":
            return self._send(200, {"schedules": [schedule_public(s) for s in
                                                  S.q("SELECT * FROM schedules ORDER BY created")]})
        if path == "/api/v1/runs":
            lim = min(int(q.get("limit", ["100"])[0]), 1000)
            off = int(q.get("offset", ["0"])[0])
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
        maxn = min(int(q.get("max", ["600"])[0]), 5000)
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
        after = int(q.get("after", ["0"])[0])
        limit = min(int(q.get("limit", ["2000"])[0]), 5000)
        rows = self.store.q("SELECT id,ts,stream,line FROM logs WHERE run_id=? AND id>? "
                            "ORDER BY id LIMIT ?", (run_id, after, limit))
        return self._send(200, {"run_id": run_id, "lines": rows})

    def _compute_stats(self, run_id):
        rows = self.store.q("SELECT * FROM telemetry WHERE run_id=? ORDER BY ts", (run_id,))
        agg = {}
        for r in rows:
            for k, v in tele_public(r).items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    a = agg.get(k)
                    if a is None:
                        agg[k] = [1, float(v), float(v), float(v)]
                    else:
                        a[0] += 1
                        a[1] += v
                        if v < a[2]:
                            a[2] = v
                        if v > a[3]:
                            a[3] = v
        stats = {k: {"n": a[0], "avg": round(a[1] / a[0], 2), "min": a[2], "max": a[3]}
                 for k, a in agg.items() if a[0]}
        return {"samples": len(rows), "stats": stats}

    def _run_stats(self, run_id):
        return self._send(200, dict({"run_id": run_id}, **self._compute_stats(run_id)))

    def _telemetry_csv(self, run_id):
        rows = self.store.q("SELECT ts,elapsed,temp_c,fan_pct,power_w,gpu_util,gpu_fan_rpm,"
                            "cpu_util,ram_used,sm_mhz,mem_used FROM telemetry "
                            "WHERE run_id=? ORDER BY ts", (run_id,))
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["ts", "elapsed_s", "temp_c", "fan_pct", "power_w", "gpu_util_pct",
                    "gpu_fan_rpm", "cpu_util_pct", "ram_used_gib", "sm_mhz", "mem_used_gib"])
        for r in rows:
            w.writerow([r[k] for k in ("ts", "elapsed", "temp_c", "fan_pct", "power_w",
                                       "gpu_util", "gpu_fan_rpm", "cpu_util", "ram_used",
                                       "sm_mhz", "mem_used")])
        return self._send(200, raw=buf.getvalue().encode(), ctype="text/csv",
                          headers={"Content-Disposition":
                                   'attachment; filename="%s-telemetry.csv"' % run_id})

    def _artifact_download(self, run_id, name):
        a = self.store.one("SELECT * FROM artifacts WHERE run_id=? AND name=?", (run_id, name))
        if not a:
            return self._err(404, "no such artifact")
        ctype = "application/json" if a["kind"] == "json" else "text/plain; charset=utf-8"
        return self._send(200, raw=(a["content"] or "").encode(), ctype=ctype,
                          headers={"Content-Disposition":
                                   'attachment; filename="%s"' % a["name"]})

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
        return self._send(200, {"hosts": hosts, "metrics": METRICS, "series": series})

    def _stream(self):
        q = self.bus.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
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
            pass
        finally:
            self.bus.unsubscribe(q)

    def _sse(self, event, data):
        self.wfile.write(("event: %s\ndata: %s\n\n" % (event, json.dumps(data))).encode())
        self.wfile.flush()

    # -- POST api ----------------------------------------------------------
    def _api_post(self, path, q, b):
        S = self.store
        if path == "/api/v1/register":
            return self._register(b)
        m = re.match(r"^/api/v1/runners/([^/]+)/heartbeat$", path)
        if m:
            rid = m.group(1)
            fields, args = ["last_seen=?"], [now()]
            for k in ("status", "current_run", "capabilities", "version", "git"):
                if k in b:
                    fields.append("%s=?" % k)
                    args.append(json.dumps(b[k]) if k == "capabilities" else b[k])
            args.append(rid)
            S.ex("UPDATE runners SET %s WHERE id=?" % ",".join(fields), args)
            r = runner_public(S.one("SELECT * FROM runners WHERE id=?", (rid,)))
            self.bus.publish({"event": "runner", "data": r})
            pending = S.one("SELECT COUNT(*) c FROM commands WHERE runner_id=? AND status='queued'",
                            (rid,))["c"]
            return self._send(200, {"ok": True, "commands": pending, "server_time": now()})
        m = re.match(r"^/api/v1/runners/([^/]+)/commands$", path)
        if m:
            if not self._is_operator(q):
                return self._err(401, "unauthorized")
            return self._issue_command(m.group(1), b)
        m = re.match(r"^/api/v1/runners/([^/]+)/commands/([^/]+)/ack$", path)
        if m:
            S.ex("UPDATE commands SET status=?, acked=?, result=? WHERE id=?",
                 (b.get("status", "done"), now(), json.dumps(b.get("result") or {}), m.group(2)))
            self.bus.publish({"event": "command", "data": {"id": m.group(2), "runner_id": m.group(1),
                                                           "status": b.get("status", "done"),
                                                           "result": b.get("result")}})
            return self._send(200, {"ok": True})
        if path == "/api/v1/schedules":
            return self._create_schedule(b)
        if path == "/api/v1/runs":
            rid = b.get("runner_id")
            run_id = b.get("id") or str(uuid.uuid4())
            S.ex("INSERT INTO runs(id,runner_id,host,label,profile,started,status,platform,gpu,"
                 "driver,torch,system,preset) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (run_id, rid, b.get("host"), b.get("label"), json.dumps(b.get("profile") or {}),
                  b.get("started") or now(), "running", b.get("platform"), b.get("gpu"),
                  b.get("driver"), b.get("torch"), json.dumps(b.get("system") or {}),
                  b.get("preset") or (b.get("profile") or {}).get("preset")))
            if rid:
                S.ex("UPDATE runners SET status='busy', current_run=? WHERE id=?", (run_id, rid))
            run = run_public(S.one("SELECT * FROM runs WHERE id=?", (run_id,)))
            self.bus.publish({"event": "run", "data": run})
            return self._send(200, {"run_id": run_id})
        m = re.match(r"^/api/v1/runs/([^/]+)/phase$", path)
        if m:
            run_id = m.group(1)
            seq = int(b.get("seq", 0))
            existing = S.one("SELECT id FROM phases WHERE run_id=? AND seq=?", (run_id, seq))
            if existing:
                S.ex("UPDATE phases SET status=?, t1=?, detail=? WHERE id=?",
                     (b.get("status", "running"), b.get("t1"), b.get("detail"), existing["id"]))
            else:
                S.ex("INSERT INTO phases(run_id,seq,grp,label,status,t0,t1,detail) "
                     "VALUES(?,?,?,?,?,?,?,?)",
                     (run_id, seq, b.get("group"), b.get("label"), b.get("status", "running"),
                      b.get("t0") or now(), b.get("t1"), b.get("detail")))
            self.bus.publish({"event": "phase", "data": {
                "run_id": run_id, "seq": seq, "group": b.get("group"), "label": b.get("label"),
                "status": b.get("status", "running"), "detail": b.get("detail")}})
            return self._send(200, {"ok": True})
        m = re.match(r"^/api/v1/runs/([^/]+)/telemetry$", path)
        if m:
            run_id = m.group(1)
            samples = b.get("samples") or []
            rows = []
            last_named = None
            for s in samples:
                if isinstance(s, dict):
                    core = [s.get(f) for f in SAMPLE_FIELDS]
                    extra = {k: v for k, v in s.items()
                             if k not in SAMPLE_FIELDS and isinstance(v, (int, float))}
                    last_named = dict(s)
                else:
                    core = (list(s) + [None] * len(SAMPLE_FIELDS))[:len(SAMPLE_FIELDS)]
                    extra = {}
                    last_named = dict(zip(SAMPLE_FIELDS, core))
                rows.append([run_id, now()] + core + [json.dumps(extra) if extra else None])
            with S.lock:
                S.conn.executemany(
                    "INSERT INTO telemetry(run_id,ts,elapsed,temp_c,fan_pct,power_w,gpu_util,"
                    "gpu_fan_rpm,cpu_util,ram_used,sm_mhz,mem_used,extra) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                S.conn.commit()
            if last_named is not None:
                self.bus.publish({"event": "telemetry",
                                  "data": {"run_id": run_id, "sample": last_named, "n": len(rows)}})
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
            content = b.get("content")
            raw = base64.b64decode(content) if content else b""
            S.ex("INSERT INTO artifacts(run_id,name,kind,size,sha256,content) VALUES(?,?,?,?,?,?)",
                 (run_id, name, b.get("kind", "json"), len(raw),
                  b.get("sha256"), raw.decode("utf-8", "replace")))
            return self._send(200, {"ok": True})
        m = re.match(r"^/api/v1/runs/([^/]+)/finish$", path)
        if m:
            run_id = m.group(1)
            S.ex("UPDATE runs SET status=?, ended=?, exit_code=?, summary=?, platform=COALESCE(?,platform),"
                 " gpu=COALESCE(?,gpu), driver=COALESCE(?,driver), torch=COALESCE(?,torch),"
                 " system=CASE WHEN ?='{}' THEN system ELSE ? END WHERE id=?",
                 (b.get("status", "done"), b.get("ended") or now(), b.get("exit_code"),
                  json.dumps(b.get("summary") or {}), b.get("platform"), b.get("gpu"),
                  b.get("driver"), b.get("torch"), json.dumps(b.get("system") or {}),
                  json.dumps(b.get("system") or {}), run_id))
            run = S.one("SELECT runner_id FROM runs WHERE id=?", (run_id,))
            if run and run["runner_id"]:
                S.ex("UPDATE runners SET status='idle', current_run=NULL WHERE id=?",
                     (run["runner_id"],))
            run = run_public(S.one("SELECT * FROM runs WHERE id=?", (run_id,)))
            self.bus.publish({"event": "run", "data": run})
            return self._send(200, {"ok": True})
        return self._err(404, "not found")

    def _register(self, b):
        S = self.store
        rid = b.get("runner_id")
        key = b.get("api_key")
        existing = None
        if rid:
            existing = S.one("SELECT * FROM runners WHERE id=?", (rid,))
            if existing and key and existing["api_key"] != key:
                existing = None  # stale/wrong key -> treat as new
        if not existing:
            rid = rid or str(uuid.uuid4())
            key = secrets.token_urlsafe(32)
        else:
            key = existing["api_key"]
        S.ex("INSERT INTO runners(id,name,os,kind,version,capabilities,first_seen,last_seen,"
             "status,current_run,addr,git,api_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
             "ON CONFLICT(id) DO UPDATE SET name=excluded.name, os=excluded.os, kind=excluded.kind, "
             "version=excluded.version, capabilities=excluded.capabilities, last_seen=excluded.last_seen, "
             "status=excluded.status, addr=excluded.addr, git=excluded.git",
             (rid, b.get("name"), b.get("os"), b.get("kind", "agent"), b.get("version"),
              json.dumps(b.get("capabilities") or {}), now(), now(),
              b.get("status", "idle"), b.get("current_run"), self.client_address[0],
              b.get("git"), key))
        r = runner_public(S.one("SELECT * FROM runners WHERE id=?", (rid,)))
        self.bus.publish({"event": "runner", "data": r})
        return self._send(200, {"runner_id": rid, "api_key": key, "poll_interval": 2.0,
                                "telemetry_interval": 1.0, "server_time": now()})

    def _issue_command(self, rid, b):
        S = self.store
        kind = b.get("kind")
        if kind not in ("start", "stop", "update", "fan_curve", "power_limit", "restart",
                        "exec", "shell"):
            return self._err(400, "unknown command kind")
        cid = str(uuid.uuid4())
        S.ex("INSERT INTO commands(id,runner_id,kind,payload,status,created) VALUES(?,?,?,?,?,?)",
             (cid, rid, kind, json.dumps(b.get("payload") or {}), "queued", now()))
        self.bus.publish({"event": "command", "data": {"id": cid, "runner_id": rid,
                                                       "kind": kind, "status": "queued"}})
        return self._send(200, {"id": cid, "status": "queued"})

    def _create_schedule(self, b):
        S = self.store
        rid = b.get("runner_id")
        if not rid or not S.one("SELECT id FROM runners WHERE id=?", (rid,)):
            return self._err(400, "runner_id required")
        kind = b.get("kind", "every")
        every_s = int(b.get("every_s") or 0)
        at_time = b.get("at_time") or "03:00"
        sid = str(uuid.uuid4())
        nxt = (next_daily(at_time) if kind == "daily"
               else now() + max(60, every_s))
        S.ex("INSERT INTO schedules(id,runner_id,label,profile,kind,every_s,at_time,next_run,"
             "enabled,last_run,created,last_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
             (sid, rid, b.get("label") or "scheduled run", json.dumps(b.get("profile") or {}),
              kind, every_s, at_time, nxt, 1 if b.get("enabled", True) else 0, None, now(), None))
        s = schedule_public(S.one("SELECT * FROM schedules WHERE id=?", (sid,)))
        self.bus.publish({"event": "schedule", "data": s})
        return self._send(200, {"id": sid, "next_run": nxt})

    # -- static ------------------------------------------------------------
    def _static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        rel = path.lstrip("/")
        full = os.path.normpath(os.path.join(self.web_dir, rel))
        if not full.startswith(os.path.normpath(self.web_dir)) or not os.path.isfile(full):
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
        cmd = str(uuid.uuid4())
        store.ex("INSERT INTO commands(id,runner_id,kind,payload,status,created) "
                 "VALUES(?,?,?,?,?,?)",
                 (cmd, s["runner_id"], "start", s["profile"] or "{}", "queued", now()))
        nxt = (next_daily(s["at_time"]) if s["kind"] == "daily"
               else now() + max(60, s["every_s"] or 3600))
        store.ex("UPDATE schedules SET next_run=?, last_run=?, last_status=? WHERE id=?",
                 (nxt, now(), "queued", s["id"]))
        bus.publish({"event": "command", "data": {"id": cmd, "runner_id": s["runner_id"],
                                                  "kind": "start", "status": "queued"}})
        bus.publish({"event": "schedule", "data": schedule_public(
            store.one("SELECT * FROM schedules WHERE id=?", (s["id"],)))})
        fired += 1
    return fired


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


def scheduler_loop(store, bus, stop):
    """Fire due schedules by queueing a start command to the target runner."""
    while not stop.is_set():
        try:
            scheduler_tick(store, bus)
            reap_stale_runs(store, bus)
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


def main():
    ap = argparse.ArgumentParser(description="bench-test control plane")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8722)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--token", default=None, help="join/registration + operator API bearer")
    ap.add_argument("--web", default=WEB_DIR)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    token = find_token(a.token)
    store = Store(a.db)
    Handler.store, Handler.bus, Handler.token, Handler.web_dir = store, Bus(), token, a.web
    httpd = ThreadingHTTPServer((a.host, a.port), Handler)
    httpd.quiet = a.quiet
    httpd.daemon_threads = True

    stop = threading.Event()
    threading.Thread(target=scheduler_loop, args=(store, Handler.bus, stop), daemon=True).start()

    print("[benchd] listening on http://%s:%d  db=%s  auth=%s"
          % (a.host, a.port, a.db, "token+login" if token else "OPEN (no token)"), flush=True)
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
    main()
