#!/usr/bin/env python3
"""benchd - bench-test control plane (single pane of glass).

A dependency-free HTTP + SSE server that:

  * lets runners (the headless agent, and the GUI/TUI dashboards) register and
    stream live phase + telemetry data,
  * queues operator commands (start/stop a run, update the toolkit, set the fan
    curve / power limit) for runners to pick up,
  * serves a self-contained web dashboard over history, live runs, trends and
    side-by-side comparisons.

Storage is SQLite (stdlib).  Nothing outside the Python standard library is
required, so it runs on the Proxmox lab box and on the live USB alike.

    python3 server/benchd.py --host 0.0.0.0 --port 8722

Config (first wins): --token flag, $BENCHD_TOKEN, server/benchd.conf,
~/.config/benchd.conf   (key=value, `BENCHD_TOKEN=...`).

API (all under /api/v1, bearer-token protected except /health):
  runner  POST /register
          POST /runners/<id>/heartbeat
          GET  /runners/<id>/commands
          POST /runners/<id>/commands/<cid>/ack
          POST /runs
          POST /runs/<id>/phase
          POST /runs/<id>/telemetry
          POST /runs/<id>/event
          POST /runs/<id>/artifact
          POST /runs/<id>/finish
  ui      GET  /state  /stream  /runners  /runs  /runs/<id>
          GET  /runs/<id>/telemetry  /trends  /metrics
          POST /runners/<id>/commands
"""
import argparse
import base64
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")
DEFAULT_DB = os.path.join(HERE, "data", "benchd.sqlite")

# canonical telemetry sample order (matches bench/agent.py)
SAMPLE_FIELDS = ["elapsed", "temp_c", "fan_pct", "power_w", "gpu_util",
                 "gpu_fan_rpm", "cpu_util", "ram_used", "sm_mhz", "mem_used"]

# metrics offered for trends/compare, as dotted paths into a run's summary row
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

ONLINE_WINDOW = 15.0  # seconds since last heartbeat before a runner is offline

SCHEMA = """
CREATE TABLE IF NOT EXISTS runners (
  id TEXT PRIMARY KEY, name TEXT, os TEXT, kind TEXT, version TEXT,
  capabilities TEXT, first_seen REAL, last_seen REAL, status TEXT,
  current_run TEXT, addr TEXT, git TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, runner_id TEXT, host TEXT, label TEXT, profile TEXT,
  started REAL, ended REAL, status TEXT, exit_code INTEGER,
  platform TEXT, gpu TEXT, driver TEXT, torch TEXT, summary TEXT
);
CREATE TABLE IF NOT EXISTS phases (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, seq INTEGER, grp TEXT,
  label TEXT, status TEXT, t0 REAL, t1 REAL, detail TEXT
);
CREATE TABLE IF NOT EXISTS telemetry (
  run_id TEXT, ts REAL, elapsed REAL, temp_c REAL, fan_pct REAL, power_w REAL,
  gpu_util REAL, gpu_fan_rpm REAL, cpu_util REAL, ram_used REAL,
  sm_mhz REAL, mem_used REAL
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
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started);
CREATE INDEX IF NOT EXISTS idx_runs_runner ON runs(runner_id);
CREATE INDEX IF NOT EXISTS idx_phases_run ON phases(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_tele_run ON telemetry(run_id, ts);
CREATE INDEX IF NOT EXISTS idx_cmd_runner ON commands(runner_id, status);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, ts);
"""


# --------------------------------------------------------------------- store --
class Store:
    """SQLite access, serialised behind one lock (low write volume)."""

    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.executescript("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")
            self.conn.executescript(SCHEMA)
            self.conn.commit()

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
    """Fan-out of live events to SSE subscribers."""

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


# --------------------------------------------------------------- helpers -----
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
    r["online"] = online(r.get("last_seen"))
    try:
        r["capabilities"] = json.loads(r.get("capabilities") or "{}")
    except Exception:
        r["capabilities"] = {}
    return r


