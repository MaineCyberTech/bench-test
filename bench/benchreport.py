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
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid

GROUPS = ["gpu", "cpu", "ram", "disk", "combined", "fullsoak", "net", "nvenc",
          "llm", "fan", "sys", "gfx"]


def supported_groups():
    """The groups this platform's battery actually implements.

    `fan` is Linux-only and `llm` is Windows-only; runners advertise this so the
    dashboards can disable a toggle that would be a silent no-op.
    """
    if os.name == "nt":
        return [g for g in GROUPS if g != "fan"]
    return [g for g in GROUPS if g != "llm"]

SAMPLE_FIELDS = ["elapsed", "temp_c", "fan_pct", "power_w", "gpu_util",
                 "gpu_fan_rpm", "cpu_util", "ram_used", "sm_mhz"]

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
    # extras streamed to the server for richer charts
    "mem_util_pct": "mem_util", "mem_util": "mem_util",
    "mem_mhz": "mem_mhz",
    "vram_used_mib": "vram_used_mib",
    "vram_total_mib": "vram_total_mib", "vram_total": "vram_total_mib",
    "gpu_mem_total_mib": "vram_total_mib",
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


def load_conf(here=None):
    """Resolve (server, token) from the environment or a bench-agent.conf.

    Identical search order on Windows and Linux, so the GUI, TUI and agent all
    check in the same way on every platform. Explicit config wins over the copy
    baked into the packaged app (like the live USB), so a machine can point at a
    different control plane without rebuilding:
        $BENCH_AGENT_CONF, ./bench-agent.conf, ~/.config/bench-agent.conf,
        /etc/bench-agent.conf, /mnt/benchdata/bench-agent.conf,
        <toolkit>/bench-agent.conf (baked app default), <bench>/bench-agent.conf
    `$BENCHD_SERVER` / `$BENCHD_TOKEN` in the environment override everything.
    `here` (the bench/ dir) is a test seam; callers use the default.
    """
    here = here or os.path.dirname(os.path.abspath(__file__))  # .../bench
    repo = os.path.dirname(here)                               # toolkit root / bundle dir
    cands = [os.environ.get("BENCH_AGENT_CONF"),
             os.path.join(os.getcwd(), "bench-agent.conf"),
             os.path.expanduser("~/.config/bench-agent.conf"),
             "/etc/bench-agent.conf",
             "/mnt/benchdata/bench-agent.conf",
             os.path.join(repo, "bench-agent.conf"),           # baked default (app bundle)
             os.path.join(here, "bench-agent.conf")]           # baked beside the phases
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


# Telemetry the dashboards surface. One list, so the GUI and TUI cannot drift:
# (canonical field, label, decimals) — "-" decimals means the raw value (text,
# e.g. pstate "P8"). Keep in step with the sampler columns (docs/DATA.md).
DASH_FIELDS = [
    ("temp_c", "GPU TEMP", 0), ("gpu_util", "GPU %", 0), ("mem_util", "VRAM %", 0),
    ("vram_used_mib", "VRAM MiB", 0), ("sm_mhz", "SM MHz", 0), ("mem_mhz", "MEM MHz", 0),
    ("power_w", "POWER W", 0), ("power_limit_w", "CAP W", 0), ("fan_pct", "FAN %", 0),
    ("gpu_fan_rpm", "GPU RPM", 0), ("pstate", "PSTATE", "-"), ("gpu_enc_pct", "ENC %", 0),
    ("gpu_dec_pct", "DEC %", 0), ("temp_mem_c", "VRAM °C", 0), ("temp_limit_c", "GPU LIMIT", 0),
    ("throttle", "THROTTLE", 0), ("video_mhz", "NVENC MHz", 0), ("pcie_gen", "PCIE GEN", 0),
    ("pcie_width", "PCIE WIDTH", 0), ("retired", "RETIRED", 0),
    ("cpu_util", "CPU %", 0), ("cpu_temp_c", "CPU °C", 0), ("cpu_max_core_c", "CORE MAX °C", 0),
    ("cpu_fan_rpm", "CPU FAN", 0), ("cpu_pkg_w", "CPU W", 1), ("cpu_mhz", "CPU MHz", 0),
    ("nvme_c", "NVMe °C", 0), ("ram_used", "RAM GiB", 1), ("load1", "LOAD1", 2),
    ("psi_io", "IO STALL", 1), ("disk_read_mbs", "DISK R", 1), ("disk_write_mbs", "DISK W", 1),
    ("net_rx_mbs", "NET RX", 1), ("net_tx_mbs", "NET TX", 1),
]


def hardware_line(info=None):
    """One-line hardware summary for the dashboards (cross-platform).

    Never renders a placeholder like "None GiB": unknown fields are left out.
    """
    si = info if isinstance(info, dict) else (system_info() or {})
    bits = []
    cpu = re.sub(r"\((R|TM)\)", "", str(si.get("cpu") or "")).strip()
    cpu = re.sub(r"\s+CPU\s*@.*$", "", cpu, flags=re.I).strip()
    if cpu:
        cores, thr = si.get("cores"), si.get("threads")
        if cores and thr:
            cpu += " (%sc/%st)" % (cores, thr)
        elif thr:
            cpu += " (%st)" % thr
        elif cores:
            cpu += " (%sc)" % cores
        bits.append(cpu)
    if si.get("ram_gib"):
        bits.append("%s GiB" % si["ram_gib"])
    if si.get("gpu"):
        g = str(si["gpu"])
        bits.append(g + ((" · driver %s" % si["driver"]) if si.get("driver") else ""))
    return " · ".join(bits)


