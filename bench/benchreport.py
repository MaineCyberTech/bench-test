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
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

GROUPS = ["gpu", "cpu", "ram", "disk", "combined", "fullsoak", "net", "nvenc",
          "llm", "fan", "sys", "gfx"]

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
    # extras streamed to the server for richer charts
    "mem_util_pct": "mem_util", "mem_util": "mem_util",
    "mem_mhz": "mem_mhz",
    "vram_used_mib": "vram_used_mib",
    "cpu_package_c": "cpu_temp_c", "cpu_temp_c": "cpu_temp_c",
    "cpu_max_core_c": "cpu_max_core_c",
    "cpu_fan_rpm": "cpu_fan_rpm",
    "load1": "load1",
    "swap_used_gib": "swap_used_gib",
    "nvme_c": "nvme_c",
    "gpu_enc_pct": "gpu_enc_pct",
    "gpu_dec_pct": "gpu_dec_pct",
    "pstate": "pstate",
    # throughput / limits (Windows sampler + Linux logger)
    "power_limit_w": "power_limit_w",
    "temp_mem_c": "temp_mem_c",
    "gr_mhz": "gr_mhz",
    "ram_total_gib": "ram_total_gib",
    "disk_read_mbs": "disk_read_mbs",
    "disk_write_mbs": "disk_write_mbs",
    "net_rx_mbs": "net_rx_mbs",
    "net_tx_mbs": "net_tx_mbs",
    # headroom / health (added)
    "temp_limit_c": "temp_limit_c",
    "power_default_w": "power_default_w",
    "power_max_w": "power_max_w",
    "video_mhz": "video_mhz",
    "pcie_gen": "pcie_gen",
    "pcie_width": "pcie_width",
    "retired": "retired",
    "throttle": "throttle",
    "throttle_bits": "throttle_bits",
    "clocks_throttle_reasons_active": "throttle_bits",
    # Linux sampler extras
    "cpu_pkg_w": "cpu_pkg_w",
    "cpu_mhz": "cpu_mhz",
    "psi_io": "psi_io",
    # per-engine GPU (Windows PDH counters)
    "gpu_3d_pct": "gpu_3d_pct",
    "gpu_copy_pct": "gpu_copy_pct",
}


def _pstate(v):
    """'P0'/'P8' -> 0/8 (numeric, so it can be streamed as an extra metric)."""
    s = str(v).strip().upper().lstrip("P")
    return _f(s)


def _parse_conf(path):
    d = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln or ln.startswith("#") or "=" not in ln:
                    continue
                k, v = ln.split("=", 1)
                d[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return d


def load_conf():
    """Resolve (server, token) from the environment or a bench-agent.conf.

    Identical search order on Windows and Linux, so the GUI, TUI and agent all
    check in the same way on every platform:
        $BENCH_AGENT_CONF, ./bench-agent.conf, ~/.config/bench-agent.conf,
        /etc/bench-agent.conf, /mnt/benchdata/bench-agent.conf
    """
    cands = [os.environ.get("BENCH_AGENT_CONF"),
             os.path.join(os.getcwd(), "bench-agent.conf"),
             os.path.expanduser("~/.config/bench-agent.conf"),
             "/etc/bench-agent.conf",
             "/mnt/benchdata/bench-agent.conf"]
    conf = {}
    for c in cands:
        if c and os.path.isfile(c):
            conf = _parse_conf(c)
            break
    server = os.environ.get("BENCHD_SERVER") or conf.get("BENCHD_SERVER") or ""
    token = os.environ.get("BENCHD_TOKEN") or conf.get("BENCHD_TOKEN") or ""
    return server, token


def _cpu_name():
    """A human CPU name (marketing string) rather than platform.processor()."""
    import platform as _p
    if os.name == "nt":
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                               r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            v, _ = winreg.QueryValueEx(k, "ProcessorNameString")
            if v:
                return v.strip()
        except Exception:
            pass
    else:
        try:
            with open("/proc/cpuinfo") as fh:
                for ln in fh:
                    if ln.lower().startswith("model name"):
                        return ln.split(":", 1)[1].strip()
        except Exception:
            pass
    return (_p.processor() or _p.machine() or "").strip()


def system_info():
    """Best-effort host spec block for run history (cpu, ram, gpu, driver, platform)."""
    import platform as _p
    info = {"os": _p.system(), "platform": _p.platform(), "hostname": socket.gethostname(),
            "python": _p.python_version()}
    info["cpu"] = _cpu_name()
    try:
        import psutil
        info["ram_gib"] = round(psutil.virtual_memory().total / 1024 ** 3, 1)
    except Exception:
        try:
            with open("/proc/meminfo") as fh:
                for ln in fh:
                    if ln.startswith("MemTotal"):
                        info["ram_gib"] = round(int(ln.split()[1]) / 1048576.0, 1)
                        break
        except Exception:
            pass
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total",
                              "--format=csv,noheader"], capture_output=True, text=True,
                             timeout=8,
                             creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                            if os.name == "nt" else 0)).stdout.strip()
        if out:
            parts = [x.strip() for x in out.splitlines()[0].split(",")]
            info["gpu"] = parts[0]
            if len(parts) > 1:
                info["driver"] = parts[1]
            if len(parts) > 2:
                info["vram"] = parts[2]
    except Exception:
        pass
    return info


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


