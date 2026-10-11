#!/usr/bin/env python3
"""benchreport.py - report a bench-test run to a benchd control plane.

A tiny stdlib-only client shared by the headless agent (bench/agent.py) and the
GUI/TUI dashboards, so any of them can stream live phases + telemetry to the
server and pick up operator commands.

    from benchreport import Reporter, PhaseTracker
    rep = Reporter("http://lab:8722", token, kind="agent")
    if rep.register(capabilities={"groups": [...]}):
        rep.start_run("Quick check", profile={"gpu_soak": 60})
        tr = PhaseTracker(rep)
        for line in runner_output: tr.feed(line)
        rep.finish("done", 0, summary=build_summary(report_json))

Everything fails soft: if the server is unreachable the run continues and the
call is a no-op, so benchmarking never depends on the control plane.
"""
import glob
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

GROUPS = ["gpu", "cpu", "ram", "disk", "combined", "fullsoak", "net", "nvenc", "sys", "gfx", "fan"]

SAMPLE_FIELDS = ["elapsed", "temp_c", "fan_pct", "power_w", "gpu_util",
                 "gpu_fan_rpm", "cpu_util", "ram_used", "sm_mhz", "mem_used"]

# columns the telemetry CSV may expose -> our canonical field names
CSV_ALIASES = {
    "elapsed_s": "elapsed", "elapsed": "elapsed",
    "temp_c": "temp_c", "gpu_temp_c": "temp_c",
    "fan_pct": "fan_pct", "gpu_fan_pct": "fan_pct",
    "power_w": "power_w", "gpu_power_w": "power_w",
    "gpu_util_pct": "gpu_util", "util_pct": "gpu_util", "gpu_util": "gpu_util",
    "gpu_fan_rpm": "gpu_fan_rpm", "fan_rpm": "gpu_fan_rpm",
    "cpu_util_pct": "cpu_util", "cpu_util": "cpu_util",
    "ram_used_gib": "ram_used", "ram_used": "ram_used",
    "sm_mhz": "sm_mhz", "sm_clock_mhz": "sm_mhz",
    "mem_used_gib": "mem_used", "vram_used_gib": "mem_used",
}


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def identity_path():
    return os.environ.get("BENCH_AGENT_IDENTITY") or os.path.expanduser("~/.config/bench-agent.json")


def load_identity(server):
    """Return {runner_id, api_key} previously issued for this server, if any."""
    try:
        with open(identity_path()) as fh:
            data = json.load(fh)
        return data.get((server or "").rstrip("/"), {}) or {}
    except Exception:
        return {}


def save_identity(server, runner_id, api_key):
    try:
        p = identity_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        data = {}
        if os.path.isfile(p):
            try:
                with open(p) as fh:
                    data = json.load(fh)
            except Exception:
                data = {}
        data[(server or "").rstrip("/")] = {"runner_id": runner_id, "api_key": api_key}
        with open(p, "w") as fh:
            json.dump(data, fh, indent=2)
        try:
            os.chmod(p, 0o600)
        except Exception:
            pass
    except Exception:
        pass