def _total_ram_gib():
    """Installed RAM in GiB, without requiring psutil."""
    try:
        import psutil
        return round(psutil.virtual_memory().total / 1024 ** 3, 1)
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes

            class _MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = _MS()
            st.dwLength = ctypes.sizeof(_MS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return round(st.ullTotalPhys / 1024 ** 3, 1)
        except Exception:
            pass
    try:
        with open("/proc/meminfo") as fh:
            for ln in fh:
                if ln.startswith("MemTotal"):
                    return round(int(ln.split()[1]) / 1048576.0, 1)
    except Exception:
        pass
    try:
        return round(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1024 ** 3, 1)
    except Exception:
        return None


def system_info():
    """Best-effort host spec block for run history (cpu, ram, gpu, driver, platform)."""
    import platform as _p
    info = {"os": _p.system(), "platform": _p.platform(), "hostname": socket.gethostname(),
            "python": _p.python_version()}
    info["cpu"] = _cpu_name()
    ram = _total_ram_gib()
    if ram:
        info["ram_gib"] = ram
    cpus = os.cpu_count()
    if cpus:
        info["threads"] = cpus
    try:
        import psutil
        phys = psutil.cpu_count(logical=False)
        if phys:
            info["cores"] = phys
    except Exception:
        pass
    try:
        out = run_hidden(["nvidia-smi", "--query-gpu=name,driver_version,memory.total",
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
    if not info.get("gpu") and os.name == "nt":
        # AMD/Intel on Windows have no nvidia-smi, so without this the runner
        # registers with no GPU at all and the fleet view shows a blank card.
        # WMI is the same source Get-SystemInfo.ps1 uses; skip the Microsoft
        # Basic Display Adapter unless it is the only thing present.
        try:
            ps = ("Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue | "
                  "ForEach-Object { $_.Name + '|' + $_.DriverVersion + '|' + $_.AdapterRAM }")
            out = run_hidden(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
            rows = [ln.split("|") for ln in out.splitlines() if "|" in ln]
            pick = next((r for r in rows if r and "Basic Display" not in r[0]), None)
            pick = pick or (rows[0] if rows else None)
            if pick and pick[0].strip():
                info["gpu"] = pick[0].strip()
                if len(pick) > 1 and pick[1].strip():
                    info["driver"] = pick[1].strip()
                try:
                    b = int(pick[2])
                    if b > 0:
                        info["vram"] = "%.0f MiB" % (b / 1048576.0)
                except (IndexError, ValueError):
                    pass
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


# a line that means the phase did not actually produce its result - a phase that
# dies must not be reported as 'done' (that is how a dead GPU phase stayed
# invisible: 'ModuleNotFoundError: No module named torch' inside a 'done' phase)
FATAL_MARKERS = ("Traceback (most recent call last):", "ModuleNotFoundError:",
                 "ImportError:", "SyntaxError:", "CUDA out of memory",
                 "no CUDA-capable device", '"ok": false', '"ok":false')


class PhaseTracker:
    """Turns '[group] label' runner lines into open/close phase events.

    Mirrors the dashboards: a new phase closes *every* still-open phase, so a
    group's last row is closed when the run moves to the next group.
    """

    def __init__(self, reporter=None):
        self.reporter = reporter
        self.seq = 0
        self.open = []  # list of {seq, group, label}
        self.failed = False

    def _fatal(self, line):
        return any(m in line for m in FATAL_MARKERS)

    def feed(self, line):
        if self._fatal(line):
            self.failed = True
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
        status = "failed" if self.failed else "done"
        for ph in self.open:
            if self.reporter:
                self.reporter.phase(ph["seq"], ph["group"], ph["label"], status, t1=time.time())
        self.open = []
        self.failed = False

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
    tail = []           # bounded copy for the result: a runner with no active run has
                        # nowhere to stream to, so the ack has to carry the output or a
                        # remote diagnostic comes back as "rc 0" and nothing else
    try:
        for ln in p.stdout:
            ln = ln.rstrip("\n")
            buf.append(ln)
            tail.append(ln[:400])
            if len(tail) > 200:
                del tail[:-200]
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
        msg = "exec aborted: %s" % e
        buf.append(msg)
        tail.append(msg)
        rc = -9
    if buf and reporter:
        reporter.log(buf, stream="exec")
    return {"rc": rc, "out": "\n".join(tail)}


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
            popen_hidden(c, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"power": "reboot" if reboot else "shutdown", "cmd": " ".join(c)}
        except FileNotFoundError:
            continue
        except Exception as e:
            return {"power": "failed", "error": str(e)}
    return {"power": "failed", "error": "no power command found"}


# ------------------------------------------------------- remote update ------
# Live-USB sticks boot a read-only ISO: the toolkit under /opt/bench-test comes
# from the image and the root is a tmpfs overlay, so pointing /opt/bench-test at
# a fresher copy on the stick's writable partition updates the runner without
# reflashing. Kernel/driver changes still need a new ISO, which we can download,
# verify and (optionally) flash to another device from the control plane.

REPO_SLUG = os.environ.get("BENCH_REPO_SLUG") or "MaineCyberTech/bench-test"
TOOLKIT_DIR = os.environ.get("BENCH_TOOLKIT_DIR") or "/opt/bench-test"
PERSIST_DIR = os.environ.get("BENCH_PERSIST_DIR") or "/mnt/benchdata"
GH_API = os.environ.get("BENCH_GH_API") or "https://api.github.com"
UA = "bench-test-updater"


# The dashboards run under pythonw (no console), so any console child - git,
# nvidia-smi, shutdown - would flash a window of its own; the startup update ran
# git status/fetch/rev-list/pull and produced a burst of them.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def run_hidden(cmd, **kw):
    """subprocess.run that never opens a console window on Windows."""
    if os.name == "nt":
        kw.setdefault("creationflags", NO_WINDOW)
    return subprocess.run(cmd, **kw)


def popen_hidden(cmd, **kw):
    """subprocess.Popen that never opens a console window on Windows."""
    if os.name == "nt":
        kw.setdefault("creationflags", NO_WINDOW)
    return subprocess.Popen(cmd, **kw)


# preset/payload knobs -> runner flags. The runners already accept all of these;
# they were simply unreachable from a preset, so runs used the runner's long
# defaults (cpu 120s, ram 120s, gpu size 12288) - which is why "Quick check" took
# over half an hour.
EXTRA_ARG_KEYS = [("cpu_soak", "--cpu-soak"), ("ram_soak", "--ram-soak"),
                  ("disk_seconds", "--disk-seconds"), ("disk_size_mb", "--disk-size-mb"),
                  ("gpu_size", "--gpu-size"),
                  ("cpu_size", "--cpu-size"), ("ram_size_mb", "--ram-size-mb"),
                  ("passes", "--passes"), ("steady_gb", "--steady-gb"),
                  ("stable_hold", "--stable-hold"), ("gfx_engine", "--gfx-engine")]


def extra_runner_args(profile):
    """Tuning flags a preset may set beyond gpu/combined/fullsoak/gfx.

    `stable` turns on the plateau soak; `stable_hold` is how many seconds to keep
    soaking *after* the plateau is reached, and implies `stable` (the runners need
    the switch as well as the hold).
    """
    p = profile or {}
    args = []
    for key, flag in EXTRA_ARG_KEYS:
        v = p.get(key)
        if v not in (None, ""):
            args += [flag, str(v)]
    if p.get("stable") or p.get("stable_hold") not in (None, ""):
        args.append("--soak-until-stable")
    return args


def tune_summary(profile):
    """One short human line for the extra tuning knobs (empty when none are set)."""
    t = profile or {}
    parts = []
    for key, fmt in (("cpu_soak", "cpu %ss"), ("ram_soak", "ram %ss"),
                     ("disk_seconds", "disk %ss"), ("disk_size_mb", "disk %sMB"),
                     ("gpu_size", "gpu %s"),
                     ("cpu_size", "cpuN %s"), ("ram_size_mb", "ramN %sMB"),
                     ("passes", "%s pass"), ("steady_gb", "steady %sG")):
        if t.get(key) is not None:
            parts.append(fmt % t[key])
    if t.get("stable") or t.get("stable_hold") not in (None, ""):
        hold = t.get("stable_hold")
        parts.append("until stable" + ((" +%ss" % hold) if hold not in (None, "") else ""))
    if t.get("gfx_engine"):
        parts.append("gfx %s" % t["gfx_engine"])
    return " · ".join(parts)


def is_live():
    """True on a booted live image (archiso) rather than a normal install."""
    return os.path.isdir("/run/archiso") or os.path.exists("/etc/arch-release")


VERSION_FILE = ".bench-version"   # generated stamp of a fetched copy (JSON)
SEMVER_FILE = "VERSION"           # the release version, e.g. 1.1.0
# Wire format between runners and the control plane. Bump it when a payload or a
# summary key changes shape, so an out-of-step runner is *detected* instead of
# quietly producing wrong data.
PROTOCOL = 1
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$")


def git_describe(root=None):
    """``(release, commits_since_tag, commit)`` from ``git describe``.

    Returns ``("", 0, "")`` when this is not a checkout or there is no tag yet.
    """
    repo = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        # --match: the release workflows also tag `app-<n>` and `iso-<gpu>-<n>`,
        # which are newer than the version tag and would otherwise be picked as
        # "the" tag (git describe takes the most recent one reachable from HEAD)
        r = run_hidden(["git", "-C", repo, "describe", "--tags", "--long", "--dirty",
                        "--match", "v[0-9]*"],
                       capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return ("", 0, "")
        m = re.match(r"^v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?)-(\d+)-g([0-9a-f]+)",
                     r.stdout.strip())
        if not m:
            return ("", 0, "")
        return (m.group(1), int(m.group(2)), m.group(3))
    except Exception:
        return ("", 0, "")


def bench_version(root=None):
    """The **release** version - bare semver, from the git tag.

    Exactly the tag you are on (`1.1.0`). How far past it you are is build
    information, not release information, so it lives in ``bench_build()`` and
    ``version_string()``. Only a copy with no git (a live stick) falls back to the
    VERSION file it was fetched with, which the release workflow keeps equal to the
    latest tag. Validated rather than trusted: a malformed value becomes
    ``0.0.0+invalid`` instead of being passed around as a release.
    """
    root = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rel, _n, _c = git_describe(root)
    if rel:
        return rel
    try:
        with open(os.path.join(root, SEMVER_FILE), encoding="utf-8") as fh:
            v = fh.read().strip().splitlines()[0].strip()
    except (OSError, IndexError):
        # No git and no VERSION file: an app bundle carries its release in the
        # .bench-version stamp (packaging/build_app.py bakes it). Read it here
        # rather than via toolkit_version(), which calls back into this function.
        try:
            with open(os.path.join(root, VERSION_FILE), encoding="utf-8") as fh:
                v = str(json.load(fh).get("version") or "").strip()
        except Exception:
            v = ""
        if v.endswith(("+unknown", "+invalid")):
            v = ""
    if not v:
        return "0.0.0+unknown"
    return v if _SEMVER_RE.match(v) else "0.0.0+invalid"


def bench_build(root=None):
    """The build identity within the release: ``468.d089bab5`` or just the commit.

    ``<commits-since-tag>.<commit>`` makes two builds of the same release
    distinguishable and ordered; a copy falls back to the commit (or content hash)
    recorded in its fetch stamp.
    """
    root = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rel, n, c = git_describe(root)
    if rel and c:
        return ("%d.%s" % (n, c)) if n else c
    tv = toolkit_version(root)
    return tv.get("build") or tv.get("commit") or ""


def version_string(root=None):
    """``1.1.0+468.d089bab5`` - release plus the exact build.

    Every commit produces a distinct, comparable version, so a release that ships
    many commits can never report one identical version for all of them.
    """
    v = bench_version(root)
    b = bench_build(root)
    return ("%s+%s" % (v, b)) if b else v


def version_info(root=None):
    """Everything needed to say exactly what is running, from one place.

    Every surface (GUI/TUI headers, runner registration, web, /health) should read
    this rather than re-deriving a version, so they cannot disagree.
    """
    tv = toolkit_version(root)
    rel, n, _c = git_describe(root)
    return {"version": version_string(root),
            "release": bench_version(root),
            "build": bench_build(root),
            "commit": tv.get("commit") or git_commit(root),
            "mode": tv.get("mode"),
            "ref": tv.get("ref"),
            "commits_since_tag": n,
            "protocol": PROTOCOL}


def git_commit(repo=None):
    """Short HEAD of a git checkout ('' when it isn't one)."""
    repo = repo or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        r = run_hidden(["git", "-C", repo, "rev-parse", "--short", "HEAD"],
                       capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def toolkit_version(root=None):
    """Identify the running toolkit so the fleet view can spot stale runners."""
    root = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ver = bench_version(root)
    if os.path.isdir(os.path.join(root, ".git")):
        return {"mode": "git", "commit": git_commit(root), "ref": None,
                "sha256": None, "fetched_at": None, "version": ver,
                "build": git_commit(root), "protocol": PROTOCOL}
    try:
        with open(os.path.join(root, VERSION_FILE), encoding="utf-8") as fh:
            v = json.load(fh)
        if isinstance(v, dict):
            v.setdefault("mode", "copy")
            v["version"] = ver
            v["protocol"] = PROTOCOL
            # resolving the commit needs the GitHub API, which can be rate limited;
            # the content hash still tells two copies apart, so use it as the build
            v["build"] = v.get("commit") or (v.get("sha256") or "")[:7] or None
            return v
    except Exception:
        pass
    return {"mode": "copy", "commit": None, "ref": None, "sha256": None,
            "fetched_at": None, "version": ver, "build": None, "protocol": PROTOCOL}


def _resolve_commit(ref):
    """Best-effort ref -> short commit (needs network; '' when unknown).

    Cache-busted on purpose: GitHub (or its CDN) will serve a branch tip that is
    a minute or two old, which stamps a freshly fetched copy with a commit it is
    already past - and the fleet view then shows a false "update available".
    """
    s = str(ref or "")
    if re.match(r"^[0-9a-f]{7,40}$", s):
        return s[:7]
    try:
        url = "%s/repos/%s/commits/%s?bench=%d" % (GH_API, REPO_SLUG, s,
                                                   int(time.time() * 1000))
        req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                   "Accept": "application/vnd.github+json",
                                                   "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return (json.loads(r.read().decode()).get("sha") or "")[:7]
    except Exception:
        return ""


def persist_root():
    """A writable place that survives the session if possible (the stick)."""
    for d in (PERSIST_DIR, "/var/tmp", tempfile.gettempdir()):
        if d and os.path.isdir(d) and os.access(d, os.W_OK):
            return d
    return tempfile.gettempdir()


def _download(url, dest, log=None, timeout=180, every=100 << 20):
    """Stream a URL to a path or an open binary file object."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    n = 0
    close = False
    fh = dest
    if isinstance(dest, str):
        fh = open(dest, "wb")
        close = True
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                fh.write(b)
                n += len(b)
                if log and n % every < (1 << 20):
                    log("[update] %d MiB" % (n >> 20))
    finally:
        if close:
            fh.close()
    return n


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _safe_extract(tar_path, dest):
    """Extract a tarball, stripping the top dir, ignoring links/escapes."""
    root = os.path.realpath(dest)
    with tarfile.open(tar_path, "r:gz") as tf:
        for m in tf.getmembers():
            rel = m.name.split("/", 1)[1] if "/" in m.name else ""
            if not rel:
                continue
            target = os.path.realpath(os.path.join(dest, rel))
            if target != root and not target.startswith(root + os.sep):
                continue
            if m.isdir():
                os.makedirs(target, exist_ok=True)
            elif m.isfile():
                os.makedirs(os.path.dirname(target), exist_ok=True)
                src = tf.extractfile(m)
                if src is None:
                    continue
                with open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                try:
                    os.chmod(target, m.mode & 0o777)
                except OSError:
                    pass


def repoint_toolkit(toolkit_dir, target, force=False):
    """Point the toolkit directory at `target`.

    Only safe where the toolkit dir is disposable (the live overlay); elsewhere
    the operator must pass force / set BENCH_ALLOW_TOOLKIT_SWITCH=1.
    """
    allow = force or os.environ.get("BENCH_ALLOW_TOOLKIT_SWITCH") == "1" or is_live()
    if os.path.islink(toolkit_dir):
        if os.path.realpath(toolkit_dir) == os.path.realpath(target):
            return True
        if not allow:
            return False
        os.remove(toolkit_dir)
    elif os.path.isdir(toolkit_dir):
        if not allow:
            return False
        shutil.rmtree(toolkit_dir, ignore_errors=True)
    try:
        os.makedirs(os.path.dirname(toolkit_dir.rstrip("/")), exist_ok=True)
        os.symlink(target, toolkit_dir)
    except OSError:
        return False
    return True


def _app_bundle_root(root):
    """True when `root` is a PyInstaller app bundle rather than a checkout.

    A bundle ships the toolkit next to its exe and stamps itself `.bench-version`
    with mode 'app' (packaging/build_app.py). It is neither a live runner nor a git
    checkout, so the "use git pull instead" guard does not apply to it.
    """
    if not root or os.path.isdir(os.path.join(root, ".git")):
        return False
    try:
        with open(os.path.join(root, VERSION_FILE), encoding="utf-8") as fh:
            return json.load(fh).get("mode") == "app"
    except Exception:
        return False


def _read_version_stamp(root):
    """The `.bench-version` stamp as a dict ({} when absent or unreadable)."""
    try:
        with open(os.path.join(root, VERSION_FILE), encoding="utf-8") as fh:
            v = json.load(fh)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _touch_version_stamp(root, **fields):
    """Merge fields into the `.bench-version` stamp, keeping what is already there.

    Replacing the whole stamp would drop the app's own bookkeeping (e.g. when the
    startup update check last ran).
    """
    stamp = _read_version_stamp(root)
    stamp.update(fields)
    try:
        with open(os.path.join(root, VERSION_FILE), "w", encoding="utf-8") as fh:
            json.dump(stamp, fh, indent=1)
    except OSError:
        pass
    return stamp


def _code_runs_from_disk(root):
    """True when this app's own modules live in `root/bench` (the launcher shape).

    A bundle built before the launcher froze the dashboard into the exe, so a
    toolkit refresh there only updates the files that get *executed* - the note must
    not promise a dashboard update that cannot happen.
    """
    try:
        return os.path.abspath(os.path.join(root, "bench")) == \
            os.path.dirname(os.path.abspath(__file__))
    except Exception:
        return False


def _install_into_bundle(bundle, src, ref, commit, sha256, nbytes, log=None):
    """Refresh the toolkit copy that ships next to a bundled dashboard's exe.

    Only the toolkit can be replaced in place - the frozen dashboard keeps running
    its own bundled code - so the result says exactly that instead of implying the
    whole app was updated.
    """
    log = log or (lambda _m: None)
    dst = os.path.join(bundle, "bench")
    try:
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        shutil.copytree(os.path.join(src, "bench"), dst)
    except OSError as e:
        return {"error": "could not refresh the bundled toolkit: %s" % e, "root": bundle}
    ver = ""
    try:
        with open(os.path.join(src, SEMVER_FILE), encoding="utf-8") as fh:
            ver = fh.read().strip().splitlines()[0].strip()
    except (OSError, IndexError):
        ver = ""
    if ver:
        try:
            with open(os.path.join(bundle, SEMVER_FILE), "w", encoding="utf-8") as fh:
                fh.write(ver + "\n")
        except OSError:
            pass
    # merge, so the stamp keeps any bookkeeping the app already had (mode stays
    # 'app' so the bundle keeps being recognised as one)
    _touch_version_stamp(bundle, mode="app", ref=ref, commit=commit, sha256=sha256,
                         updated_at=time.time(), version=ver or None)
    log("[update] refreshed the bundled toolkit in %s" % dst)
    if _code_runs_from_disk(bundle):
        note = ("the app runs from this toolkit, so the dashboard and the runners "
                "were both updated - restarting applies it")
    else:
        note = ("this bundle froze its dashboard into the exe, so only the runners "
                "and phases were updated; download a new app bundle for the UI")
    return {"ok": True, "kind": "app", "updated": True, "ref": ref, "bytes": nbytes,
            "sha256": sha256, "dest": dst, "commit": commit, "version": ver or None,
            "restart_required": True, "note": note}


def update_toolkit(ref="main", sha256=None, log=None, root=None):
    """Fetch the toolkit at `ref` onto the stick and switch the runner onto it.

    Only meaningful for a live/external install: `root` is the toolkit in use
    (a runner passes its own REPO). A git checkout updates with `update` instead.
    """
    log = log or (lambda _m: None)
    toolkit_dir = root or TOOLKIT_DIR
    if os.path.isdir(os.path.join(toolkit_dir, ".git")) and not os.path.islink(toolkit_dir):
        return {"kind": "git", "error": "a git checkout updates with the 'update' command"}
    if root and os.path.isdir(root) and not is_live() and \
            not _app_bundle_root(root) and \
            os.environ.get("BENCH_ALLOW_TOOLKIT_SWITCH") != "1":
        return {"kind": "install", "toolkit": toolkit_dir,
                "error": "not a live runner - update the checkout with 'update' (git pull) "
                         "or set BENCH_ALLOW_TOOLKIT_SWITCH=1"}

    root = os.path.join(persist_root(), "bench-toolkit")
    dest = os.path.join(root, str(ref).replace("/", "_"))
    tmp = dest + ".part"
    tar_path = os.path.join(tempfile.gettempdir(), "bench-toolkit-%s.tar.gz" % uuid.uuid4().hex[:8])
    url = os.environ.get("BENCH_TOOLKIT_URL") or \
        "https://codeload.github.com/%s/tar.gz/%s" % (REPO_SLUG, ref)
    log("[update] fetching %s" % url)
    try:
        n = _download(url, tar_path, log=log)
    except Exception as e:
        return {"error": "download failed: %s" % e, "url": url}
    got = sha256_file(tar_path)
    if sha256 and got.lower() != str(sha256).lower():
        os.remove(tar_path)
        return {"error": "sha256 mismatch", "expected": sha256, "got": got}
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    try:
        _safe_extract(tar_path, tmp)
    except Exception as e:
        return {"error": "extract failed: %s" % e}
    finally:
        try:
            os.remove(tar_path)
        except OSError:
            pass
    if not os.path.isfile(os.path.join(tmp, "bench", "benchreport.py")):
        return {"error": "archive did not contain the toolkit"}
    commit = _resolve_commit(ref)
    ver = ""
    try:
        with open(os.path.join(tmp, SEMVER_FILE), encoding="utf-8") as fh:
            ver = fh.read().strip().splitlines()[0].strip()
    except (OSError, IndexError):
        ver = ""
    if _app_bundle_root(toolkit_dir):
        # a bundle refreshes the toolkit it ships next to its exe, in place
        res = _install_into_bundle(toolkit_dir, tmp, ref, commit, got, n, log)
        shutil.rmtree(tmp, ignore_errors=True)
        return res
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(tmp, dest)
    try:
        with open(os.path.join(dest, VERSION_FILE), "w", encoding="utf-8") as fh:
            json.dump({"mode": "copy", "ref": ref, "commit": commit, "sha256": got,
                       "fetched_at": time.time(), "version": ver or None}, fh, indent=1)
    except OSError:
        pass
    os.makedirs(root, exist_ok=True)
    cur = os.path.join(root, "current")
    try:
        if os.path.islink(cur):
            os.remove(cur)
        os.symlink(dest, cur)
    except OSError:
        # BENCHDATA is vfat and cannot hold symlinks; leave a plain pointer so the
        # boot-time re-point (toolkit_boot_link) still finds this copy.
        cur = dest
        try:
            with open(os.path.join(root, "current.txt"), "w", encoding="utf-8") as fh:
                fh.write(dest)
        except OSError:
            pass
    # keep the image's PyTorch reachable: re-pointing the toolkit hides the baked
    # copy, and every GPU phase dies with ModuleNotFoundError without it
    ensure_torch_packages(log)
    switched = repoint_toolkit(toolkit_dir, dest)
    log("[update] %s -> %s (switched=%s)" % (ref, dest, switched))
    return {"ok": True, "ref": ref, "bytes": n, "sha256": got, "dest": dest,
            "current": cur, "switched": switched, "toolkit": toolkit_dir,
            "commit": commit, "restart_required": bool(switched)}


def update_needs_restart(res):
    """True when an update should be followed by a restart.

    A git checkout restarts even when the pull was a no-op: the running process
    reports the commit it started with, so a fresh registration is what clears
    the "update available" badge.
    """
    if not isinstance(res, dict) or not res.get("ok"):
        return False
    if res.get("kind") == "git":
        return True
    # an app bundle refreshed its shipped toolkit in place, so it must re-register
    # (or it keeps reporting the version it booted with)
    if res.get("kind") == "app" or res.get("restart_required"):
        return True
    return bool(res.get("switched"))


def should_restart_after_update(res, running=False, requested=False):
    """Whether a dashboard should restart itself after applying an update.

    A plain update has to be followed by a restart or the runner keeps reporting
    the commit it started with and the fleet badge keeps saying "update
    available". A run in progress wins: defer it (the caller can pass
    requested=True to force the restart).
    """
    return update_needs_restart(res) and (bool(requested) or not running)


def auto_update(repo=None, log=None, enabled=None):
    """Pull the checkout if it is behind upstream — intended to run at startup.

    Refuses when: not a git checkout (e.g. the live USB, which updates via the
    toolkit copy + boot link), BENCH_NO_UPDATE=1, BENCH_OFFLINE=1, the tree is
    dirty (local edits win), or git fails. Returns a dict describing the outcome.
    """
    log = log or (lambda _m: None)
    repo = repo or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if enabled is None:
        enabled = os.environ.get("BENCH_NO_UPDATE") != "1"
    if not enabled:
        return {"kind": "none", "ok": True, "updated": False}
    if not os.path.isdir(os.path.join(repo, ".git")):
        # An app bundle ships the toolkit next to its exe and (since the launcher)
        # RUNS the app code from it, so refreshing bench/ updates the dashboard and
        # the runners together. Throttled, because a launch must not always be a
        # download; BENCH_APP_UPDATE_HOURS=0 checks on every launch.
        if not _app_bundle_root(repo):
            return {"kind": "none", "ok": True, "updated": False}
        if os.environ.get("BENCH_OFFLINE") == "1":
            return {"kind": "app", "ok": True, "updated": False, "skipped": "offline"}
        try:
            hours = float(os.environ.get("BENCH_APP_UPDATE_HOURS") or 6)
        except ValueError:
            hours = 6.0
        last = float(_read_version_stamp(repo).get("checked_at") or 0)
        age = time.time() - last
        if last and hours > 0 and age < hours * 3600:
            return {"kind": "app", "ok": True, "updated": False,
                    "skipped": "checked %.1fh ago" % (age / 3600.0)}
        log("[update] checking the app's toolkit (%s)" % (os.environ.get("BENCH_APP_REF") or "main"))
        res = update_toolkit(os.environ.get("BENCH_APP_REF") or "main", log=log, root=repo)
        # record the attempt either way, or a failing download retries every launch
        _touch_version_stamp(repo, checked_at=time.time(),
                             last_update_ok=bool(res.get("ok")),
                             last_update_error=res.get("error"))
        return res
    if os.environ.get("BENCH_OFFLINE") == "1":
        return {"kind": "git", "ok": True, "updated": False, "skipped": "offline"}

    def git(*args, timeout=60):
        return run_hidden(["git", "-C", repo] + list(args), capture_output=True,
                          text=True, timeout=timeout)

    try:
        dirty = git("status", "--porcelain").stdout.strip()
        if dirty:
            log("[update] local changes present - skipping auto-update")
            return {"kind": "git", "ok": True, "updated": False, "skipped": "dirty"}
        git("fetch", "--quiet", timeout=180)
        behind = git("rev-list", "--count", "HEAD..@{u}").stdout.strip()
        if not behind.isdigit() or behind == "0":
            return {"kind": "git", "ok": True, "updated": False, "behind": 0}
        log("[update] %s commit(s) behind upstream - pulling" % behind)
        r = git("pull", "--ff-only", timeout=180)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        updated = r.returncode == 0 and "Already up to date" not in out
        log("[update] pull rc=%s updated=%s" % (r.returncode, updated))
        return {"kind": "git", "ok": r.returncode == 0, "updated": updated,
                "behind": int(behind), "out": out[-400:]}
    except Exception as e:
        log("[update] auto-update failed: %s" % e)
        return {"kind": "git", "ok": False, "updated": False, "error": str(e)}


def drop_stray_script_arg(argv=None, me=None):
    """Drop this app's own path when an older `restart_self` re-passed it.

    `restart_self` used to spawn `[sys.executable] + sys.argv`, and `sys.argv[0]` is
    the *script* - so a launcher bundle's successor received its own path as a
    positional argument, argparse rejected it, and the app exited immediately: a
    remote update closed the TUI and nothing came back.

    Only a path that is exactly this file is removed, so a real argument can never
    be eaten (the agent takes a REMAINDER positional, for instance). The launcher
    drops it too, for a successor started before this fix.
    """
    argv = list(sys.argv if argv is None else argv)
    try:
        here = os.path.abspath(me or __file__)
    except Exception:
        return argv
    if (len(argv) > 1 and argv[1].lower().endswith(".py") and os.path.isfile(argv[1])
            and os.path.abspath(argv[1]) == here):
        return [argv[0]] + argv[2:]
    return argv


def restart_self(delay=1.5, log=None, console=False):
    """Re-exec this runner so it picks up a freshly switched toolkit (live USB).

    Windows has no real execv: spawn a successor with the same argv and exit, or
    the process is left running with a dead worker thread (which is what made a
    remote 'restart' silent the runner).

    `console` is for the dashboards: a successor spawned hidden (CREATE_NO_WINDOW)
    has no console at all, so the TUI ran with no UI. It must *inherit* the
    console, not get a new one - CREATE_NEW_CONSOLE opens a separate window that is
    never shown or focused, which looks exactly like 'running in the background'.
    """
    log = log or (lambda _m: None)
    time.sleep(max(0.0, delay))
    log("[update] restarting to run the new toolkit")
    try:
        # sys.argv[0] is the *script*, not the command (the launcher rewrites it), so
        # re-passing the whole argv handed the successor a stray positional argument
        # - argparse rejected it and the window closed with nothing coming back.
        argv = [sys.executable]
        if not getattr(sys, "frozen", False):
            argv.append(os.path.abspath(sys.argv[0]) if sys.argv else "")
        argv += [a for a in sys.argv[1:] if a]
        if os.name == "nt":
            flags = 0 if console else getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen(argv, cwd=os.getcwd(), close_fds=False, creationflags=flags)
            os._exit(0)
        os.execv(sys.executable, argv)
    except Exception as e:
        log("[update] restart failed: %s" % e)


def self_update(repo=None, log=None):
    """Update a git checkout with `git pull`, or a live install with the toolkit copy."""
    repo = repo or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.isdir(os.path.join(repo, ".git")):
        r = run_hidden(["git", "-C", repo, "pull", "--ff-only"],
                       capture_output=True, text=True, timeout=180)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        return {"ok": r.returncode == 0, "kind": "git", "rc": r.returncode, "out": out[-2000:]}
    return update_toolkit(log=log, root=repo)


def _newest_toolkit_copy(root):
    """The most recently fetched toolkit copy under `root` ('' when there is none).

    BENCHDATA is vfat, so the 'current' symlink cannot exist there; this is the
    last-resort way to find what the control plane last fetched.
    """
    best, best_t = "", -1.0
    try:
        for name in sorted(os.listdir(root)):
            d = os.path.join(root, name)
            if not os.path.isdir(d) or name in ("current",):
                continue
            if not os.path.isfile(os.path.join(d, "bench", "benchreport.py")):
                continue
            t = 0.0
            try:
                with open(os.path.join(d, VERSION_FILE), encoding="utf-8") as fh:
                    t = float(json.load(fh).get("fetched_at") or 0)
            except Exception:
                try:
                    t = os.path.getmtime(d)
                except OSError:
                    t = 0.0
            if t > best_t:
                best, best_t = d, t
    except OSError:
        return ""
    return best


def venv_python(repo=None):
    """The repo's virtualenv interpreter when it exists, else ''.

    numpy/psutil/PyTorch live in the venv, so a dashboard or agent started from a
    bare system python must not run the telemetry sampler with that bare
    interpreter: without psutil it records no CPU/RAM/disk/net telemetry.
    """
    repo = repo or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for rel in (("Scripts", "python.exe"), ("bin", "python3"), ("bin", "python")):
        p = os.path.join(repo, ".venv", *rel)
        if os.path.isfile(p):
            return p
    return ""


TORCH_DIR = os.environ.get("BENCH_TORCH_DIR") or "/opt/bench-pypackages"


def _overlay_lowerdirs():
    """The overlay's lower dirs from /proc/mounts (the live image's squashfs)."""
    out = []
    try:
        with open("/proc/mounts") as fh:
            for ln in fh:
                parts = ln.split()
                if len(parts) < 4:
                    continue
                for opt in parts[3].split(","):
                    if opt.startswith("lowerdir="):
                        out += [p for p in opt.split("=", 1)[1].split(":") if p]
    except OSError:
        pass
    return out


def _pth_files():
    import glob
    return glob.glob("/usr/lib/python3*/site-packages/zz-bench-torch.pth")


def ensure_torch_packages(log=None):
    """Keep the image's PyTorch importable after the toolkit is re-pointed.

    Images bake the packages either into the toolkit dir (older builds, at
    /opt/bench-test/py-packages) or into TORCH_DIR (newer ones). Re-pointing
    /opt/bench-test at a fetched copy hides the older location, which silently
    removes torch - every GPU phase then dies with ModuleNotFoundError - so the
    packages are bound to the stable path and the .pth is made to list it.

    Returns the directory python should use ('' when there is nothing to link).
    """
    log = log or (lambda _m: None)
    stable = TORCH_DIR
    if os.path.isdir(os.path.join(stable, "torch")):
        return stable
    src = ""
    cands = [os.path.join(TOOLKIT_DIR, "py-packages")]
    cands += [os.path.join(d, "opt", "bench-test", "py-packages") for d in _overlay_lowerdirs()]
    for cand in cands:
        if os.path.isdir(os.path.join(cand, "torch")):
            src = cand
            break
    if not src:
        return ""
    try:
        os.makedirs(stable, exist_ok=True)
        if not os.path.ismount(stable):
            r = subprocess.run(["mount", "--bind", src, stable], capture_output=True, timeout=60)
            if r.returncode != 0:
                log("[torch] could not bind %s -> %s" % (src, stable))
                return ""
        for pth in _pth_files():
            try:
                with open(pth, encoding="utf-8") as fh:
                    have = fh.read().split()
            except OSError:
                continue
            if stable not in have:
                with open(pth, "a", encoding="utf-8") as fh:
                    fh.write(stable + "\n")
        log("[torch] %s -> %s" % (src, stable))
    except Exception as e:
        log("[torch] link failed: %s" % e)
        return ""
    return stable


def toolkit_boot_link(log=None):
    """Re-point the toolkit at the persisted copy at boot (live USB)."""
    log = log or (lambda _m: None)
    root = os.path.join(persist_root(), "bench-toolkit")
    cur = os.path.join(root, "current")
    target = ""
    if os.path.isfile(os.path.join(cur, "bench", "benchreport.py")):
        target = os.path.realpath(cur)
    else:
        # no symlink (vfat BENCHDATA cannot hold one): try the pointer file, then
        # the freshest fetched copy
        try:
            with open(os.path.join(root, "current.txt"), encoding="utf-8") as fh:
                p = fh.read().strip()
        except OSError:
            p = ""
        if p and os.path.isfile(os.path.join(p, "bench", "benchreport.py")):
            target = p
        else:
            target = _newest_toolkit_copy(root)
    if not target:
        return False
    if os.path.islink(TOOLKIT_DIR) and os.path.realpath(TOOLKIT_DIR) == os.path.realpath(target):
        return True
    ok = repoint_toolkit(TOOLKIT_DIR, os.path.realpath(target))
    if ok:
        log("[bench] toolkit -> %s" % os.path.realpath(target))
    return ok


def gh_release(tag):
    """Return {tag, assets: [{name, url, size}]} for a public release."""
    url = "%s/repos/%s/releases/tags/%s" % (GH_API, REPO_SLUG, tag)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                   "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            j = json.loads(r.read().decode())
    except Exception as e:
        return None, "release %s not found (%s)" % (tag, e)
    assets = [{"name": a.get("name"), "url": a.get("browser_download_url"),
               "size": a.get("size")} for a in (j.get("assets") or [])]
    return {"tag": j.get("tag_name") or tag, "assets": assets}, None


def iso_fetch(tag, gpu="nvidia-legacy", dest=None, log=None):
    """Download + concatenate a release's ISO parts and verify SHA256SUMS."""
    log = log or (lambda _m: None)
    rel, err = gh_release(tag)
    if err:
        return {"error": err}
    parts = sorted([a for a in rel["assets"] if re.match(r"^bench-live-.*\.iso\.part\.", a["name"] or "")],
                   key=lambda a: a["name"])
    if not parts:
        return {"error": "no ISO parts on %s" % tag, "assets": [a["name"] for a in rel["assets"]]}
    out_dir = dest or os.path.join(_stage_root(), "iso")
    os.makedirs(out_dir, exist_ok=True)
    iso = os.path.join(out_dir, "bench-live-%s.iso" % gpu)
    total = 0
    try:
        with open(iso, "wb") as fh:
            for a in parts:
                log("[iso] %s" % a["name"])
                total += _download(a["url"], fh, log=None, timeout=900)
    except Exception as e:
        return {"error": "download failed: %s" % e, "iso": iso, "bytes": total}
    got = sha256_file(iso)
    ok = None
    sums = [a for a in rel["assets"] if (a["name"] or "") == "SHA256SUMS"]
    if sums:
        try:
            f = os.path.join(out_dir, "SHA256SUMS")
            _download(sums[0]["url"], f, log=None, timeout=60)
            want = None
            for ln in open(f, encoding="utf-8"):
                p = ln.split()
                if len(p) >= 2 and p[-1].lstrip("*").endswith(".iso"):
                    want = p[0].lower()
            ok = (want is None) or (want == got)
        except Exception:
            ok = None
    return {"ok": True, "iso": iso, "bytes": total, "sha256": got, "verified": ok,
            "parts": len(parts), "tag": rel["tag"]}


def _device_of_path(path):
    """The block device backing a path (from /proc/mounts), or ''."""
    best, dev = "", ""
    try:
        rp = os.path.realpath(path)
        with open("/proc/mounts") as fh:
            for ln in fh:
                p = ln.split()
                if len(p) < 2:
                    continue
                mnt = os.path.realpath(p[1].replace("\\040", " "))
                if (rp == mnt or rp.startswith(mnt.rstrip("/") + "/")) and len(mnt) >= len(best):
                    best, dev = mnt, p[0]
    except OSError:
        return ""
    return os.path.realpath(dev) if dev else ""


def _mounts_of(device):
    """[(source, mountpoint)] for the partitions of `device` (from /proc/mounts)."""
    base = os.path.basename(os.path.realpath(device))
    out = []
    try:
        with open("/proc/mounts") as fh:
            for ln in fh:
                p = ln.split()
                if len(p) < 2:
                    continue
                src = os.path.basename(p[0])
                if src == base or (src.startswith(base) and src[len(base):].isdigit()):
                    out.append((p[0], p[1].replace("\\040", " ")))
    except OSError:
        return []
    return out


def _unmount_all(device):
    """Unmount every partition of the device (needed before write/re-read)."""
    base = os.path.basename(os.path.realpath(device))
    try:
        with open("/proc/mounts") as fh:
            for ln in fh:
                p = ln.split()
                if len(p) < 2:
                    continue
                src = os.path.basename(p[0])
                if src == base or (src.startswith(base) and src[len(base):].isdigit()):
                    subprocess.run(["umount", p[1].replace("\\040", " ")],
                                   capture_output=True, timeout=30)
    except OSError:
        pass


def _is_usb_device(device):
    """True when the device sits on a USB transport (or reports removable)."""
    try:
        rp = os.path.realpath(device)
        out = subprocess.run(["udevadm", "info", "-q", "path", "-n", rp],
                             capture_output=True, text=True, timeout=10).stdout
        if "usb" in out:
            return True
        name = os.path.basename(rp)
        disk = name
        if not os.path.exists("/sys/block/%s" % disk):
            parent = subprocess.run(["lsblk", "-no", "PKNAME", rp],
                                    capture_output=True, text=True, timeout=10).stdout.strip()
            disk = parent or name
        with open("/sys/block/%s/removable" % disk) as fh:
            return fh.read().strip() == "1"
    except Exception:
        return False


def _mountpoint_of(device):
    """Mountpoint of the device or any of its partitions, or ''."""
    base = os.path.basename(os.path.realpath(device))
    best = ""
    try:
        with open("/proc/mounts") as fh:
            for ln in fh:
                p = ln.split()
                if len(p) < 2:
                    continue
                src = os.path.basename(p[0])
                if src == base or (src.startswith(base) and src[len(base):].isdigit()):
                    best = p[1].replace("\\040", " ")
    except OSError:
        pass
    return best


def _stash_confs(device):
    """Read the upload/agent configs before a destructive write."""
    confs = {}
    mnt = _mountpoint_of(device)
    if mnt:
        for n in ("bench-upload.conf", "bench-agent.conf"):
            try:
                with open(os.path.join(mnt, n), "rb") as fh:
                    confs[n] = fh.read()
            except OSError:
                pass
    for n, p in (("bench-upload.conf", "/etc/bench-upload.conf"),
                 ("bench-agent.conf", "/etc/bench-agent.conf")):
        if n not in confs:
            try:
                with open(p, "rb") as fh:
                    confs[n] = fh.read()
            except OSError:
                pass
    return confs


def _stage_root():
    """Staging root for ISOs: RAM on a live system (the stick is the target),
    the persistent partition otherwise."""
    if os.path.exists("/run/archiso"):
        stage = "/var/tmp/bench-iso-stage"
        if not os.path.ismount(stage):
            try:
                os.makedirs(stage, exist_ok=True)
                subprocess.run(["mount", "-t", "tmpfs", "-o", "size=6144M", "tmpfs", stage],
                               capture_output=True, timeout=30)
            except Exception:
                pass
        if os.path.ismount(stage):
            return stage
    return persist_root()


def _finish_stick(device, iso_bytes, confs, log=None):
    """Recreate the BENCHDATA partition + restore configs after a raw write.

    Mirrors Build-LiveUSB.sh's write step so a live-flashed stick keeps its
    results partition and credentials. USB devices only; best effort."""
    log = log or (lambda _m: None)
    if not _is_usb_device(device):
        return {"partition": "skipped (not a USB device)"}
    _unmount_all(device)
    start_mib = iso_bytes // 1048576 + 16
    subprocess.run(["sync"], timeout=60)
    subprocess.run(["blockdev", "--rereadpt", device], capture_output=True, timeout=30)
    subprocess.run(["partx", "-a", device], capture_output=True, timeout=30)
    log("[iso] creating BENCHDATA partition at %dMiB" % start_mib)
    r = subprocess.run(["sfdisk", "--append", device],
                       input="start=%d, type=c\n" % (start_mib * 2048),
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return {"partition": "sfdisk failed: %s" % (r.stderr.strip()[:140] or r.stdout.strip()[:140])}
    subprocess.run(["sync"], timeout=60)
    time.sleep(1)
    r1 = subprocess.run(["blockdev", "--rereadpt", device], capture_output=True, text=True, timeout=30)
    r2 = subprocess.run(["partx", "-a", device], capture_output=True, text=True, timeout=30)
    time.sleep(2)
    out = subprocess.run(["lsblk", "-lbno", "NAME,START", device],
                         capture_output=True, text=True, timeout=20).stdout
    part = ""
    min_sec = iso_bytes // 512
    for ln in out.splitlines():
        f = ln.split()
        if len(f) == 2 and f[1].isdigit() and int(f[1]) >= min_sec:
            part = "/dev/" + f[0]
            break
    if not part:
        return {"partition": "not visible yet",
                "rereadpt": (r1.stderr or r1.stdout).strip()[:120],
                "partx": (r2.stderr or r2.stdout).strip()[:120]}
    subprocess.run(["mkfs.vfat", "-n", "BENCHDATA", part], capture_output=True, timeout=120)
    restored = []
    os.makedirs(PERSIST_DIR, exist_ok=True)
    if subprocess.run(["mount", part, PERSIST_DIR], capture_output=True, timeout=30).returncode == 0:
        for n, data in confs.items():
            try:
                with open(os.path.join(PERSIST_DIR, n), "wb") as fh:
                    fh.write(data)
                restored.append(n)
            except OSError:
                pass
        subprocess.run(["umount", PERSIST_DIR], capture_output=True, timeout=30)
    log("[iso] BENCHDATA on %s (restored: %s)" % (part, ", ".join(restored) or "none"))
    return {"partition": part, "restored": restored}


def _esp_device():
    """The BENCH_ESP partition device, or '' when this is not an A/B stick."""
    try:
        out = subprocess.run(["blkid", "-o", "device", "-t", "LABEL=BENCH_ESP"],
                             capture_output=True, text=True, timeout=10).stdout.split()
    except Exception:
        return ""
    return out[0] if out else ""


def _grubenv_read(path):
    env = {}
    try:
        with open(path, "rb") as fh:
            for ln in fh.read(1024).decode("utf-8", "replace").splitlines():
                if "=" in ln and not ln.startswith("#"):
                    k, v = ln.split("=", 1)
                    env[k.strip()] = v.strip()
    except OSError:
        pass
    return env


def _grubenv_write(path, env):
    body = "# GRUB Environment Block\n"
    for k in sorted(env):
        body += "%s=%s\n" % (k, env[k])
    body += "#" * (1024 - len(body))
    with open(path, "wb") as fh:
        fh.write(body[:1024].encode())


def _slot_uuids(disk):
    """(uuid_a, uuid_b) for the two slot partitions (2 and 3)."""
    out = []
    for n in (2, 3):
        r = subprocess.run(["blkid", "-p", "-o", "value", "-s", "UUID", "%s%d" % (disk, n)],
                           capture_output=True, text=True, timeout=15).stdout.strip()
        out.append(r)
    return out[0], out[1]


def _write_ab_grub_cfg(path, ua, ub):
    with open(path, "w") as fh:
        fh.write("""set timeout=5
set timeout_style=menu
load_env
if [ "${attempts}" -ge 3 ]; then
    if [ "${active}" = "A" ]; then set active=B; else set active=A; fi
    set attempts=0
    save_env active attempts
fi
if [ "${active}" = "B" ]; then set default=1; else set default=0; fi
menuentry "bench-test (slot A)" {
    search --no-floppy --set=root --fs-uuid %s
    linux /arch/boot/x86_64/vmlinuz-linux archisobasedir=arch archisosearchuuid=%s bench_slot=A copytoram=y nvidia_drm.modeset=1 nvidia_drm.fbdev=1
    initrd /arch/boot/x86_64/initramfs-linux.img
}
menuentry "bench-test (slot B)" {
    search --no-floppy --set=root --fs-uuid %s
    linux /arch/boot/x86_64/vmlinuz-linux archisobasedir=arch archisosearchuuid=%s bench_slot=B copytoram=y nvidia_drm.modeset=1 nvidia_drm.fbdev=1
    initrd /arch/boot/x86_64/initramfs-linux.img
}
""" % (ua, ua, ub, ub))


def iso_write_slot(iso_path, confirm=False, log=None):
    """A/B update: write the ISO into the INACTIVE slot and flip the boot flag.

    The active slot and BENCHDATA are never touched; grub auto-reverts after
    three unconfirmed boots. Auto-detects the stick via the BENCH_ESP label."""
    log = log or (lambda _m: None)
    if not confirm:
        return {"error": "confirm=true required"}
    if not iso_path or not os.path.isfile(iso_path):
        return {"error": "no such iso: %s" % iso_path}
    esp = _esp_device()
    if not esp:
        return {"error": "no BENCH_ESP partition found - not an A/B stick (use iso_write)"}
    disk = esp.rstrip("0123456789")
    ua, ub = _slot_uuids(disk)
    if not ua or not ub:
        return {"error": "cannot read slot UUIDs on %s" % disk}
    # never write an ISO stored on the target stick
    iso_dev = _device_of_path(os.path.dirname(os.path.abspath(iso_path)))
    if iso_dev and iso_dev.rstrip("0123456789") == disk:
        return {"error": "the staged ISO lives on the target stick - stage it in RAM or elsewhere"}
    mnt = "/run/bench-esp"
    os.makedirs(mnt, exist_ok=True)
    if not os.path.ismount(mnt):
        if subprocess.run(["mount", esp, mnt], capture_output=True, timeout=30).returncode != 0:
            return {"error": "cannot mount %s" % esp}
    env = _grubenv_read(os.path.join(mnt, "boot/grub/grubenv"))
    active = (env.get("active") or "A").upper()
    target = "B" if active == "A" else "A"
    part = "%s%d" % (disk, 2 if target == "A" else 3)
    cfg = os.path.join(mnt, "boot/grub/grub.cfg")
    if not os.path.isdir(os.path.dirname(cfg)):
        return {"error": "the ESP (%s) has no boot/grub - refusing to write a boot config the "
                         "firmware would not read" % esp}
    log("[ab] active=%s -> writing slot %s (%s)" % (active, target, part))
    # The raw write needs every partition of the disk released - the ESP we just
    # mounted included - so remember what to put back: the runner keeps running
    # from RAM (copytoram), but BENCHDATA (toolkit copy + results) is on that disk.
    saved = [(s, m) for s, m in _mounts_of(disk) if m != mnt]
    _unmount_all(disk)
    iso_bytes = os.path.getsize(iso_path)
    try:
        try:
            with open(iso_path, "rb") as src, open(part, "wb", buffering=0) as dst:
                shutil.copyfileobj(src, dst, length=4 << 20)
                dst.flush()
                os.fsync(dst.fileno())
        except Exception as e:
            return {"error": "write failed: %s" % e, "slot": target}
        subprocess.run(["sync"], timeout=60)
        # the boot config lives on the ESP, which _unmount_all() just released
        if not os.path.ismount(mnt):
            if subprocess.run(["mount", esp, mnt], capture_output=True, timeout=30).returncode != 0:
                return {"error": "cannot remount the ESP (%s) to update the boot config" % esp,
                        "slot": target}
        subprocess.run(["blockdev", "--rereadpt", disk], capture_output=True, timeout=30)
        time.sleep(1)
        ua, ub = _slot_uuids(disk)
        if not ua or not ub:
            return {"error": "slot UUIDs unreadable after write", "slot": target}
        _write_ab_grub_cfg(cfg, ua, ub)
        env["active"] = target
        env["attempts"] = "0"
        _grubenv_write(os.path.join(mnt, "boot/grub/grubenv"), env)
        subprocess.run(["sync"], timeout=60)
    finally:
        subprocess.run(["umount", mnt], capture_output=True, timeout=30)
        restored = []
        for src_dev, mnt_pt in saved:
            if os.path.ismount(mnt_pt):
                continue
            if subprocess.run(["mount", src_dev, mnt_pt], capture_output=True,
                              text=True, timeout=30).returncode == 0:
                restored.append(mnt_pt)
            else:
                log("[ab] could not remount %s (it comes back at reboot)" % mnt_pt)
    log("[ab] slot %s updated - reboot to try it (auto-revert after 3 failed boots)" % target)
    return {"ok": True, "slot": target, "partition": part, "bytes": iso_bytes,
            "previous": active, "restored": restored}


def iso_write(iso_path, device, confirm=False, log=None):
    """Write a staged ISO to a block device (destructive; guarded)."""
    log = log or (lambda _m: None)
    if not confirm:
        return {"error": "confirm=true required to write an ISO to a device"}
    if not iso_path or not os.path.isfile(iso_path):
        return {"error": "no such iso: %s" % iso_path}
    if not str(device).startswith("/dev/"):
        return {"error": "device must be a /dev/... path"}
    try:
        st = os.stat(device)
    except OSError as e:
        return {"error": "cannot stat %s (%s)" % (device, e)}
    if not stat.S_ISBLK(st.st_mode):
        return {"error": "%s is not a block device" % device}
    # On a live system the stick is safe to reflash in place (copytoram keeps
    # everything in RAM), so only the system-disk guards stay; elsewhere the
    # data/persist mounts are protected too.
    live = os.path.exists("/run/archiso")
    guards = ("/", "/opt") if live else ("/", "/mnt/benchdata", "/opt", PERSIST_DIR)
    for guard in guards:
        if _device_of_path(guard) and _device_of_path(guard) == os.path.realpath(device):
            return {"error": "refusing to overwrite the device backing %s" % guard}
    # Never write an ISO that is stored ON the target device (the write would
    # clobber the file mid-read).
    iso_dev = _device_of_path(os.path.dirname(os.path.abspath(iso_path)))
    if iso_dev and (iso_dev == os.path.realpath(device)
                    or iso_dev.rstrip("0123456789") == os.path.realpath(device)):
        return {"error": "the staged ISO lives on the target device - stage it "
                         "in RAM or on another stick first"}
    confs = _stash_confs(device)
    _unmount_all(device)
    iso_bytes = os.path.getsize(iso_path)
    log("[iso] writing %s -> %s" % (iso_path, device))
    try:
        with open(iso_path, "rb") as src, open(device, "wb", buffering=0) as dst:
            shutil.copyfileobj(src, dst, length=4 << 20)
            dst.flush()
            os.fsync(dst.fileno())
    except Exception as e:
        return {"error": "write failed: %s" % e, "device": device}
    res = {"ok": True, "device": device, "bytes": iso_bytes}
    try:
        res.update(_finish_stick(device, iso_bytes, confs, log=log))
    except Exception as e:
        res["partition_error"] = str(e)
    return res



class TelemetryTail:
    """Tails this run's telemetry CSV (sampled at 2 Hz) and yields canonical rows."""

    def __init__(self, results_dir, interval=0.3, since=None):
        self.dir = results_dir
        self.interval = interval
        self.path = None
        self.fh = None
        self.cols = {}
        self._n = 0
        # Only consider CSVs written from here on: at run start the newest file is
        # still the PREVIOUS run's, and tailing it posted that run's samples (with
        # its clock) under the new run id - which is what stretched the charts.
        self.since = time.time() if since is None else since

    def _find(self):
        files = [f for f in glob.glob(os.path.join(self.dir, "*.telemetry.csv"))
                 if os.path.getmtime(f) >= self.since - 1]
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
def _nvenc_row(obj):
    """{codec: fps} from either nvenc report shape (list or codec-keyed dict).

    Windows writes {results: [{codec, fps, enc_util_max}, ...]}, the Linux phase
    writes {phase: bench, results: {h264_nvenc: {fps} | {skipped, reason}}}.
    Neither has a 'phases' array, which is why this never reached the summary.
    """
    if not isinstance(obj, dict):
        return None
    res = obj.get("results")
    fps, util, skipped = {}, {}, {}
    if isinstance(res, dict):
        for codec, v in res.items():
            if not isinstance(v, dict):
                continue
            if v.get("skipped"):
                skipped[codec] = str(v.get("reason") or "skipped")[:160]
            elif v.get("fps") is not None:
                fps[codec] = v["fps"]
    elif isinstance(res, list):
        for v in res:
            if not isinstance(v, dict) or not v.get("codec"):
                continue
            codec = v["codec"]
            if v.get("skipped"):
                skipped[codec] = str(v.get("reason") or "skipped")[:160]
            elif v.get("fps") is not None:
                fps[codec] = v["fps"]
            if v.get("enc_util_max") is not None:
                util[codec] = v["enc_util_max"]
    if not (fps or skipped):
        return None
    row = {"fps": fps}
    if util:
        row["enc_util_max"] = util
    if skipped:
        row["skipped"] = skipped
    return row


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
                # the size is what makes this comparable: the same card reports a
                # very different TFLOPS at N=4096 vs N=10240, and the total adds the
                # conv work the matmul-only number leaves out
                if r.get("size") is not None:
                    out["gpu_soak_size"] = r.get("size")
                if r.get("total_tflops") is not None:
                    out["gpu_soak_total_tflops"] = r.get("total_tflops")
                out["gpu_soak_telemetry"] = r.get("telemetry")
            elif r.get("gflops") is not None:
                out["cpu_soak_gflops"] = r.get("gflops")
                if r.get("size") is not None:
                    out["cpu_soak_size"] = r.get("size")
            elif r.get("gbps") is not None:
                out["ram_soak_gbps"] = r.get("gbps")
        elif phase == "seqwrite" and r.get("mbps") is not None:
            out["disk_seqwrite_mbps"] = r.get("mbps")
        elif phase == "integrity" and r.get("bad_elements") is not None:
              # a verdict, not a number: a mismatch or a run cut short by a
              # MemoryError must not read as "verified"
              out["ram_integrity"] = {"bad_elements": r.get("bad_elements"),
                                      "chunks": r.get("chunks"),
                                      "passes": r.get("passes"),
                                      "short": r.get("short")}
        elif phase == "bench" and r.get("download_mbps") is not None:
            out["net"] = r
        elif phase in ("stress", "fullsoak") and isinstance(r.get("verdict"), dict):
            out["fullsoak"] = {"group": group or "fullsoak", "phase": phase, "result": r}
        nv = _nvenc_row(r)
        if nv:
            out["nvenc"] = nv
        if group == "gfx" and isinstance(r, dict):
            out["gfx"] = r
    # nvenc also writes a flat report with no phases array (Windows), and the
    # Windows gfx phase leaves a Unigine Valley screenshot instead of a result
    nv = _nvenc_row(data)
    if nv:
        out["nvenc"] = nv
    if "gfx" not in out:
        shots = sorted(glob.glob(os.path.join(os.path.dirname(report_path), "valley-*.png")),
                       key=os.path.getmtime)
        if shots:
            out["gfx"] = {"screenshot": os.path.basename(shots[-1])}
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
    # Windows nvenc writes a flat report with no phases array, so the loop below
    # never sees it
    if "nvenc" not in s:
        res = j.get("results")
        if isinstance(res, list):
            res = {str(x.get("codec")): {k: v for k, v in x.items() if k != "codec"}
                   for x in res if isinstance(x, dict) and x.get("codec")}
        if isinstance(res, dict) and any(isinstance(v, dict) and ("fps" in v or v.get("skipped"))
                                         for v in res.values()):
            s["nvenc"] = {"width": j.get("width"), "height": j.get("height"), "results": res}
    def keep_gfx(r):
        """A scored gfx record wins; a skipped one only stands in until one arrives.

        setd() refuses to overwrite, which is why the first record used to stick, so
        assign directly. Several gfx records can exist: furmark "skipped" (no
        monitor) and then the glmark2 fallback that actually ran and scored.
        """
        if not isinstance(r, dict):
            return
        have = s.get("gfx") or {}
        new_scored = r.get("score") is not None and not r.get("skipped")
        have_scored = have.get("score") is not None and not have.get("skipped")
        if new_scored or not have_scored:
            s["gfx"] = r

    # A Windows gfx phase writes a FLAT report ({host, timestamp, engine, result})
    # with no phases array, so the loop below never sees it - the same shape problem
    # nvenc had, and it left the AMD run with no gfx in its summary at all.
    flat = j.get("result")
    if isinstance(flat, dict):
        phase = str(flat.get("phase") or flat.get("group") or "").lower()
        if "score" in flat or phase in ("furmark", "valley", "glmark2", "glmark2-es2"):
            keep_gfx(flat)

    ph = j.get("phases")
    items = ph if isinstance(ph, list) else (list(ph.values()) if isinstance(ph, dict) else [])
    for p in items:
        if not isinstance(p, dict):
            continue
        r = p.get("result") if isinstance(p.get("result"), dict) else p
        phase = p.get("phase") or r.get("phase")
        grp = p.get("group") or r.get("group")
        if grp == "gfx":
            keep_gfx(r)
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
                setd("gpu_soak_size", r.get("size"))
                setd("gpu_soak_total_tflops", r.get("total_tflops"))
                setd("gpu_soak_telemetry", r.get("telemetry"))
            elif r.get("gflops") is not None:
                setd("cpu_soak_gflops", r["gflops"])
                setd("cpu_soak_size", r.get("size"))
            elif r.get("gbps") is not None:
                setd("ram_soak_gbps", r["gbps"])
        elif phase == "seqwrite":
            setd("disk_seqwrite_mbps", r.get("mbps"))
        elif phase == "integrity" and r.get("bad_elements") is not None:
              # a verdict, not a number: a mismatch or a run cut short by a
              # MemoryError must not read as "verified"
              setd("ram_integrity", {"bad_elements": r.get("bad_elements"),
                                     "chunks": r.get("chunks"),
                                     "passes": r.get("passes"),
                                     "short": r.get("short")})
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
            if isinstance(res, dict) and any(isinstance(v, dict) and ("fps" in v or v.get("skipped"))
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


def _hardware_verdicts(results_dir, since=None, window_h=8):
    """Pass/fail signals from the Windows health report and the Linux inventory.

    The results dir accumulates one health/inventory report per phase (and earlier
    runs), so the same finding arrives many times with a drifting event count - a
    real desktop run produced ~50 entries for the same four findings. Key each
    finding by kind and keep the widest window, because this list is a verdict, not
    an event log.
    """
    v = {}          # kind -> {"level", "text", "_n"}
    order = []
    rank = {"info": 0, "good": 1, "warn": 2, "bad": 3}

    def add(level, kind, text, n=None):
        cur = v.get(kind)
        if cur is None:
            v[kind] = {"level": level, "text": text, "_n": n}
            order.append(kind)
            return
        if n is not None and (cur["_n"] is None or n > cur["_n"]):
            cur["text"], cur["_n"] = text, n
        if rank.get(level, 0) > rank.get(cur["level"], 0):
            cur["level"] = level

    days = None
    for p in glob.glob(os.path.join(results_dir, "*.health.json")):
        mt = os.path.getmtime(p)
        if since is not None:
            if mt < since - 5:            # a previous run's report, not this one's
                continue
        elif time.time() - mt > window_h * 3600:
            continue
        try:
            with open(p, encoding="utf-8-sig") as fh:
                h = json.load(fh)
        except Exception:
            continue
        if days is None and h.get("days"):
            days = int(h["days"])
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
        # The health report scans a window (7 days by default), so a count is the
        # machine's recent history, not necessarily this run: say which window it is.
        win = " in the last %d days" % days if days else " recently"
        if kp:
            add("bad", "kp", "unexpected shutdown (Kernel-Power 41)%s: %d" % (win, kp), kp)
        if bc:
            add("bad", "bc", "bugcheck / BSOD%s: %d" % (win, bc), bc)
        if stor:
            add("warn", "stor", "storage events%s: %d" % (win, stor), stor)
        if therm:
            add("warn", "therm", "thermal events%s: %d" % (win, therm), therm)
        if whea:
            add("bad", "whea", "WHEA hardware errors%s: %d event(s)" % (win, whea), whea)
        if tdr:
            add("bad", "tdr", "display driver resets (TDR)%s: %d" % (win, tdr), tdr)
        if not whea and not tdr:
            add("good", "whea_ok", "no WHEA / TDR events%s" % win)
        if not kp and not bc:
            add("good", "kp_ok", "no unexpected shutdowns or bugchecks%s" % win)
        for d in (h.get("disks") or []):
            if d.get("read_errors") or d.get("write_errors"):
                add("bad", "disk_io_%s" % d.get("name"),
                    "disk %s I/O errors r/w=%s/%s"
                    % (d.get("name"), d.get("read_errors"), d.get("write_errors")))
            try:
                if d.get("wear") is not None and float(d["wear"]) >= 80:
                    add("warn", "disk_wear_%s" % d.get("name"),
                        "disk %s wear %s%%" % (d.get("name"), d["wear"]))
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
                add("bad", "disk_predfail_%s" % d.get("name"),
                    "disk %s reports predictive failure" % d.get("name"))
            if str(d.get("health") or "").lower() not in ("healthy", "", "none"):
                add("warn", "disk_health_%s" % d.get("name"),
                    "disk %s health: %s" % (d.get("name"), d.get("health")))
        for sp in (inv.get("SmartPredict") or []):
            if sp.get("predict_failure"):
                add("bad", "smart_%s" % sp.get("instance"),
                    "SMART predictive failure: %s" % sp.get("instance"))
        disks = inv.get("Disks") or []
        if disks and inv.get("Elevated") is False and \
                all(d.get("wear") is None and d.get("temp_c") is None for d in disks):
            add("info", "smart_not_elevated",
                "disk SMART not collected (battery not elevated)")
        hw = inv.get("hardware_errors") or []
        if hw:
            add("warn", "hw_errors", "hardware error lines since boot: %d" % len(hw), len(hw))
        ecc = inv.get("ecc") or {}
        if ecc.get("ue"):
            add("bad", "ecc_ue", "ECC uncorrectable errors: %d" % ecc["ue"], ecc["ue"])
        elif ecc.get("ce"):
            add("warn", "ecc_ce", "ECC correctable errors: %d" % ecc["ce"], ecc["ce"])
        for dev, d in (inv.get("disk_health") or {}).items():
            me = d.get("media_errors")
            if isinstance(me, (int, float)) and me > 0:
                add("bad", "media_%s" % dev, "%s media errors: %d" % (dev, me), me)
            up = d.get("used_pct")
            if isinstance(up, (int, float)) and up >= 80:
                add("warn", "used_%s" % dev, "%s wear %s%%" % (dev, up))
    return [{"level": v[k]["level"], "text": v[k]["text"]} for k in order]


def build_summary_dir(results_dir, window_h=8, since=None):
    """Merge all of a run's report JSONs (Windows main + per-group) into one row.

    `since` (a unix time, normally the run's start) restricts the merge to files
    this run wrote: the results dir keeps earlier runs for hours, and merging
    those made a summary inherit metrics it never measured (a 'System info + 60s
    soak' run reported disk/net numbers from a previous run).
    """
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
        if since is not None:
            if mt < since - 5:          # a little slack for write ordering
                continue
        elif now - mt > window_h * 3600:
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
    verdicts = _hardware_verdicts(results_dir, since=since, window_h=window_h)
    if verdicts:
        out["verdicts"] = verdicts
    # Windows gfx is a Unigine Valley run kept as a screenshot. Scope it to this
    # run exactly like the reports above: the results dir keeps earlier runs for
    # hours, and an unscoped screenshot made a FurMark run report a stale Valley
    # result from the previous day.
    if "gfx" not in out:
        shots = sorted(glob.glob(os.path.join(results_dir, "valley-*.png")),
                       key=os.path.getmtime)
        if since is not None:
            shots = [s for s in shots if os.path.getmtime(s) >= since - 5]
        else:
            shots = [s for s in shots if now - os.path.getmtime(s) <= window_h * 3600]
        if shots:
            out["gfx"] = {"screenshot": os.path.basename(shots[-1])}
    return out


def prune_results(results_dir, keep=100, log=None):
    """Keep the newest `keep` run directories under a results dir.

    A results dir accumulates one directory per run (`host-mode-YYYYMMDD-HHMMSS`,
    with a subdirectory per phase) and nothing ever removed them. 100 runs is
    generous - a run is ~1-2 MB now that the Valley frames are pruned - and
    BENCH_KEEP_RUNS=0 keeps everything. Only directories whose name matches the
    run pattern are candidates; anything else is left alone.
    """
    log = log or (lambda _m: None)
    if keep is None or keep <= 0:
        return 0
    try:
        dirs = [d for d in glob.glob(os.path.join(results_dir, "*"))
                if os.path.isdir(d) and
                re.match(r"^.+-[0-9]{8}-[0-9]{6}$", os.path.basename(d))]
    except OSError:
        return 0
    if len(dirs) <= keep:
        return 0
    removed = 0
    for d in sorted(dirs, key=os.path.getmtime)[:-keep]:
        try:
            shutil.rmtree(d)
            removed += 1
        except OSError:
            pass
    if removed:
        log("[report] pruned %d old run dir(s), kept %d" % (removed, keep))
    return removed


def prune_screenshots(results_dir, keep=1, log=None):
    """Keep only the newest `keep` Valley frames in a run's results dir.

    Unigine Valley writes a full-resolution PNG per frame (five were typical, ~5 MB
    each), which made screenshots 91% of the results directory - 662 MB of 727 MB
    across 43 runs, and the same pattern on the live stick's small USB. One frame
    is enough for the run's gfx record to have an image; the score is in the report.
    """
    log = log or (lambda _m: None)
    try:
        shots = sorted(glob.glob(os.path.join(results_dir, "valley-*.png")),
                       key=os.path.getmtime)
    except OSError:
        return 0
    removed = 0
    for p in shots[:-keep] if keep else shots:
        try:
            os.remove(p)
            removed += 1
        except OSError:
            pass
    if removed:
        log("[report] pruned %d Valley screenshot(s), kept %d" % (removed, keep))
    return removed


def upload_run_files(rep, results_dir, log=None, limit=60, max_bytes=512 * 1024):
    """Attach the run's own report files, per-phase logs and gfx screenshot.

    Everything a phase writes otherwise stays on the machine that ran it: the
    per-phase reports, the inventory, the nvenc report, the Valley frame and the
    logs (including the gfx diagnostics). They are small, so they go up as
    artifacts next to the telemetry CSV.
    """
    log = log or (lambda _m: None)
    try:
        _keep = int(os.environ.get("BENCH_KEEP_RUNS") or 100)
    except ValueError:
        _keep = 100
    prune_results(results_dir, keep=_keep, log=log)
    prune_screenshots(results_dir, log=log)
    if rep is None or not getattr(rep, "run_id", None):
        return 0
    keep = []

    def add(p):
        try:
            if os.path.isfile(p) and os.path.getsize(p) <= max_bytes:
                keep.append(p)
        except OSError:
            pass

    for pat in ("*.json", "*.md"):
        for p in glob.glob(os.path.join(results_dir, pat)):
            add(p)
    for p in glob.glob(os.path.join(results_dir, "*-logs", "**", "*"), recursive=True):
        add(p)
    keep.sort(key=os.path.getmtime, reverse=True)
    keep = keep[:limit]
    shots = sorted(glob.glob(os.path.join(results_dir, "valley-*.png")),
                   key=os.path.getmtime)
    if shots:
        keep.append(shots[-1])
    n = 0
    for p in keep:
        try:
            with open(p, "rb") as fh:
                rep.artifact(os.path.basename(p), fh.read(), kind="file")
        except OSError:
            continue
        n += 1
    log("[report] attached %d run files" % n)
    return n


def upload_telemetry_csv(rep, results_dir, log=None, cap=20000, sent=None):
    """Send the run's full 1 Hz CSV and attach the raw file.

    The live stream carries only the ten fields the charts use, so everything
    else (disk/net throughput, CPU temp, PCIe link, throttle bits, power limits,
    nvenc, ...) was thrown away when a run ended. Every column is now posted
    (the server keeps the ones outside its fixed schema in its `extra` column)
    and the CSV itself is attached, so nothing is lost.

    `sent` is what the dashboard already streamed live, as
    {csv basename: {elapsed, ...}}. Those rows are skipped: without it every
    sample was stored twice, because the live stream posts the run and this
    upload posted the whole file again at the end (the charts then drew each
    point twice, and the stored rate looked ~2x the configured one). Headless
    runners (agent.py) stream nothing, pass nothing, and still send every row.

    Returns the number of samples sent.
    """
    log = log or (lambda _m: None)
    if rep is None or not getattr(rep, "run_id", None):
        return 0
    try:
        files = glob.glob(os.path.join(results_dir, "*.telemetry.csv"))
    except Exception:
        return 0
    if not files:
        return 0
    path = max(files, key=os.path.getmtime)
    base = os.path.basename(path)
    already = sent.get(base) if isinstance(sent, dict) else None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    except OSError:
        return 0
    if len(lines) < 2:
        return 0
    hdr = [h.strip().lower() for h in lines[0].split(",")]
    canon = [CSV_ALIASES.get(h, h) for h in hdr]
    samples = []
    for ln in lines[1:cap + 1]:
        vals = ln.split(",")
        s = {}
        for i, key in enumerate(canon):
            if i >= len(vals) or vals[i] == "":
                continue
            try:
                s[key] = float(vals[i])
            except ValueError:
                continue            # pstate and friends are labels, not numbers
        if "elapsed" in s:
            if already and s["elapsed"] in already:
                continue        # already streamed live; sending it again doubles the chart
            samples.append(s)
    for i in range(0, len(samples), 200):
        rep.telemetry(samples[i:i + 200])
    rep.artifact(os.path.basename(path), "\n".join(lines) + "\n", kind="csv")
    log("[report] telemetry: %d samples%s + %s"
        % (len(samples),
           " (%d already streamed live)" % len(already) if already else "",
           os.path.basename(path)))
    return len(samples)


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