_MACHINE_ID = None


def machine_id():
    """A stable, machine-derived id so a runner keeps its identity across
    restarts, reinstalls and ephemeral live-USB boots.

    The persisted runner_id (a random uuid in `bench-agent.json`) does not
    survive those, so re-registration used to mint a fresh runner every time
    (hence the duplicate 'bench-live' / hostname rows). This id is derived from
    the machine instead, so the control plane can recognise a runner it has
    already seen.

    Order: `$BENCH_MACHINE_ID` -> hardware/OS id (DMI uuid **or** board serial,
    Windows `MachineGuid`, `/etc/machine-id`) -> a random id persisted next to
    the identity file as a last resort.
    """
    global _MACHINE_ID
    if _MACHINE_ID:
        return _MACHINE_ID

    def _read(path):
        try:
            with open(path) as fh:
                return fh.read().strip()
        except Exception:
            return ""

    mid = (os.environ.get("BENCH_MACHINE_ID") or "").strip()
    if not mid and os.name != "nt":
        # DMI first: stable on a live USB (which runs as root) and across an OS
        # reinstall; skip the all-zero placeholder some boards report.
        for p in ("/sys/class/dmi/id/product_uuid", "/sys/class/dmi/id/board_serial"):
            v = _read(p).lower()
            if v and set(v.replace("-", "")) != {"0"}:
                mid = "dmi-" + v
                break
    if not mid and os.name == "nt":
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                               r"SOFTWARE\Microsoft\Cryptography", 0,
                               winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
            v = str(winreg.QueryValueEx(k, "MachineGuid")[0]).strip().lower()
            if v:
                mid = "win-" + v
        except Exception:
            pass
    if not mid:
        for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            v = _read(p).lower()
            if v and "uninitialized" not in v:
                mid = "lnx-" + v
                break
    if not mid:
        # last resort: a random id persisted next to the identity file
        try:
            p = os.path.join(os.path.dirname(identity_path()), "bench-machine-id")
            mid = _read(p)
            if not mid:
                mid = "rnd-" + uuid.uuid4().hex
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w") as fh:
                    fh.write(mid)
        except Exception:
            mid = "rnd-" + uuid.uuid4().hex
    _MACHINE_ID = mid
    return mid