def run_public(r):
    r = dict(r)
    if r.get("summary"):
        try:
            r["summary"] = json.loads(r["summary"])
        except Exception:
            pass
    if r.get("profile"):
        try:
            r["profile"] = json.loads(r["profile"])
        except Exception:
            pass
    return r


# ------------------------------------------------------------------ server ----
class Handler(BaseHTTPRequestHandler):
    server_version = "benchd/1.0"
    protocol_version = "HTTP/1.0"
    store = None
    bus = None
    token = None
    web_dir = WEB_DIR

    # -- plumbing ----------------------------------------------------------
    def log_message(self, fmt, *args):
        if getattr(self.server, "quiet", False):
            return
        ts = time.strftime("%H:%M:%S")
        print("[benchd] %s %s" % (ts, fmt % args), flush=True)

    def _send(self, code, obj=None, raw=None, ctype="application/json"):
        body = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
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

    def _auth(self, q):
        if self.token is None:
            return True
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Bearer ") and hdr[7:] == self.token:
            return True
        if q.get("token", [None])[0] == self.token:
            return True
        return False

    # -- dispatch ----------------------------------------------------------
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization,Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path
        if path == "/health":
            return self._send(200, {"ok": True, "runners": len(self.store.q("SELECT id FROM runners")),
                                    "time": now()})
        if path.startswith("/api/"):
            if not self._auth(q):
                return self._err(401, "unauthorized")
            try:
                return self._api_get(path, q)
            except Exception as exc:
                return self._err(500, "%s: %s" % (type(exc).__name__, exc))
        return self._static(path)

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path
        if not path.startswith("/api/"):
            return self._err(404, "not found")
        if not self._auth(q):
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
                last = S.one("SELECT * FROM telemetry WHERE run_id=? ORDER BY ts DESC LIMIT 1",
                             (a["id"],))
                a["latest"] = last
                a["phases"] = S.q("SELECT seq,grp,label,status,t0,t1,detail FROM phases "
                                  "WHERE run_id=? ORDER BY seq", (a["id"],))
            recent = [run_public(r) for r in
                      S.q("SELECT * FROM runs ORDER BY started DESC LIMIT 25")]
            return self._send(200, {"runners": runners, "active": active, "recent": recent,
                                    "metrics": METRICS, "server_time": now()})
        if path == "/api/v1/metrics":
            return self._send(200, {"metrics": METRICS})
        if path == "/api/v1/runners":
            return self._send(200, {"runners": [runner_public(r) for r in
                                                S.q("SELECT * FROM runners ORDER BY name")]})
        m = re.match(r"^/api/v1/runners/([^/]+)/commands$", path)
        if m:
            rows = S.q("SELECT id,run_id,kind,payload,status,created,result FROM commands "
                       "WHERE runner_id=? AND status='queued' ORDER BY created", (m.group(1),))
            out = []
            for r in rows:
                try:
                    r["payload"] = json.loads(r["payload"] or "{}")
                except Exception:
                    r["payload"] = {}
                out.append(r)
            if out:
                S.ex("UPDATE commands SET status='sent', sent=? WHERE runner_id=? AND status='queued'",
                     (now(), m.group(1)))
            return self._send(200, {"commands": out})
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
        m = re.match(r"^/api/v1/runs/([^/]+)/telemetry$", path)
        if m:
            return self._run_telemetry(m.group(1), q)
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
            return self._send(200, run)
        if path == "/api/v1/trends":
            return self._trends(q)
        if path == "/api/v1/stream":
            return self._stream()
        return self._err(404, "not found")

    def _run_telemetry(self, run_id, q):
        S = self.store
        maxn = min(int(q.get("max", ["600"])[0]), 5000)
        rows = S.q("SELECT * FROM telemetry WHERE run_id=? ORDER BY ts", (run_id,))
        if len(rows) > maxn:  # downsample evenly, keeping the last sample
            step = len(rows) / float(maxn)
            picked, i = [], 0.0
            while int(i) < len(rows):
                picked.append(rows[int(i)])
                i += step
            if picked[-1] is not rows[-1]:
                picked.append(rows[-1])
            rows = picked
        fields = SAMPLE_FIELDS + ["ts"]
        return self._send(200, {"run_id": run_id, "fields": fields,
                                "rows": [[r.get(f) for f in fields] for r in rows]})

    def _trends(self, q):
        host = q.get("host", [None])[0]
        metrics = q.get("metric", [])
        if not metrics:
            metrics = [m[0] for m in METRICS[:4]]
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
            # initial snapshot
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
            rid = b.get("runner_id") or str(uuid.uuid4())
            S.ex("INSERT INTO runners(id,name,os,kind,version,capabilities,first_seen,last_seen,"
                 "status,current_run,addr,git) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                 "ON CONFLICT(id) DO UPDATE SET name=excluded.name, os=excluded.os, "
                 "kind=excluded.kind, version=excluded.version, capabilities=excluded.capabilities, "
                 "last_seen=excluded.last_seen, status=excluded.status, addr=excluded.addr, "
                 "git=excluded.git",
                 (rid, b.get("name"), b.get("os"), b.get("kind", "agent"), b.get("version"),
                  json.dumps(b.get("capabilities") or {}), now(), now(),
                  b.get("status", "idle"), b.get("current_run"),
                  self.client_address[0], b.get("git")))
            r = runner_public(S.one("SELECT * FROM runners WHERE id=?", (rid,)))
            self.bus.publish({"event": "runner", "data": r})
            return self._send(200, {"runner_id": rid, "poll_interval": 2.0,
                                    "telemetry_interval": 1.0, "server_time": now()})
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
            kind = b.get("kind")
            if kind not in ("start", "stop", "update", "fan_curve", "power_limit", "restart"):
                return self._err(400, "unknown command kind")
            cid = str(uuid.uuid4())
            S.ex("INSERT INTO commands(id,runner_id,kind,payload,status,created) "
                 "VALUES(?,?,?,?,?,?)",
                 (cid, m.group(1), kind, json.dumps(b.get("payload") or {}), "queued", now()))
            cmd = S.one("SELECT * FROM commands WHERE id=?", (cid,))
            self.bus.publish({"event": "command", "data": {"id": cid, "runner_id": m.group(1),
                                                           "kind": kind, "status": "queued"}})
            return self._send(200, {"id": cid, "status": "queued"})
        m = re.match(r"^/api/v1/runners/([^/]+)/commands/([^/]+)/ack$", path)
        if m:
            S.ex("UPDATE commands SET status=?, acked=?, result=? WHERE id=?",
                 (b.get("status", "done"), now(), json.dumps(b.get("result") or {}), m.group(2)))
            self.bus.publish({"event": "command", "data": {"id": m.group(2), "runner_id": m.group(1),
                                                           "status": b.get("status", "done"),
                                                           "result": b.get("result")}})
            return self._send(200, {"ok": True})
        if path == "/api/v1/runs":
            rid = b.get("runner_id")
            run_id = b.get("id") or str(uuid.uuid4())
            S.ex("INSERT INTO runs(id,runner_id,host,label,profile,started,status,platform,gpu,"
                 "driver,torch) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (run_id, rid, b.get("host"), b.get("label"), json.dumps(b.get("profile") or {}),
                  b.get("started") or now(), "running", b.get("platform"), b.get("gpu"),
                  b.get("driver"), b.get("torch")))
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
            ev = {"run_id": run_id, "seq": seq, "group": b.get("group"), "label": b.get("label"),
                  "status": b.get("status", "running"), "detail": b.get("detail")}
            self.bus.publish({"event": "phase", "data": ev})
            return self._send(200, {"ok": True})
        m = re.match(r"^/api/v1/runs/([^/]+)/telemetry$", path)
        if m:
            run_id = m.group(1)
            samples = b.get("samples") or []
            rows = []
            for s in samples:
                vals = (list(s) + [None] * len(SAMPLE_FIELDS))[:len(SAMPLE_FIELDS)]
                rows.append([run_id, now()] + vals)
            with S.lock:
                S.conn.executemany(
                    "INSERT INTO telemetry(run_id,ts,elapsed,temp_c,fan_pct,power_w,gpu_util,"
                    "gpu_fan_rpm,cpu_util,ram_used,sm_mhz,mem_used) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                S.conn.commit()
            if samples:
                last = (list(samples[-1]) + [None] * len(SAMPLE_FIELDS))[:len(SAMPLE_FIELDS)]
                self.bus.publish({"event": "telemetry",
                                  "data": {"run_id": run_id, "sample": last,
                                           "n": len(samples)}})
            return self._send(200, {"ok": True, "accepted": len(rows)})
        m = re.match(r"^/api/v1/runs/([^/]+)/event$", path)
        if m:
            S.ex("INSERT INTO events(run_id,ts,level,message) VALUES(?,?,?,?)",
                 (m.group(1), b.get("ts") or now(), b.get("level", "info"), b.get("message", "")))
            self.bus.publish({"event": "event", "data": {"run_id": m.group(1),
                                                         "level": b.get("level", "info"),
                                                         "message": b.get("message", "")}})
            return self._send(200, {"ok": True})
        m = re.match(r"^/api/v1/runs/([^/]+)/artifact$", path)
        if m:
            run_id = m.group(1)
            name = b.get("name", "artifact")
            content = b.get("content")
            raw = base64.b64decode(content) if content else b""
            S.ex("INSERT INTO artifacts(run_id,name,kind,size,sha256,content) VALUES(?,?,?,?,?,?)",
                 (run_id, name, b.get("kind", "json"), len(raw), b.get("sha256"), raw.decode("utf-8", "replace")))
            return self._send(200, {"ok": True})
        m = re.match(r"^/api/v1/runs/([^/]+)/finish$", path)
        if m:
            run_id = m.group(1)
            status = b.get("status", "done")
            S.ex("UPDATE runs SET status=?, ended=?, exit_code=?, summary=?, platform=COALESCE(?,platform),"
                 " gpu=COALESCE(?,gpu), driver=COALESCE(?,driver), torch=COALESCE(?,torch) WHERE id=?",
                 (status, now(), b.get("exit_code"), json.dumps(b.get("summary") or {}),
                  b.get("platform"), b.get("gpu"), b.get("driver"), b.get("torch"), run_id))
            run = S.one("SELECT runner_id FROM runs WHERE id=?", (run_id,))
            if run and run["runner_id"]:
                S.ex("UPDATE runners SET status='idle', current_run=NULL WHERE id=?",
                     (run["runner_id"],))
            run = run_public(S.one("SELECT * FROM runs WHERE id=?", (run_id,)))
            self.bus.publish({"event": "run", "data": run})
            return self._send(200, {"ok": True})
        return self._err(404, "not found")

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
        with open(full, "rb") as fh:
            return self._send(200, raw=fh.read(), ctype=ctype)


# -------------------------------------------------------------------- config --
def find_token(cli_token):
    if cli_token:
        return cli_token
    if os.environ.get("BENCHD_TOKEN"):
        return os.environ["BENCHD_TOKEN"]
    for cand in (os.path.join(HERE, "benchd.conf"), os.path.expanduser("~/.config/benchd.conf"),
                 "/etc/benchd.conf"):
        if os.path.isfile(cand):
            for ln in open(cand):
                ln = ln.strip()
                if ln.startswith("BENCHD_TOKEN="):
                    return ln.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def main():
    ap = argparse.ArgumentParser(description="bench-test control plane")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8722)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--token", default=None, help="shared bearer token (else env/conf)")
    ap.add_argument("--web", default=WEB_DIR)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    token = find_token(a.token)
    Handler.store = Store(a.db)
    Handler.bus = Bus()
    Handler.token = token
    Handler.web_dir = a.web
    httpd = ThreadingHTTPServer((a.host, a.port), Handler)
    httpd.quiet = a.quiet
    httpd.daemon_threads = True
    print("[benchd] listening on http://%s:%d  db=%s  auth=%s"
          % (a.host, a.port, a.db, "token" if token else "NONE"), flush=True)
    if not token:
        print("[benchd] WARNING: no token set - anyone on the network can control runners",
              flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