class Reporter:
    def __init__(self, server, token, name=None, kind="agent", version=None,
                 os_name=None, timeout=6, log=None, git=None, runner_id=None,
                 api_key=None, persist=False):
        self.server = (server or "").rstrip("/")
        self.join_token = token or ""
        self.token = api_key or token or ""
        self.runner_id = runner_id
        self.api_key = api_key
        self.persist = persist
        self.name = name or socket.gethostname()
        self.kind = kind
        self.os_name = os_name or sys.platform
        self.version = version
        self.git = git
        self.timeout = timeout
        self.log = log or (lambda *_a: None)
        self.run_id = None
        self.enabled = bool(self.server and self.token)
        self.last_error = None

    # -- transport ---------------------------------------------------------
    def _req(self, method, path, body=None):
        if not self.enabled:
            return 0, {}
        url = self.server + path
        req = urllib.request.Request(url, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        req.add_header("User-Agent", "bench-test-reporter")
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, data, timeout=self.timeout) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            self.last_error = "HTTP %s" % e.code
            return e.code, {}
        except Exception as e:
            self.last_error = str(e)
            return 0, {}

    # -- lifecycle ---------------------------------------------------------
    def register(self, capabilities=None, status="idle"):
        s, r = self._req("POST", "/api/v1/register", {
            "runner_id": self.runner_id, "api_key": self.api_key,
            "name": self.name, "os": self.os_name, "kind": self.kind, "version": self.version,
            "git": self.git, "capabilities": capabilities or {}, "status": status,
            "current_run": self.run_id})
        if s == 200 and r.get("runner_id"):
            self.runner_id = r["runner_id"]
            if r.get("api_key"):
                self.api_key = r["api_key"]
                self.token = r["api_key"]
                if self.persist:
                    save_identity(self.server, self.runner_id, self.api_key)
            self.log("[report] registered as %s (%s)" % (self.runner_id[:8], self.server))
            return True
        self.log("[report] register failed (%s)" % (self.last_error or s))
        return False

    def heartbeat(self, status=None, current_run=None, capabilities=None):
        if not self.runner_id:
            return None
        body = {}
        if status is not None:
            body["status"] = status
        body["current_run"] = current_run if current_run is not None else self.run_id
        if capabilities is not None:
            body["capabilities"] = capabilities
        s, r = self._req("POST", "/api/v1/runners/%s/heartbeat" % self.runner_id, body)
        return r if s == 200 else None

    def start_run(self, label, profile=None, host=None, platform=None, gpu=None,
                  driver=None, torch=None):
        if not self.runner_id:
            return None
        s, r = self._req("POST", "/api/v1/runs", {
            "runner_id": self.runner_id, "host": host or self.name, "label": label,
            "profile": profile or {}, "started": time.time(), "platform": platform,
            "gpu": gpu, "driver": driver, "torch": torch})
        if s == 200:
            self.run_id = r.get("run_id")
            return self.run_id
        return None

    def phase(self, seq, group, label, status="running", t0=None, t1=None, detail=None):
        if not self.run_id:
            return
        self._req("POST", "/api/v1/runs/%s/phase" % self.run_id,
                  {"seq": seq, "group": group, "label": label, "status": status,
                   "t0": t0, "t1": t1, "detail": detail})

    def telemetry(self, samples):
        if not self.run_id or not samples:
            return
        self._req("POST", "/api/v1/runs/%s/telemetry" % self.run_id, {"samples": samples})

    def event(self, message, level="info"):
        if not self.run_id:
            return
        self._req("POST", "/api/v1/runs/%s/event" % self.run_id,
                  {"level": level, "message": message, "ts": time.time()})

    def artifact(self, name, text, kind="json"):
        if not self.run_id:
            return
        import base64
        raw = text.encode("utf-8", "replace") if isinstance(text, str) else text
        self._req("POST", "/api/v1/runs/%s/artifact" % self.run_id,
                  {"name": name, "kind": kind, "content": base64.b64encode(raw).decode()})

    def finish(self, status="done", exit_code=0, summary=None, platform=None, gpu=None,
               driver=None, torch=None):
        if not self.run_id:
            return
        self._req("POST", "/api/v1/runs/%s/finish" % self.run_id, {
            "status": status, "exit_code": exit_code, "summary": summary or {},
            "platform": platform, "gpu": gpu, "driver": driver, "torch": torch})
        self.run_id = None

    # -- control -----------------------------------------------------------
    def poll_commands(self):
        if not self.runner_id:
            return []
        s, r = self._req("GET", "/api/v1/runners/%s/commands" % self.runner_id)
        return r.get("commands", []) if s == 200 else []

    def ack(self, cid, status="done", result=None):
        if not self.runner_id:
            return
        self._req("POST", "/api/v1/runners/%s/commands/%s/ack" % (self.runner_id, cid),
                  {"status": status, "result": result or {}})


class PhaseTracker:
    """Turns '[group] label' runner lines into open/close phase events.

    Mirrors the dashboards: a new phase closes *every* still-open phase, so a
    group's last row is closed when the run moves to the next group.
    """

    def __init__(self, reporter=None):
        self.reporter = reporter
        self.seq = 0
        self.open = []  # list of {seq, group, label}

    def feed(self, line):
        if not (line.startswith("[") and "]" in line):
            return None
        head = line[1:line.index("]")]
        rest = line[line.index("]") + 1:].strip()
        if head == "warn":
            if self.reporter:
                self.reporter.event(rest, "warn")
            return ("warn", rest)
        if head not in GROUPS:
            return None
        self._close_all()
        self.seq += 1
        ph = {"seq": self.seq, "group": head, "label": rest}
        self.open.append(ph)
        if self.reporter:
            self.reporter.phase(ph["seq"], head, rest, "running", t0=time.time())
        return ("open", head, rest)

    def _close_all(self):
        for ph in self.open:
            if self.reporter:
                self.reporter.phase(ph["seq"], ph["group"], ph["label"], "done", t1=time.time())
        self.open = []

    def close(self):
        self._close_all()


class ReporterPump:
    """Background thread that flushes telemetry, heartbeats and polls commands.

    Keeps the dashboard UI thread free of blocking HTTP calls; if the server is
    unreachable the queue simply drains into no-ops.
    """

    def __init__(self, reporter, on_command=None, interval=2.0, log=None):
        import queue
        self.rep = reporter
        self.on_command = on_command
        self.interval = interval
        self.log = log or (lambda *_a: None)
        self.samples = queue.Queue()
        self._stop = False
        self._thread = None

    def start(self):
        import threading
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True

    def push_sample(self, row):
        try:
            self.samples.put_nowait(row)
        except Exception:
            pass

    def _drain(self):
        batch = []
        while len(batch) < 120:
            try:
                batch.append(self.samples.get_nowait())
            except Exception:
                break
        if batch:
            self.rep.telemetry(batch)

    def _loop(self):
        while not self._stop:
            try:
                self._drain()
                self.rep.heartbeat()
                for c in self.rep.poll_commands():
                    if self.on_command:
                        try:
                            self.on_command(c)
                        except Exception as e:
                            self.log("[report] command failed: %s" % e)
                            self.rep.ack(c.get("id"), "failed", {"error": str(e)})
            except Exception as e:
                self.log("[report] pump error: %s" % e)
            time.sleep(self.interval)