class Reporter:
    def __init__(self, server, token, name=None, kind="agent", version=None,
                 os_name=None, timeout=6, log=None, git=None, runner_id=None,
                 api_key=None, persist=False, ephemeral=False, instance=None):
        self.server = (server or "").rstrip("/")
        self.join_token = token or ""
        self.token = api_key or token or ""
        self.runner_id = runner_id
        self.api_key = api_key
        self.persist = persist
        self.ephemeral = ephemeral
        self.name = name or socket.gethostname()
        self.kind = kind
        # Lets several dashboards share a host without colliding: the live USB
        # runs a TUI on tty1 *and* ttyS0, so 'bench-live' alone is ambiguous.
        # Defaults to $BENCH_AGENT_INSTANCE, which bench-tui@.service sets to %i.
        self.instance = (instance if instance is not None
                         else os.environ.get("BENCH_AGENT_INSTANCE") or "").strip()
        self.machine_id = machine_id()
        self.os_name = os_name or sys.platform
        self.version = version
        self.git = git
        self.timeout = timeout
        self.logfn = log or (lambda *_a: None)
        self.run_id = None
        self.enabled = bool(self.server and self.token)
        self.last_error = None
        self.last_ok = None

    def status(self):
        """'off' | 'connecting' | 'connected' | 'offline' for the dashboards."""
        if not self.enabled:
            return "off"
        if not self.runner_id:
            return "connecting"
        if self.last_ok and (time.time() - self.last_ok) < 10:
            return "connected"
        return "offline"

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
                self.last_ok = time.time()
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
            "current_run": self.run_id, "machine_id": self.machine_id,
            "ephemeral": self.ephemeral, "instance": self.instance})
        if s == 200 and r.get("runner_id"):
            self.runner_id = r["runner_id"]
            if r.get("api_key"):
                self.api_key = r["api_key"]
                self.token = r["api_key"]
                if self.persist:
                    save_identity(self.server, self.runner_id, self.api_key)
            self.logfn("[report] registered as %s (%s)" % (self.runner_id[:8], self.server))
            return True
        self.logfn("[report] register failed (%s)" % (self.last_error or s))
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
                  driver=None, torch=None, system=None, preset=None):
        if not self.runner_id:
            return None
        s, r = self._req("POST", "/api/v1/runs", {
            "runner_id": self.runner_id, "host": host or self.name, "label": label,
            "profile": profile or {}, "started": time.time(), "platform": platform,
            "gpu": gpu, "driver": driver, "torch": torch, "system": system or {},
            "preset": preset})
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

    def log(self, lines, stream="out"):
        """Stream runner output lines to the server (live console / remote view)."""
        if not self.run_id or not lines:
            return
        if isinstance(lines, str):
            lines = [lines]
        self._req("POST", "/api/v1/runs/%s/log" % self.run_id,
                  {"stream": stream, "lines": [str(x) for x in lines]})

    def artifact(self, name, text, kind="json"):
        if not self.run_id:
            return
        import base64
        raw = text.encode("utf-8", "replace") if isinstance(text, str) else text
        self._req("POST", "/api/v1/runs/%s/artifact" % self.run_id,
                  {"name": name, "kind": kind, "content": base64.b64encode(raw).decode()})

    def finish(self, status="done", exit_code=0, summary=None, platform=None, gpu=None,
               driver=None, torch=None, ended=None, system=None):
        if not self.run_id:
            return
        self._req("POST", "/api/v1/runs/%s/finish" % self.run_id, {
            "status": status, "exit_code": exit_code, "summary": summary or {},
            "platform": platform, "gpu": gpu, "driver": driver, "torch": torch,
            "ended": ended, "system": system or {}})
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
        self.logs = queue.Queue()
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

    def push_log(self, line):
        try:
            self.logs.put_nowait(line)
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
        lines = []
        while len(lines) < 200:
            try:
                lines.append(self.logs.get_nowait())
            except Exception:
                break
        if lines:
            self.rep.log(lines)

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


def read_inventory(results_dir):
    """Full system inventory for a finished run.

    Linux: the report's `sys`/info phase result. Windows: the newest
    `*.sysinfo.json` artifact. Returns a normalised block plus the raw
    inventory under `inventory`, or None.
    """
    inv = None
    osname = None
    try:
        cands = [p for p in glob.glob(os.path.join(results_dir, "*-linux-*.json"))
                 if not p.endswith((".info.json", ".manifest.json"))]
        if cands:
            with open(max(cands, key=os.path.getmtime), encoding="utf-8-sig") as fh:
                j = json.load(fh)
            for ph in j.get("phases", []):
                if ph.get("group") == "sys" and isinstance(ph.get("result"), dict):
                    inv, osname = ph["result"], "Linux"
                    break
    except Exception:
        pass
    if inv is None:
        try:
            cands = glob.glob(os.path.join(results_dir, "*.sysinfo.json"))
            if cands:
                with open(max(cands, key=os.path.getmtime), encoding="utf-8-sig") as fh:
                    inv, osname = json.load(fh), "Windows"
        except Exception:
            pass
    if not isinstance(inv, dict):
        return None
    out = {"os": osname, "inventory": inv}
    if osname == "Linux":
        c = inv.get("cpu") or {}
        g = inv.get("gpu") or {}
        out.update({"platform": inv.get("distro"), "kernel": inv.get("kernel"),
                    "cpu": c.get("model"), "threads": c.get("logical_cpus"),
                    "ram_gib": inv.get("memory_gib"), "gpu": g.get("name"),
                    "driver": g.get("driver"), "vram": g.get("vram"),
                    "storage": inv.get("storage"), "network": inv.get("network_ifaces")})
    elif osname == "Windows":
        c = inv.get("CPU") or {}
        r = inv.get("RAM") or {}
        o = inv.get("OS") or {}
        out.update({"platform": o.get("caption"), "cpu": c.get("name"),
                    "cores": c.get("cores"), "threads": c.get("threads"),
                    "ram_gib": r.get("total_gib"), "manufacturer": inv.get("Manufacturer"),
                    "model": inv.get("Model"), "board": inv.get("Board"),
                    "bios": inv.get("BIOS")})
        nv = inv.get("nvidia")
        if isinstance(nv, dict):
            out["driver"] = nv.get("driver") or nv.get("DriverVersion") or nv.get("version")
        elif isinstance(nv, (list, str)):
            # Get-SystemInfo.ps1 stores the raw nvidia-smi line ("name, driver, ...")
            txt = (nv[0] if nv else "") if isinstance(nv, list) else nv
            parts = [p.strip() for p in str(txt or "").split(",")]
            if len(parts) > 1 and parts[1]:
                out.setdefault("gpu", parts[0])
                out["driver"] = parts[1]
        gpus = inv.get("GPUs")
        if isinstance(gpus, list) and gpus and isinstance(gpus[0], dict):
            out["gpu"] = gpus[0].get("name") or gpus[0].get("Name")
    return out


def run_exec(reporter, cmd, timeout=120, cwd=None):
    """Run a shell command, streaming its output to the server (remote terminal)."""
    import subprocess
    if reporter:
        reporter.log(["$ " + cmd], stream="exec")
    try:
        p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1, cwd=cwd,
                             creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                            if os.name == "nt" else 0))
    except Exception as e:
        if reporter:
            reporter.log(["exec error: %s" % e], stream="exec")
        return {"rc": -1, "error": str(e)}
    buf = []
    try:
        for ln in p.stdout:
            buf.append(ln.rstrip("\n"))
            if len(buf) >= 20:
                if reporter:
                    reporter.log(buf, stream="exec")
                buf = []
        rc = p.wait(timeout=timeout)
    except Exception as e:
        try:
            p.kill()
        except Exception:
            pass
        buf.append("exec aborted: %s" % e)
        rc = -9
    if buf and reporter:
        reporter.log(buf, stream="exec")
    return {"rc": rc}


def power_action(reboot=False):
    """Shut down / reboot this machine for the control plane's power commands.

    The caller acks *before* the host goes away, so the reply is best-effort.
    A short delay gives the ack and any log flush time to land.
    """
    if os.name == "nt":
        choices = [["shutdown", "/r" if reboot else "/s", "/t", "15",
                    "/c", "bench-test control plane"]]
    else:
        choices = [["systemctl", "reboot" if reboot else "poweroff"],
                   ["shutdown", "-r" if reboot else "-h", "now"]]
    for c in choices:
        try:
            subprocess.Popen(c, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"power": "reboot" if reboot else "shutdown", "cmd": " ".join(c)}
        except FileNotFoundError:
            continue
        except Exception as e:
            return {"power": "failed", "error": str(e)}
    return {"power": "failed", "error": "no power command found"}


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
        """Return named sample dicts (canonical field -> value) since the last call."""
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
            d = {}
            for canon, i in self.cols.items():
                if i >= len(vals):
                    continue
                v = _f(vals[i])
                if v is None and canon == "pstate":
                    v = _pstate(vals[i])
                if v is not None:
                    d[canon] = v
            if d:
                out.append(d)
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


def _throttle_active(thr):
    if not thr:
        return False
    if isinstance(thr, str):
        thr = [thr]
    try:
        for s in thr:
            v = str(s).split(":", 1)[-1].strip().lower()
            if v and v not in ("not active", "0 us", "0", "n/a", "not supported"):
                return True
    except Exception:
        return False
    return False