class TelemetryTail:
    """Tails the newest 1 Hz telemetry CSV and yields canonical sample rows."""

    def __init__(self, results_dir, interval=1.0):
        self.dir = results_dir
        self.interval = interval
        self.path = None
        self.fh = None
        self.cols = {}
        self._n = 0

    def _find(self):
        files = glob.glob(os.path.join(self.dir, "*.telemetry.csv"))
        if not files:
            return None
        newest = max(files, key=os.path.getmtime)
        return newest if newest != self.path else None

    def _open(self, path):
        try:
            self.fh = open(path)
            header = self.fh.readline().strip().split(",")
        except OSError:
            self.fh = None
            return
        self.path = path
        self.cols = {}
        for i, name in enumerate(header):
            canon = CSV_ALIASES.get(name.strip().lower())
            if canon:
                self.cols[canon] = i
        self.fh.seek(0)
        self.fh.readline()  # skip header for the tail

    def read(self):
        """Return a list of canonical sample rows since the last call."""
        if self.fh is None:
            p = self._find()
            if p:
                self._open(p)
            else:
                return []
        out = []
        while True:
            line = self.fh.readline()
            if not line:
                break
            vals = line.strip().split(",")
            if not vals or vals[0] == "":
                continue
            row = []
            for f in SAMPLE_FIELDS:
                i = self.cols.get(f)
                row.append(_f(vals[i]) if (i is not None and i < len(vals)) else None)
            out.append(row)
        return out

    def close(self):
        if self.fh:
            try:
                self.fh.close()
            except Exception:
                pass
            self.fh = None


# --------------------------------------------------------------- summaries ---
def build_summary(report_path):
    """Extract a runs-index-style summary row from a report JSON (any platform)."""
    out = {"report": report_path}
    try:
        with open(report_path) as fh:
            data = json.load(fh)
    except Exception as e:
        return {"error": str(e)}
    out["host"] = data.get("host")
    out["timestamp"] = data.get("timestamp")
    for ph in data.get("phases", []):
        if not isinstance(ph, dict):
            continue
        phase = ph.get("phase")
        group = ph.get("group")
        r = ph.get("result") or {}
        if not isinstance(r, dict):
            continue
        if phase == "matmul" and isinstance(r.get("results"), dict):
            sub = r["results"]
            first = next(iter(sub.values()), {}) if sub else {}
            if isinstance(first, dict) and "tflops" in first:
                out["gpu_matmul_tflops"] = {k: v.get("tflops") for k, v in sub.items()}
            elif isinstance(first, dict) and "gflops" in first:
                out["cpu_matmul_gflops"] = {k: v.get("gflops") for k, v in sub.items()}
        elif phase == "soak":
            if r.get("matmul_tflops") is not None:
                out["gpu_soak_tflops"] = r.get("matmul_tflops")
                out["gpu_soak_telemetry"] = r.get("telemetry")
            elif r.get("gflops") is not None:
                out["cpu_soak_gflops"] = r.get("gflops")
            elif r.get("gbps") is not None:
                out["ram_soak_gbps"] = r.get("gbps")
        elif phase == "seqwrite" and r.get("mbps") is not None:
            out["disk_seqwrite_mbps"] = r.get("mbps")
        elif phase == "bench" and r.get("download_mbps") is not None:
            out["net"] = r
        elif phase in ("stress", "fullsoak") and isinstance(r.get("verdict"), dict):
            out["fullsoak"] = {"group": group or "fullsoak", "phase": phase, "result": r}
    return out


def read_runs_index(results_dir):
    """Return the last runs-index row in a results dir, if the runner wrote one."""
    path = os.path.join(results_dir, "runs-index.jsonl")
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as fh:
            lines = [ln for ln in fh if ln.strip()]
        return json.loads(lines[-1]) if lines else None
    except Exception:
        return None


def newest_report(results_dir):
    cands = (glob.glob(os.path.join(results_dir, "*-linux-*.json"))
             + glob.glob(os.path.join(results_dir, "*-*.json")))
    cands = [c for c in cands if not c.endswith((".info.json", ".llm.json", ".nvenc.json",
                                                 ".sysinfo.json", ".sensors.json", ".pcie.json",
                                                 ".health.json", ".env.json", ".manifest.json"))]
    if not cands:
        return None
    return max(cands, key=os.path.getmtime)