def _merge_into(s, j):
    """Merge one report dict into a runs-index-style summary (tolerant of shapes)."""
    def setd(k, v):
        if v is not None and k not in s:
            s[k] = v
    if not isinstance(j, dict):
        return
    if j.get("download_mbps") is not None and "net" not in s:
        s["net"] = {k: j.get(k) for k in ("download_mbps", "upload_mbps", "ping_target",
                                          "ping_target_ms", "ping_gateway_ms")}
    ph = j.get("phases")
    items = ph if isinstance(ph, list) else (list(ph.values()) if isinstance(ph, dict) else [])
    for p in items:
        if not isinstance(p, dict):
            continue
        r = p.get("result") if isinstance(p.get("result"), dict) else p
        phase = p.get("phase") or r.get("phase")
        grp = p.get("group") or r.get("group")
        if phase == "matmul" and isinstance(r.get("results"), dict):
            sub = r["results"]
            first = next(iter(sub.values()), {})
            if isinstance(first, dict) and "tflops" in first:
                setd("gpu_matmul_tflops", {k: v.get("tflops") for k, v in sub.items()})
            elif isinstance(first, dict) and "gflops" in first:
                setd("cpu_matmul_gflops", {k: v.get("gflops") for k, v in sub.items()})
        elif grp == "combined" and phase in ("stress", "soak", "combined"):
            setd("combined", r)                       # Linux shape
        elif grp == "fullsoak" and phase in ("soak", "stress", "fullsoak"):
            if isinstance(r.get("verdict"), dict):
                setd("fullsoak", {"group": grp, "phase": phase, "result": r})
        elif phase == "soak":
            if r.get("matmul_tflops") is not None:
                setd("gpu_soak_tflops", r["matmul_tflops"])
                setd("gpu_soak_telemetry", r.get("telemetry"))
            elif r.get("gflops") is not None:
                setd("cpu_soak_gflops", r["gflops"])
            elif r.get("gbps") is not None:
                setd("ram_soak_gbps", r["gbps"])
        elif phase == "seqwrite":
            setd("disk_seqwrite_mbps", r.get("mbps"))
        # -- results that were measured but previously dropped ------------------
        elif phase == "pcie" and r.get("h2d_gbps") is not None:
            setd("pcie", {"h2d_gbps": r.get("h2d_gbps"), "d2h_gbps": r.get("d2h_gbps")})
        elif phase == "membw" and isinstance(r.get("results"), dict):
            setd("gpu_membw", r["results"])
        elif phase == "bench":
            res = r.get("results")
            if isinstance(res, list):        # Windows NVENC: [{codec, fps, ...}]
                res = {str(x.get("codec")): {k: v for k, v in x.items() if k != "codec"}
                       for x in res if isinstance(x, dict) and x.get("codec")}
            if isinstance(res, dict) and any(isinstance(v, dict) and "fps" in v
                                             for v in res.values()):
                setd("nvenc", {"width": r.get("width"), "height": r.get("height"),
                               "results": res})
            elif r.get("download_mbps") is not None or r.get("ping_ms") is not None:
                setd("net", {k: r[k] for k in
                             ("download_mbps", "upload_mbps", "download_bytes", "target",
                              "ping_ms", "ping_avg_ms", "ping_mdev_ms", "ping_loss_pct",
                              "ping_target", "ping_target_ms", "ping_gateway_ms")
                             if r.get(k) is not None})
        elif grp == "disk" and phase in ("seqwrite", "seqread", "randread", "randwrite",
                                         "latency", "steadywrite"):
            d = {k: r[k] for k in ("mbps", "iops", "p50", "p90", "p99", "p999",
                                   "us_p50", "us_p90", "us_p99", "us_p999",
                                   "size_mb", "seconds") if r.get(k) is not None}
            for a, b in (("us_p50", "p50"), ("us_p90", "p90"),
                         ("us_p99", "p99"), ("us_p999", "p999")):
                if a in d and b not in d:
                    d[b] = d[a]
            s.setdefault("disk", {})[phase] = d
        elif grp == "ram" and phase == "bandwidth":
            s.setdefault("ram", {})["bandwidth"] = r.get("results")
        elif grp == "ram" and phase == "latency":
            s.setdefault("ram", {})["latency"] = {
                k: r[k] for k in ("ns_per_access_approx", "accesses") if r.get(k) is not None}
        elif grp == "gfx" and any(r.get(k) is not None
                                  for k in ("score", "fps", "avg_fps", "fps_avg")):
            g = {k: r[k] for k in ("score", "fps", "min_fps", "max_fps", "avg_fps",
                                   "fps_min", "fps_max", "fps_avg", "resolution", "preset")
                 if r.get(k) is not None}
            for a, b in (("fps_min", "min_fps"), ("fps_max", "max_fps"), ("fps_avg", "avg_fps")):
                if a in g and b not in g:
                    g[b] = g[a]
            setd("gfx", g)
    if isinstance(j.get("results"), list) and \
            any(isinstance(x, dict) and "tok_per_s" in x for x in j["results"]):
        setd("llm", {"results": j["results"]})
    if ("throttle" in j or "errors" in j) and isinstance(j.get("results"), dict) and "fullsoak" not in s:
        active = _throttle_active(j.get("throttle"))
        clean = (not j.get("errors")) and (not active)
        s["fullsoak"] = {"result": {"verdict": {"clean": bool(clean), "throttle_active": active}}}
    elif isinstance(j.get("verdict"), dict) and "fullsoak" not in s:
        s["fullsoak"] = {"result": {"verdict": j["verdict"]}}
    elif isinstance(j.get("gpu"), dict) and isinstance(j.get("cpu"), dict) and "combined" not in s:
        s["combined"] = {"gpu": j.get("gpu"), "cpu": j.get("cpu")}


def _hardware_verdicts(results_dir):
    """Pass/fail signals from the Windows health report and the Linux inventory."""
    v = []
    for p in glob.glob(os.path.join(results_dir, "*.health.json")):
        try:
            with open(p, encoding="utf-8-sig") as fh:
                h = json.load(fh)
        except Exception:
            continue
        summ = h.get("summary") or []
        whea = sum(int(s.get("count") or 0) for s in summ if "WHEA" in str(s.get("provider", "")))
        tdr = sum(int(s.get("count") or 0) for s in summ
                  if "nvlddmkm" in str(s.get("provider", "")) or str(s.get("id")) == "4101")
        kp = sum(int(s.get("count") or 0) for s in summ
                 if str(s.get("id")) == "41" and "Power" in str(s.get("provider", "")))
        bc = sum(int(s.get("count") or 0) for s in summ
                 if "BugCheck" in str(s.get("provider", "")) or str(s.get("id")) == "1001")
        stor = sum(int(s.get("count") or 0) for s in summ
                   if any(k in str(s.get("provider", "")).lower()
                          for k in ("disk", "storahci", "stornvme", "ntfs", "volmgr")))
        therm = sum(int(s.get("count") or 0) for s in summ
                    if "Thermal" in str(s.get("provider", "")))
        if kp:
            v.append({"level": "bad", "text": "unexpected shutdown (Kernel-Power 41): %d" % kp})
        if bc:
            v.append({"level": "bad", "text": "bugcheck / BSOD: %d" % bc})
        if stor:
            v.append({"level": "warn", "text": "storage events: %d" % stor})
        if therm:
            v.append({"level": "warn", "text": "thermal events: %d" % therm})
        if whea:
            v.append({"level": "bad", "text": "WHEA hardware errors: %d event(s)" % whea})
        if tdr:
            v.append({"level": "bad", "text": "display driver resets (TDR): %d" % tdr})
        if not whea and not tdr:
            v.append({"level": "good", "text": "no WHEA / TDR events in the window"})
        if not kp and not bc:
            v.append({"level": "good", "text": "no unexpected shutdowns or bugchecks"})
        for d in (h.get("disks") or []):
            if d.get("read_errors") or d.get("write_errors"):
                v.append({"level": "bad", "text": "disk %s I/O errors r/w=%s/%s"
                          % (d.get("name"), d.get("read_errors"), d.get("write_errors"))})
            try:
                if d.get("wear") is not None and float(d["wear"]) >= 80:
                    v.append({"level": "warn", "text": "disk %s wear %s%%" % (d.get("name"), d["wear"])})
            except (TypeError, ValueError):
                pass
    for p in glob.glob(os.path.join(results_dir, "*.info.json")) + \
             glob.glob(os.path.join(results_dir, "*.sysinfo.json")):
        try:
            with open(p, encoding="utf-8-sig") as fh:
                inv = json.load(fh)
        except Exception:
            continue
        inv = inv.get("inventory", inv) if isinstance(inv, dict) else {}
        # Windows inventory: predictive failure + the elevation gap
        for d in (inv.get("Disks") or []):
            if "Pred Fail" in str(d.get("wmi_status") or ""):
                v.append({"level": "bad", "text": "disk %s reports predictive failure" % d.get("name")})
            if str(d.get("health") or "").lower() not in ("healthy", "", "none"):
                v.append({"level": "warn", "text": "disk %s health: %s" % (d.get("name"), d.get("health"))})
        for sp in (inv.get("SmartPredict") or []):
            if sp.get("predict_failure"):
                v.append({"level": "bad", "text": "SMART predictive failure: %s" % sp.get("instance")})
        disks = inv.get("Disks") or []
        if disks and inv.get("Elevated") is False and \
                all(d.get("wear") is None and d.get("temp_c") is None for d in disks):
            v.append({"level": "info",
                      "text": "disk SMART not collected (battery not elevated)"})
        hw = inv.get("hardware_errors") or []
        if hw:
            v.append({"level": "warn", "text": "hardware error lines since boot: %d" % len(hw)})
        ecc = inv.get("ecc") or {}
        if ecc.get("ue"):
            v.append({"level": "bad", "text": "ECC uncorrectable errors: %d" % ecc["ue"]})
        elif ecc.get("ce"):
            v.append({"level": "warn", "text": "ECC correctable errors: %d" % ecc["ce"]})
        for dev, d in (inv.get("disk_health") or {}).items():
            me = d.get("media_errors")
            if isinstance(me, (int, float)) and me > 0:
                v.append({"level": "bad", "text": "%s media errors: %d" % (dev, me)})
            up = d.get("used_pct")
            if isinstance(up, (int, float)) and up >= 80:
                v.append({"level": "warn", "text": "%s wear %s%%" % (dev, up)})
    return v


def build_summary_dir(results_dir, window_h=8):
    """Merge all of a run's report JSONs (Windows main + per-group) into one row."""
    now = time.time()
    reports = []
    main = None
    main_mt = 0.0
    for p in glob.glob(os.path.join(results_dir, "*.json")):
        b = os.path.basename(p)
        if b.endswith((".info.json", ".sysinfo.json", ".sensors.json", ".pcie.json",
                       ".health.json", ".env.json", ".manifest.json")):
            continue
        mt = os.path.getmtime(p)
        if now - mt > window_h * 3600:
            continue
        try:
            with open(p, encoding="utf-8-sig") as fh:
                j = json.load(fh)
        except Exception:
            continue
        if not isinstance(j, dict):
            continue
        reports.append(j)
        if re.match(r"^.*-\d{8}-\d{6}\.json$", b) and isinstance(j.get("phases"), list) and mt > main_mt:
            main, main_mt = j, mt
    out = {}
    for j in reports:
        _merge_into(out, j)
    if main:
        out["host"] = main.get("host")
        out["timestamp"] = main.get("timestamp")
    verdicts = _hardware_verdicts(results_dir)
    if verdicts:
        out["verdicts"] = verdicts
    return out


def telemetry_summary(csv_path):
    """min/avg/max for key telemetry fields from a 1 Hz CSV."""
    try:
        with open(csv_path, encoding="utf-8-sig") as fh:
            header = fh.readline().strip().split(",")
            cols = {}
            for i, n in enumerate(header):
                c = CSV_ALIASES.get(n.strip().lower())
                if c in ("temp_c", "fan_pct", "power_w", "gpu_util", "mem_util"):
                    cols[c] = i
            acc = {c: [] for c in cols}
            n = 0
            for line in fh:
                v = line.rstrip("\n").split(",")
                if not v or v[0] == "":
                    continue
                n += 1
                for c, i in cols.items():
                    val = _f(v[i]) if i < len(v) else None
                    if val is not None:
                        acc[c].append(val)
    except OSError:
        return None
    out = {"samples": n}
    for c, vals in acc.items():
        if vals:
            out[c] = [min(vals), round(sum(vals) / len(vals), 1), max(vals)]
    return out


def read_runs_index(results_dir):
    """Return the last runs-index row in a results dir, if the runner wrote one."""
    path = os.path.join(results_dir, "runs-index.jsonl")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
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
