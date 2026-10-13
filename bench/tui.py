#!/usr/bin/env python3
"""bench-tui - an htop/nvtop-style terminal dashboard for the bench-test toolkit.

Full-screen curses UI: run presets and group toggles, live phase status with
elapsed timers, htop-style CPU/RAM and nvtop-style GPU telemetry (temps, fan % +
RPM, power, clocks, VRAM) fed from the 1 Hz telemetry CSV, a log tail, a report
viewer, and one-key collect to the live USB.

    python bench/tui.py [--results-dir DIR] [--demo] [--exit-after SECS]

Keys:  s start · x stop · c collect · u upload · f fan-curve · p cycle preset · 1-0 groups
       r report · G graphical dashboard · t shell · PgUp/PgDn log · q quit
"""
import argparse
import curses
import glob
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque

def _repo_dir():
    """The toolkit checkout, resolved even when frozen (PyInstaller) or run from
    elsewhere: $BENCH_REPO, next to the executable, or the cwd."""
    cands = [os.environ.get("BENCH_REPO")]      # explicit override wins
    if getattr(sys, "frozen", False):
        cands.append(os.path.dirname(os.path.abspath(sys.executable)))
    cands.append(os.getcwd())
    for c in cands:
        if c and os.path.isdir(os.path.join(c, "bench")):
            return os.path.abspath(c)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


REPO = _repo_dir()
BENCH = os.path.join(REPO, "bench")
FAN_CURVE = os.path.join(BENCH, "fan_curve.py")
FAN_CONF = "/etc/bench-fan-curve.conf"
RESULT_COLLECTOR = "/usr/local/bin/bench-collect"
GUI_LAUNCHER = "/usr/local/bin/bench-gui"
sys.path.insert(0, BENCH)
try:
    from benchreport import (Reporter, PhaseTracker, ReporterPump, build_summary, build_summary_dir,
                             telemetry_summary, newest_report, load_identity, run_exec, system_info,
                             read_inventory, load_conf, CSV_ALIASES, power_action,
                             supported_groups,
                             update_toolkit, iso_fetch, iso_write, iso_write_slot,
                             self_update, restart_self,
                             toolkit_version, is_live, update_needs_restart, hardware_line,
                             DASH_FIELDS, auto_update, should_restart_after_update, venv_python,
                         upload_telemetry_csv, persist_root, upload_run_files,
                         extra_runner_args, tune_summary, bench_version, version_string,
                         drop_stray_script_arg)
except Exception:  # reporting is optional
    Reporter = None

# Windows runs the Python battery directly - the .sh runner is Linux-only and
# starting it there fails with WinError 193 - and it needs a real interpreter
# carrying numpy/psutil (and the GPU backend). bootstrap provisions one (cached)
# without a checkout or system Python; a repo .venv wins if present.
try:
    import bootstrap  # stdlib-only local env helper
except Exception:
    bootstrap = None

if os.name == "nt":
    RUNNER = None
    RUNNER_PREFIX = [os.path.join(BENCH, "run_win.py")]

    def runner_command(args):
        py = (bootstrap.python_path(REPO, ensure=True) if bootstrap else "") \
            or venv_python(REPO) or sys.executable
        return [py] + RUNNER_PREFIX + args, py
else:
    RUNNER = os.path.join(BENCH, "Run-LinuxBench.sh")
    RUNNER_PREFIX = []

    def runner_command(args):
        return [RUNNER] + args, ""


def _kill_tree(proc):
    """Kill a battery process AND its children (run_win -> powershell -> python).

    On Windows the runner tree (bootstrap python -> run_win.py -> PowerShell
    battery -> phase python + the sensor sampler) must go: a plain terminate
    leaves the grandchildren orphaned, holding the toolkit folder."""
    if not proc or proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass


GROUPS = ["gpu", "cpu", "ram", "disk", "combined", "fullsoak", "net", "nvenc", "llm", "fan", "sys", "gfx"]

# Only offer toggles for the groups this platform's battery actually implements
# (fan is Linux-only, llm is Windows-only): an offered no-op group is a silent lie.
try:
    UI_GROUPS = [g for g in GROUPS if g in supported_groups()]
except Exception:
    UI_GROUPS = list(GROUPS)

# one unique hotkey per offered toggle (1-9, 0, then - / =): the old 1-0 map
# wrapped at ten, so sys and gfx were unreachable and their labels collided
_GROUP_KEYS = list("1234567890-=")[:len(UI_GROUPS)]
_KEY_HINT = {11: "1-9,0,-", 12: "1-9,0,-,="}.get(len(_GROUP_KEYS), ",".join(_GROUP_KEYS))


def _g(*on):
    return [g for g in GROUPS if g in on]


_DEF_DUR = {"gpu": "600", "combined": "300", "fullsoak": "300", "gfx": "60"}

# Extra knobs the runner accepts but the four duration fields do not expose; a
# preset carries them in "tune". Without them a run uses the runner's long
# defaults (cpu 120s, ram 120s, gpu matrix 12288, steady 10GB, disk 2048MB).
#
# disk_size_mb is the one that bit hardest: the randread/randwrite/randmix phases
# are size-bound, not seconds-bound, so the default 2048MB cost 226s of the
# desktop's 1368s stabilized run. 512MB does the same job in ~57s.
_TUNE = {"cpu_soak": 45, "ram_soak": 30, "disk_seconds": 30, "disk_size_mb": 512,
         "gpu_size": 4096, "cpu_size": 2048, "ram_size_mb": 512, "passes": 1,
         "steady_gb": 2, "stable": 1, "stable_hold": 60, "gfx_engine": "furmark"}

# "Quick check" has to be quick: with no tune it inherits the runner's long
# defaults (cpu 120s, ram 120s, 10 GiB steady write, gpu matrix 12288).
_QUICK_TUNE = {"cpu_soak": 60, "ram_soak": 30, "disk_seconds": 15, "disk_size_mb": 256,
               "gpu_size": 4096, "cpu_size": 2048, "ram_size_mb": 512, "passes": 1,
               "steady_gb": 2}

PRESETS = [
    ("Quick check (short soaks)", {"groups": _g("gpu", "cpu", "ram", "disk", "combined", "fullsoak",
                                                "net", "nvenc", "sys", "gfx"),
                                   "dur": {"gpu": "60", "combined": "60", "fullsoak": "60", "gfx": "30"},
                                   "tune": dict(_QUICK_TUNE)}),
    ("Stabilized full soak (15 min)", {"groups": _g("gpu", "cpu", "ram", "disk", "combined", "fullsoak",
                                                    "net", "nvenc", "fan", "sys", "gfx"),
                                       "dur": {"gpu": "180", "combined": "45", "fullsoak": "90", "gfx": "30"},
                                       "tune": dict(_TUNE)}),
    ("System info + 60s soak (all)", {"groups": _g("sys", "fullsoak"),
                                      "dur": {**_DEF_DUR, "fullsoak": "60"}}),
    ("Full run (defaults)", {"groups": list(GROUPS), "dur": dict(_DEF_DUR)}),
    ("GPU compute", {"groups": _g("gpu", "sys"), "dur": {**_DEF_DUR, "gpu": "60"}}),
    ("CPU + RAM + disk", {"groups": _g("cpu", "ram", "disk", "sys"), "dur": dict(_DEF_DUR)}),
    ("Storage + network", {"groups": _g("disk", "net", "sys"), "dur": dict(_DEF_DUR)}),
    ("Video / graphics", {"groups": _g("nvenc", "gfx", "sys"), "dur": dict(_DEF_DUR)}),
    ("Graphics only (FurMark/Valley)", {"groups": _g("gfx", "sys"),
                                        "dur": {**_DEF_DUR, "gfx": "30"},
                                        "tune": {"gfx_engine": "furmark"}}),
    ("LLM inference (Ollama)", {"groups": _g("llm", "sys"), "dur": dict(_DEF_DUR)}),
    ("Fan response", {"groups": _g("fan", "sys"), "dur": dict(_DEF_DUR)}),
    ("Soak only (long)", {"groups": _g("gpu", "fullsoak", "sys"),
                          "dur": {**_DEF_DUR, "gpu": "1800", "fullsoak": "1800"}}),
    ("Full-system soak (long)", {"groups": _g("fullsoak", "sys"),
                                 "dur": {**_DEF_DUR, "fullsoak": "1800"}}),
]

SPARK = "▁▂▃▄▅▆▇█"


# fields the TUI already renders explicitly (so the catch-all line skips them)
_TUI_SHOWN = {"temp_c", "gpu_util", "mem_util", "vram_used_mib", "sm_mhz", "power_w",
              "fan_pct", "cpu_util", "ram_used", "cpu_temp_c", "cpu_max_core_c", "nvme_c",
              "cpu_fan_rpm", "gpu_enc_pct", "gpu_dec_pct", "pstate", "temp_limit_c",
              "power_limit_w", "throttle", "pcie_gen", "pcie_width", "retired", "video_mhz",
              "cpu_pkg_w"}


def _win_swap_gib(commit_limit):
    """(used, total) GiB of real page-file usage, never the commit charge.

    GlobalMemoryStatusEx's TotalPageFile - AvailPageFile is the system commit
    charge (13 GiB on a box whose page file held 62 MiB), so it must not be
    reported as swap. Prefer psutil's swap_memory(); fall back to the same
    NtQuerySystemInformation query run_win.py uses; (0.0, 0.0) when neither is
    available, so the header hides the reading instead of guessing.
    """
    try:
        import psutil
        sm = psutil.swap_memory()
        return (sm.used / 1024 ** 3, sm.total / 1024 ** 3)
    except Exception:
        pass
    try:
        from run_win import _win_pagefile_used_gib
        used = _win_pagefile_used_gib()
        if used is not None:
            return (used, commit_limit / 1024 ** 3)
    except Exception:
        pass
    return (0.0, 0.0)


class BenchTUI:
    def __init__(self, scr, opts):
        self.scr = scr
        self.results_dir = opts["results_dir"]
        self.collected_ok = False
        self.demo = opts["demo"]
        self.exit_after = opts["exit_after"]
        self.preset = 1
        self.groups = dict.fromkeys(GROUPS, True)
        self.dur = {"gpu": "600", "combined": "300", "fullsoak": "300", "gfx": "60"}
        # extra tuning knobs from the selected preset / a control-plane start
        self.tune = {}

        self.q = queue.Queue()
        self.proc = None
        self.running = False
        self.run_t0 = None
        self.exited = False
        self._stopped = False
        self.phases = []
        self.last_phase = {}
        self.log = deque(maxlen=2000)
        self.scroll = 0
        self.follow = True
        self.hw = "detecting..."
        self.vram_total = None
        self.tel = {k: deque(maxlen=1000) for k in ("t", "temp", "fan", "power", "util")}
        self.tel_path = None
        self.tel_cols = {}
        self.csv_last = None
        self.csv_now = {}
        # {csv basename: {elapsed, ...}} already streamed live, so the end-of-run
        # bulk upload does not store the same samples a second time
        self.tel_sent = {}
        self.gpu = {}
        self.status = "idle - press s to start"
        self.status_attr = 0
        self.rep = None
        self.pump = None
        self.tracker = None
        self._pending = queue.Queue()
        self.server = opts.get("server", "")
        self.token = opts.get("token", "")
        self.name = opts.get("name", "")
        self.instance = opts.get("instance") or os.environ.get("BENCH_AGENT_INSTANCE", "")

        # live system sampling (htop-style)
        self.sys_cpu = 0.0
        self.sys_cores = []
        self.sys_ram = (0.0, 0.0)
        self.sys_swap = (0.0, 0.0)
        self.sys_load = 0.0
        self._prev_stat = None
        self._prev_win_cpu = None
        self.curve = self._curve_state()

        self._apply_preset(1)
        self._demo_seed()
        self._setup_reporting()
        self._auto_update()

    def _auto_update(self):
        """Check for a newer checkout at startup; restart into it if pulled."""
        def work():
            res = auto_update(REPO, log=lambda m: self.log.append(m))
            if res.get("updated"):
                self.log.append("[update] restarting into the newer checkout")
                self._restart(1.0)
        threading.Thread(target=work, daemon=True).start()

    # ---------------------------------------------------------- reporting ---
    def _setup_reporting(self):
        if not (self.server and self.token and Reporter):
            return
        try:
            import platform as _p
            ident = load_identity(self.server)
            # name=None lets Reporter fall back to $BENCH_AGENT_NAME or the
            # hostname (an explicit --name still wins).
            self.rep = Reporter(self.server, self.token, name=self.name or None,
                                kind="tui", os_name=_p.system(), version=version_string(REPO),
                                runner_id=ident.get("runner_id"), api_key=ident.get("api_key"),
                                persist=True, instance=self.instance,
                                log=lambda m: self.log.append(m))
            self.sysinfo = system_info()
            caps = {"os": _p.system(), "platform": _p.platform(), "kind": "tui",
                    "groups": supported_groups(), "toolkit": toolkit_version(REPO),
                    "live": is_live(),
                    "gpu": self.sysinfo.get("gpu"), "driver": self.sysinfo.get("driver"),
                    "cpu": self.sysinfo.get("cpu"), "ram_gib": self.sysinfo.get("ram_gib")}
            self._connect_reporting(caps)
        except Exception as exc:
            self.log.append("[report] disabled: %s" % exc)
            self.rep = None

    def _connect_reporting(self, caps):
        # retry until the server is reachable (the network can be up late at boot)
        def work():
            while self.rep and not self.rep.register(caps):
                time.sleep(5)
            if self.rep and self.rep.runner_id:
                self.pump = ReporterPump(self.rep, on_command=self._pending.put,
                                         log=lambda m: self.log.append(m))
                self.pump.start()
                self.log.append("[report] connected to %s" % self.server)
        threading.Thread(target=work, daemon=True).start()

    def _apply_remote(self, cmd):
        kind = cmd.get("kind")
        payload = cmd.get("payload") or {}
        if kind == "start":
            if self.running:
                self.rep.ack(cmd.get("id"), "failed", {"error": "already running"})
                return
            # see gui.py: a bad payload used to kill the process instead of failing the
            # command
            try:
                self.start(profile=payload)
            except Exception as e:
                self.rep.ack(cmd.get("id"), "failed", {"error": "bad start payload: %s" % e})
                return
            self.rep.ack(cmd.get("id"), "done", {"started": True})
        elif kind == "stop":
            self.stop()
            self.rep.ack(cmd.get("id"), "done", {"stopping": True})
        elif kind == "fan_curve":
            self.toggle_curve()
            self.rep.ack(cmd.get("id"), "done", {"toggled": True})
        elif kind in ("shutdown", "reboot"):
            if self.running:
                self.stop()
            self.rep.ack(cmd.get("id"), "done", power_action(reboot=(kind == "reboot")))
        elif kind in ("update", "update_toolkit", "iso_fetch", "iso_write", "iso_write_slot"):
            self._update_remote(cmd.get("id"), kind, payload)
        elif kind == "restart":
            self.rep.ack(cmd.get("id"), "done", {"restart": "scheduled"})
            self._restart()
        elif kind in ("exec", "shell"):
            self._exec_remote(cmd.get("id"), payload)
        else:
            self.rep.ack(cmd.get("id"), "failed", {"error": "unsupported: %s" % kind})

    def _restart(self, delay=0.5):
        """Re-exec for a toolkit switch.

        Drop curses first: execv replaces the process image, so without endwin()
        the successor inherits a terminal left in raw mode / alt screen (the UI
        then never comes back). `console=True` because on Windows a successor
        spawned hidden has no console at all - it runs with no UI.
        """
        def work():
            try:
                curses.endwin()
            except Exception:
                pass
            restart_self(delay, lambda m: self.log.append(m), console=True)
        threading.Thread(target=work, daemon=True).start()

    def _update_remote(self, cid, kind, payload):
        if self.running:
            # the toolkit copy is what the running battery executes from (and
            # where its results land), so switching it mid-run breaks the run
            self.rep.ack(cid, "failed", {
                "error": "a run is in progress - update once it finishes"})
            return

        def work():
            log = lambda m: self.log.append(m)  # noqa: E731
            try:
                if kind == "iso_fetch":
                    res = iso_fetch(payload.get("tag") or "", payload.get("gpu") or "nvidia-legacy",
                                    payload.get("dest"), log=log)
                elif kind == "iso_write":
                    res = iso_write(payload.get("iso"), payload.get("device"),
                                    bool(payload.get("confirm")), log=log)
                elif kind == "iso_write_slot":
                    res = iso_write_slot(payload.get("iso"), bool(payload.get("confirm")), log=log)
                elif kind == "update_toolkit":
                    res = update_toolkit(payload.get("ref") or "main", payload.get("sha256"),
                                         log=log, root=REPO)
                else:
                    res = self_update(REPO, log=log)
            except Exception as e:
                res = {"error": "%s: %s" % (type(e).__name__, e)}
            self.rep.ack(cid, "done" if res.get("ok") else "failed", res)
            if should_restart_after_update(res, running=self.running,
                                           requested=bool(payload.get("restart"))):
                self._restart(1.5)
        threading.Thread(target=work, daemon=True).start()

    def _exec_remote(self, cid, payload):
        def work():
            res = run_exec(self.rep, payload.get("cmd", ""),
                           timeout=int(payload.get("timeout") or 120), cwd=REPO)
            self.rep.ack(cid, "done" if res.get("rc") == 0 else "failed", res)
        threading.Thread(target=work, daemon=True).start()

    def _apply_profile(self, profile):
        for key, val in (profile.get("groups") or {}).items():
            if key in self.groups:
                self.groups[key] = bool(val)
        for g in (profile.get("skip") or []):
            if g in self.groups:
                self.groups[g] = False
        for key, val in {"gpu": profile.get("gpu_soak"), "combined": profile.get("combined"),
                         "fullsoak": profile.get("fullsoak"), "gfx": profile.get("gfx")}.items():
            if val not in (None, "") and key in self.dur:
                self.dur[key] = str(val)
        # keep the remaining knobs the control plane sent (cpu/ram soak, sizes,
        # passes, steady GB, soak-until-stable) for extra_runner_args
        self.tune = dict(profile or {})

    # ------------------------------------------------------------- presets --
    def _apply_preset(self, idx):
        self.preset = idx
        spec = PRESETS[idx][1]
        self.groups = dict.fromkeys(GROUPS, False)
        for g in spec["groups"]:
            self.groups[g] = True
        self.dur.update(spec["dur"])
        self.tune = dict(spec.get("tune") or {})

    def _args(self):
        args = []
        for g in GROUPS:
            if not self.groups[g]:
                args.append("--skip-" + g)
        args += ["--gpu-soak", self.dur["gpu"], "--combined-seconds", self.dur["combined"],
                 "--fullsoak-seconds", self.dur["fullsoak"], "--gfx-seconds", self.dur["gfx"],
                 "-o", self.results_dir]
        args += extra_runner_args(getattr(self, "tune", None))
        # disk phases on the internal SSD when it has been unlocked and mounted
        # (live session:  cryptsetup open /dev/sda2 cryptroot
        #                  mkdir -p /mnt/internal && mount /dev/mapper/cryptroot /mnt/internal)
        try:
            if os.path.ismount("/mnt/internal"):
                scratch = "/mnt/internal/bench-scratch"
                os.makedirs(scratch, exist_ok=True)
                args += ["--disk-path", scratch]
        except Exception:
            pass
        return args

    # ---------------------------------------------------------------- run ---
    def start(self, profile=None):
        if self.running:
            return
        if profile:
            self._apply_profile(profile)
        self.phases, self.last_phase, self.scroll, self.follow = [], {}, 0, True
        self.tel = {k: deque(maxlen=1000) for k in ("t", "temp", "fan", "power", "util")}
        self.tel_path, self.tel_cols, self.csv_last, self.tel_ncols = None, {}, None, 0
        self.exited = False
        self._stopped = False
        self.run_t0 = time.time()
        os.makedirs(self.results_dir, exist_ok=True)
        env = os.environ.copy()
        try:
            cmd, py = runner_command(self._args())
            if py:
                env.setdefault("BENCH_PY", py)
                env["PATH"] = os.path.dirname(py) + os.pathsep + env.get("PATH", "")
            self.proc = subprocess.Popen(cmd, cwd=REPO, env=env,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, bufsize=1, start_new_session=True,
                                         creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                                        if os.name == "nt" else 0))
        except Exception as exc:
            self._set("failed to start: %s" % exc, 4)
            return
        self.running = True
        self._set("running: %s" % PRESETS[self.preset][0], 2)
        if self.rep and self.rep.runner_id:
            self.tracker = PhaseTracker(self.rep)
            si = getattr(self, "sysinfo", None) or {}
            # record the effective profile (including tune knobs) so a locally
            # started run is reproducible from history
            prof = profile or {
                "preset": PRESETS[self.preset][0],
                "groups": {k: (1 if v else 0) for k, v in self.groups.items()},
                **self.dur, **(getattr(self, "tune", None) or {}),
            }
            self.rep.start_run(prof.get("label") or PRESETS[self.preset][0], prof,
                               gpu=si.get("gpu"), driver=si.get("driver"), system=si,
                               preset=prof.get("preset") or PRESETS[self.preset][0])
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _reader(self, proc):
        try:
            for line in proc.stdout:
                self.q.put(line)
        except Exception:
            pass
        self.q.put(None)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self._stopped = True
            try:
                _kill_tree(self.proc)
                self._set("stopping...", 3)
            except Exception:
                pass

    def teardown(self):
        """Kill any battery this dashboard started - on quit, Ctrl-C or crash.

        Runs from curses.wrapper's finally so a closed window never leaves the
        runner tree (and the LibreHardwareMonitor sampler) alive."""
        try:
            if self.proc and self.proc.poll() is None:
                _kill_tree(self.proc)
                for _ in range(20):
                    if self.proc.poll() is not None:
                        break
                    time.sleep(0.05)
        except Exception:
            pass
        try:
            if getattr(self, "pump", None):
                self.pump.stop()
        except Exception:
            pass

    def collect(self):
        if os.path.exists(RESULT_COLLECTOR):
            self._set("collecting to USB...", 2)

            def work():
                try:
                    r = subprocess.run([RESULT_COLLECTOR, self.results_dir], timeout=900,
                                       capture_output=True, text=True)
                    out = ((r.stdout or "") + (r.stderr or "")).strip()
                    try:
                        with open(os.path.join(self.results_dir, "collect.log"), "a") as fh:
                            fh.write(out + "\n")
                    except Exception:
                        pass
                    lines = [ln for ln in out.splitlines() if ln.strip()]
                    last = (lines[-1] if lines else "no output").replace("[collect] ", "")
                    if r.returncode == 0:
                        self.collected_ok = True
                        self._set("saved to USB: %s" % last[:64], 1)
                        if self._auto_upload_enabled():
                            self.upload()
                    else:
                        self._set("USB save FAILED - %s" % last[:52], 3)
                        # a failed save is exactly when the upload matters:
                        # send from RAM if upload credentials are configured
                        if self._auto_upload_enabled():
                            self.upload()
                except Exception as exc:
                    self._set("collect failed: %s" % exc, 3)
            threading.Thread(target=work, daemon=True).start()
        else:
            self._set("results kept in %s" % self.results_dir, 5)

    def _auto_upload_enabled(self):
        for c in ("/mnt/benchdata/bench-upload.conf", "/etc/bench-upload.conf"):
            try:
                with open(c) as fh:
                    for ln in fh:
                        if ln.strip().startswith("AUTO_UPLOAD=") and \
                           ln.split("=", 1)[1].strip().strip('"') == "1":
                            return True
            except Exception:
                pass
        return False

    def upload(self):
        if not os.path.exists("/usr/local/bin/bench-upload"):
            self._set("upload not available on this system", 3)
            return
        self._set("uploading to GitHub...", 2)

        def work():
            try:
                r = subprocess.run(["/usr/local/bin/bench-upload", self.results_dir],
                                   timeout=600, capture_output=True, text=True)
                out = ((r.stdout or "") + (r.stderr or "")).strip()
                try:
                    with open(os.path.join(self.results_dir, "upload.log"), "a") as fh:
                        fh.write(out + "\n")
                except Exception:
                    pass
                lines = [ln for ln in out.splitlines() if ln.strip()]
                last = lines[-1] if lines else "no output"
                self._set(last.replace("[upload] ", "")[:70], 1 if r.returncode == 0 else 3)
            except Exception as exc:
                self._set("upload failed: %s" % exc, 3)
        threading.Thread(target=work, daemon=True).start()

    def _set(self, text, attr=0):
        self.status = text
        self.status_attr = attr

    # --------------------------------------------------------- fan curve ----
    def _curve_state(self):
        try:
            with open(FAN_CONF) as fh:
                for line in fh:
                    if line.startswith("CURVE="):
                        return line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
        return None

    def toggle_curve(self):
        script = FAN_CURVE if os.path.exists(FAN_CURVE) else None
        if not script:
            self._set("fan_curve.py not found", 4)
            return
        on = not self.curve

        def work():
            try:
                if on:
                    # write the conf + apply now, then hand over to the resident
                    # daemon so the curve keeps following the temperature
                    subprocess.run([sys.executable, script, "set"], capture_output=True,
                                   text=True, timeout=90)
                    r = subprocess.run(["systemctl", "start", "bench-fan-curve.service"],
                                       capture_output=True, text=True, timeout=60)
                    msg = "fan curve ON - resident daemon following temps" if r.returncode == 0 \
                        else "fan curve ON (one-shot; daemon unavailable)"
                    rc = 0
                else:
                    subprocess.run(["systemctl", "stop", "bench-fan-curve.service"],
                                   capture_output=True, text=True, timeout=60)
                    r = subprocess.run([sys.executable, script, "off"], capture_output=True,
                                       text=True, timeout=90)
                    out = (r.stdout or r.stderr).strip().splitlines()
                    msg = "fan curve OFF - automatic fan control"
                    rc = r.returncode
                    if rc != 0 and out:
                        msg = out[-1][:60]
                self._set(msg, 1 if rc == 0 else 3)
            except Exception as exc:
                self._set("fan curve failed: %s" % exc, 3)
            self.curve = self._curve_state()
        threading.Thread(target=work, daemon=True).start()

    # ----------------------------------------------------------- plumbing ---
    def _drain(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item is None:
                    self._on_exit()
                else:
                    self._handle(str(item).rstrip("\n"))
        except queue.Empty:
            pass

    def _handle(self, line):
        self.log.append(line)
        if self.tracker:
            self.tracker.feed(line)
        if self.pump:
            self.pump.push_log(line)
        if line.startswith("[") and "]" in line:
            head = line[1:line.index("]")]
            rest = line[line.index("]") + 1:].strip()
            if head == "warn" and "/" in rest:
                grp = rest.split("/")[0]
                ph = self.last_phase.get(grp)
                if ph:
                    ph["status"] = "warn"
                    ph["detail"] = rest
            elif head in GROUPS:
                # phases run one at a time: a new phase closes out any phase still
                # marked running (including the previous group's last phase)
                for p in self.phases:
                    if p["status"] == "run":
                        p["status"] = "ok"
                        p["t1"] = time.time()
                ph = {"group": head, "label": rest, "status": "run", "t0": time.time(),
                      "t1": None, "detail": ""}
                self.phases.append(ph)
                self.last_phase[head] = ph
                self.follow = True
        elif self.phases and line.strip():
            self.phases[-1]["detail"] = line.strip()

    def _on_exit(self):
        if self.exited:
            return
        self.exited = True
        self.running = False
        rc = self.proc.poll() if self.proc else None
        for ph in self.phases:
            if ph["status"] == "run":
                ph["status"] = "ok"
                ph["t1"] = time.time()
        if self.tracker:
            self.tracker.close()
            self.tracker = None
        if self.rep and self.rep.run_id:
            try:
                summary = build_summary_dir(self.results_dir, since=self.run_t0)
                csvs = glob.glob(os.path.join(self.results_dir, "*.telemetry.csv"))
                if csvs:
                    ts = telemetry_summary(max(csvs, key=os.path.getmtime))
                    if ts:
                        summary["telemetry"] = ts
            except Exception as e:
                # never let a summary build failure pass for an empty summary: an
                # earlier NameError here was swallowed into {} and the run looked
                # like it had measured nothing
                summary = {"summary_error": "%s: %s" % (type(e).__name__, e)}
            si = getattr(self, "sysinfo", None) or {}
            inv = read_inventory(self.results_dir) or si
            upload_telemetry_csv(self.rep, self.results_dir, sent=self.tel_sent)
            upload_run_files(self.rep, self.results_dir)
            self.rep.finish("done" if rc == 0 else ("stopped" if self._stopped else "failed"),
                            rc or 0, summary, gpu=inv.get("gpu"), driver=inv.get("driver"),
                            system=inv)
        self._set("finished (exit %s) - c collect · r report" % rc, 1 if rc == 0 else 4)
        if os.path.exists(RESULT_COLLECTOR):
            self.collect()

    def _read_telemetry(self):
        # only this run's CSV: at run start the newest file is still the previous
        # run's, and reading it fed stale samples (with the old clock) into the run
        cutoff = (self.run_t0 or 0) - 1
        files = [f for f in glob.glob(os.path.join(self.results_dir, "*.telemetry.csv"))
                 if os.path.getmtime(f) >= cutoff]
        if not files:
            return
        newest = max(files, key=os.path.getmtime)
        if newest != self.tel_path:
            self.tel_path = newest
            self.tel_cols, self.csv_last, self.tel_ncols = {}, None, 0
            try:
                with open(newest) as fh:
                    self.tel_cols = {}
                    hdr = fh.readline().strip().split(",")
                    self.tel_ncols = len(hdr)
                    for i, n in enumerate(hdr):
                        canon = CSV_ALIASES.get(n.strip().lower(), n.strip().lower())
                        if canon and canon not in self.tel_cols:
                            self.tel_cols[canon] = i
            except OSError:
                return
        try:
            with open(self.tel_path) as fh:
                lines = fh.read().strip().splitlines()
            if len(lines) < 2:
                return
            # the sampler rewrites this file several times a second, so the final
            # line is often a partial row - that made every column read as None and
            # the GPU tiles show "--". Take the last COMPLETE row.
            vals = None
            for line in reversed(lines[1:]):
                parts = line.split(",")
                if len(parts) >= self.tel_ncols:
                    vals = parts
                    break
            if vals is None:
                return
        except OSError:
            return

        def get(name):
            i = self.tel_cols.get(name)
            if i is None or i >= len(vals):
                return None
            try:
                return float(vals[i])
            except ValueError:
                return None

        def raw(name):
            i = self.tel_cols.get(name)
            return vals[i].strip() if i is not None and i < len(vals) else None

        self.csv_now = {k: get(k) for k in
                        ("temp_c", "fan_pct", "power_w", "gpu_util", "mem_util",
                         "vram_used_mib", "sm_mhz", "mem_mhz", "gr_mhz",
                         "cpu_temp_c", "cpu_max_core_c", "cpu_fan_rpm", "gpu_fan_rpm",
                         "nvme_c", "gpu_enc_pct", "gpu_dec_pct", "temp_mem_c", "video_mhz",
                         "temp_limit_c", "power_limit_w", "throttle", "retired",
                         "pcie_gen", "pcie_width", "cpu_pkg_w", "cpu_mhz", "load1", "psi_io",
                         "disk_read_mbs", "disk_write_mbs", "net_rx_mbs", "net_tx_mbs")}
        self.csv_now["pstate"] = raw("pstate")
        # the CSV column is elapsed_s, canonicalised to 'elapsed' (asking for
        # 'elapsed_s' here silently disabled every telemetry sample the TUI sent)
        elapsed = get("elapsed")
        if elapsed is not None and elapsed != self.csv_last:
            self.csv_last = elapsed
            self.tel["t"].append(elapsed)
            for key, col in (("temp", "temp_c"), ("fan", "fan_pct"), ("power", "power_w"),
                             ("util", "gpu_util")):
                v = self.csv_now.get(col)
                if v is not None:
                    self.tel[key].append(v)
            if self.pump:
                # push every column we can read, not a fixed list: 'elapsed' is
                # what the charts plot against, and leaving it out made every
                # sample land with elapsed=NULL, so the web chart showed "no data"
                sample = {k: get(k) for k in self.tel_cols if get(k) is not None}
                # get() is float-only, so non-numeric columns (pstate) were dropped
                # on the floor - send them raw so nothing the sampler gathered is lost
                for k in ("pstate",):
                    if k not in sample:
                        rv = raw(k)
                        if rv:
                            sample[k] = rv
                if sample:
                    self.pump.push_sample(sample)
                    self.tel_sent.setdefault(
                        os.path.basename(self.tel_path or ""), set()).add(elapsed)

    def _sample_system(self):
        # psutil first: it is the only cross-platform source, and the dashboards
        # run under the venv that has it. Then /proc (Linux, stdlib-only), then the
        # Windows API. Without those Windows paths the CPU bar, the RAM/load line
        # and the per-core sparkline were simply blank on Windows.
        if self._sample_system_psutil():
            return
        try:
            with open("/proc/stat") as fh:
                stats = []
                for line in fh:
                    if not line.startswith("cpu"):
                        continue
                    vals = [int(x) for x in line.split()[1:]]
                    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
                    stats.append((idle, sum(vals)))
                    if len(stats) > 1 + 16:
                        break
            if self._prev_stat:
                def pct(p, c):
                    di, dt = c[0] - p[0], c[1] - p[1]
                    return 100.0 * (1.0 - di / dt) if dt > 0 else 0.0
                self.sys_cpu = pct(self._prev_stat[0], stats[0])
                self.sys_cores = [pct(self._prev_stat[1][i], stats[i + 1])
                                  for i in range(min(len(stats) - 1, len(self._prev_stat[1])))]
            self._prev_stat = (stats[0], stats[1:])
        except Exception:
            pass
        try:
            mem = {}
            with open("/proc/meminfo") as fh:
                for line in fh:
                    k, v = line.split(":", 1)
                    mem[k] = int(v.split()[0])
            total = mem.get("MemTotal", 0) / 1048576.0
            avail = mem.get("MemAvailable", 0) / 1048576.0
            st = mem.get("SwapTotal", 0) / 1048576.0
            sf = mem.get("SwapFree", 0) / 1048576.0
            self.sys_ram = (total - avail, total)
            self.sys_swap = (st - sf, st)
        except Exception:
            pass
        try:
            self.sys_load = os.getloadavg()[0]
        except Exception:
            pass
        self._sample_system_win()

    def _sample_system_psutil(self):
        """CPU/RAM/swap/load/per-core via psutil. False when it is unavailable."""
        try:
            import psutil
        except Exception:
            return False
        try:
            self.sys_cpu = psutil.cpu_percent(interval=None)
            cores = psutil.cpu_percent(interval=None, percpu=True)
            if cores:
                self.sys_cores = cores
            vm = psutil.virtual_memory()
            self.sys_ram = (vm.used / 1024 ** 3, vm.total / 1024 ** 3)
            sm = psutil.swap_memory()
            self.sys_swap = (sm.used / 1024 ** 3, sm.total / 1024 ** 3)
            try:
                self.sys_load = psutil.getloadavg()[0]
            except Exception:
                pass
            return True
        except Exception:
            return False

    def _sample_system_win(self):
        """CPU/RAM/swap via the Windows API, for a stdlib-only run without psutil."""
        if os.name != "nt":
            return
        try:
            import ctypes
            idle = ctypes.c_ulonglong()
            kern = ctypes.c_ulonglong()
            user = ctypes.c_ulonglong()
            if ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern),
                                                     ctypes.byref(user)):
                cur = (idle.value, kern.value, user.value)
                old = self._prev_win_cpu
                self._prev_win_cpu = cur
                if old:
                    di, dk, du = cur[0] - old[0], cur[1] - old[1], cur[2] - old[2]
                    tot = dk + du          # kernel time already includes idle
                    if tot > 0:
                        self.sys_cpu = round(100.0 * (tot - di) / tot, 1)
        except Exception:
            pass
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
                self.sys_ram = ((st.ullTotalPhys - st.ullAvailPhys) / 1024 ** 3,
                                st.ullTotalPhys / 1024 ** 3)
                # TotalPageFile - AvailPageFile is the commit charge, not swap;
                # _win_swap_gib reads the real page-file usage instead
                self.sys_swap = _win_swap_gib(st.ullTotalPageFile)
        except Exception:
            pass

    def _detect_hardware(self):
        """Header line: CPU · RAM · GPU (cross-platform, shared with the GUI)."""
        si = {}
        try:
            si = system_info() or {}
        except Exception:
            si = {}
        try:
            import gpu_util
            gpu_util.probe()
            if gpu_util.NAME and not si.get("gpu"):
                si["gpu"] = gpu_util.NAME
            if gpu_util.VRAM_TOTAL_MIB:
                self.vram_total = gpu_util.VRAM_TOTAL_MIB
        except Exception:
            pass
        if not self.vram_total:
            try:
                digits = re.sub(r"[^0-9]", "", str(si.get("vram") or ""))
                if digits:
                    self.vram_total = int(digits)
            except Exception:
                pass
        cpu = si.get("cpu")
        if cpu:
            # "Intel(R) Core(TM) i7-6800K CPU @ 3.40GHz" -> "Intel Core i7-6800K"
            cpu = re.sub(r"\((?:R|TM)\)", "", str(cpu))
            cpu = re.sub(r"\s*CPU\s*@.*$", "", cpu, flags=re.I).strip()
            si["cpu"] = re.sub(r"\s+", " ", cpu)
        self.hw = hardware_line(si) or "hardware detection unavailable"

    # -------------------------------------------------------------- demo ----
    def _demo_seed(self):
        if not self.demo:
            threading.Thread(target=self._detect_hardware, daemon=True).start()
            return
        self.hw = "Omarchy · i7-3770 · 15.6 GiB · NVIDIA GeForce GTX 970"
        now = time.time()
        demo = [("gpu", "info", 4), ("gpu", "matmul", 31), ("gpu", "membw", 22), ("gpu", "pcie", 24),
                ("gpu", "conv", 19), ("gpu", "integrity", 5), ("gpu", "soak", 601), ("cpu", "info", 2),
                ("cpu", "matmul", 13)]
        for i, (grp, label, dur) in enumerate(demo):
            status = "run" if i == len(demo) - 1 else "ok"
            t1 = now if status == "run" else now - (len(demo) - i) * 15
            self.phases.append({"group": grp, "label": label, "status": status,
                                "t0": t1 - dur, "t1": None if status == "run" else t1,
                                "detail": ("soak %.1fs matmuls=495 matmul_TFLOPS=1.8" % 601.0)
                                if label == "soak" else ""})
            self.last_phase[grp] = self.phases[-1]
        for i in range(200):
            phase = min(1.0, i / 60)
            self.tel["t"].append(i)
            self.tel["temp"].append(31 + 35 * phase + (i % 5) * 0.3)
            self.tel["power"].append(22 + 128 * phase + (i % 7) * 1.2)
            self.tel["fan"].append(27 + 32 * phase)
            self.tel["util"].append(min(100, 100 * phase))
        self.sys_cpu, self.sys_cores = 87.0, [91, 88, 90, 85, 92, 86, 89, 83]
        self.sys_ram, self.sys_swap, self.sys_load = (9.8, 15.6), (0.0, 0.0), 3.42
        self.csv_now = {"temp_c": 66, "fan_pct": 59, "power_w": 152, "gpu_util_pct": 100,
                        "mem_util_pct": 48, "vram_used_mib": 1337, "sm_mhz": 1189, "pstate": "P2",
                        "cpu_package_c": 54, "cpu_max_core_c": 58, "nvme_c": 41,
                        "cpu_fan_rpm": None, "gpu_fan_rpm": 2601}
        self.log.append("[gpu] soak")
        self.log.append("    soak 601.0s matmuls=495 convs=495 matmul_TFLOPS=1.8")
        self._set("demo mode - press s to run for real", 5)

    # -------------------------------------------------------------- draw ----
    def _put(self, y, x, text, attr=0, w=None):
        H, W = self.scr.getmaxyx()
        if y < 0 or y >= H or x >= W:
            return
        text = str(text)
        if w:
            text = text[:w]
        text = text[:max(0, W - x - 1)]
        try:
            self.scr.addstr(y, x, text, attr)
        except curses.error:
            pass

    def _box(self, y, x, h, w, title="", attr=0):
        if h < 2 or w < 4:
            return
        self._put(y, x, "┌" + "─" * (w - 2) + "┐", curses.color_pair(8) | curses.A_DIM, w)
        for i in range(1, h - 1):
            self._put(y + i, x, "│", curses.color_pair(8) | curses.A_DIM)
            self._put(y + i, x + w - 1, "│", curses.color_pair(8) | curses.A_DIM)
        self._put(y + h - 1, x, "└" + "─" * (w - 2) + "┘", curses.color_pair(8) | curses.A_DIM, w)
        if title:
            self._put(y, x + 2, " %s " % title, curses.color_pair(2) | curses.A_BOLD)

    def _bar(self, y, x, width, frac, label, value, attr):
        self._put(y, x, "%-6s" % label, curses.color_pair(8))
        if frac is None:              # no data (e.g. no GPU in a VM): no empty bar
            self._put(y, x + 9 + width, value)
            return
        frac = max(0.0, min(1.0, frac))
        filled = int(frac * width)
        self._put(y, x + 7, "█" * filled, curses.color_pair(attr))
        self._put(y, x + 7 + filled, "░" * (width - filled), curses.color_pair(8) | curses.A_DIM)
        self._put(y, x + 9 + width, value)

    def _spark(self, y, x, width, series, scale, attr):
        vals = list(series)[-width:]
        if not vals:
            return
        text = "".join(SPARK[min(7, int(v / scale * 7.99))] for v in vals)
        self._put(y, x, text, curses.color_pair(attr))

    @staticmethod
    def _severity(temp):
        if temp is None:
            return 8
        if temp >= 80:
            return 4
        if temp >= 70:
            return 3
        return 1

    def draw(self):
        self.scr.erase()
        H, W = self.scr.getmaxyx()
        if H < 20 or W < 74:
            self._put(0, 0, "terminal too small (need >= 74x20)", curses.color_pair(4))
            self.scr.refresh()
            return

        hdr = " bench-test v%s " % bench_version(REPO)
        self._put(0, 0, hdr, curses.color_pair(1) | curses.A_BOLD)
        self._put(0, len(hdr) + 1, self.hw[:W - 32], curses.color_pair(8))
        self._put(0, max(len(hdr) + 2, W - 10), time.strftime("%H:%M:%S"), curses.color_pair(2))
        self._put(1, 0, "─" * (W - 1), curses.color_pair(8) | curses.A_DIM, W - 1)

        LW = 34
        MX = LW + 1
        MW = W - MX - 1
        log_h = 7
        top = 2
        log_y = H - 2 - log_h
        left_h = log_y - top
        tel_h = min(16, max(8, left_h - 5))

        # left panel
        self._box(top, 0, left_h, LW, "run profile")
        for i, name in enumerate([p[0] for p in PRESETS]):
            mark = "▶" if i == self.preset else " "
            attr = curses.color_pair(2) | curses.A_BOLD if i == self.preset else curses.color_pair(8)
            self._put(top + 1 + i, 2, "%s %s" % (mark, name), attr)
        gy = top + 2 + len(PRESETS)
        self._put(gy, 2, "groups (%s toggle)" % _KEY_HINT, curses.color_pair(8))
        for i, g in enumerate(UI_GROUPS):
            row = gy + 1 + (i % 5)
            col = 2 + (i // 5) * 16
            on = self.groups[g]
            self._put(row, col, "%s%s %s" % (_GROUP_KEYS[i], "◉" if on else "○", g),
                      curses.color_pair(1) if on else curses.color_pair(8) | curses.A_DIM)
        dy = gy + 7
        self._put(dy, 2, "soaks: gpu %ss · comb %ss" % (self.dur["gpu"], self.dur["combined"]),
                  curses.color_pair(8))
        self._put(dy + 1, 2, "       full %ss · gfx %ss" % (self.dur["fullsoak"], self.dur["gfx"]),
                  curses.color_pair(8))
        ts = tune_summary(getattr(self, "tune", None))
        if ts and dy + 2 < top + left_h:
            self._put(dy + 2, 2, ("tune: " + ts)[:LW - 4], curses.color_pair(8))
        if dy + 3 < top + left_h:
            state = "on: %s" % self.curve if self.curve else "off (auto)"
            self._put(dy + 3, 2, "fan curve: %s" % state,
                      curses.color_pair(2) if self.curve else curses.color_pair(8) | curses.A_DIM)
            self._put(dy + 4, 2, "[f] toggle curve", curses.color_pair(8) | curses.A_DIM)

        # telemetry panel (nvtop/htop-style)
        self._box(top, MX, tel_h, MW, "gpu / system telemetry")
        ry = top + 1
        barw = max(10, MW - 40)
        t = self.csv_now.get("temp_c")
        fan = self.csv_now.get("fan_pct")
        rpm = self.csv_now.get("gpu_fan_rpm")
        pw = self.csv_now.get("power_w")
        util = self.csv_now.get("gpu_util")
        vram = self.csv_now.get("vram_used_mib")

        def room():
            return ry < top + tel_h - 1

        if room():
            self._bar(ry, MX + 2, barw, (t / 100) if t is not None else None, "temp", "%5s" % (
                ("%.0f °C" % t) if t is not None else "--"), self._severity(t)); ry += 1
        if room():
            fan_txt = ("%.0f %%" % fan) if fan is not None else "--"
            if rpm:
                fan_txt += " %.0frpm" % rpm
            self._bar(ry, MX + 2, barw, (fan / 100) if fan is not None else None, "fan", "%9s" % fan_txt, 1); ry += 1
        if room():
            self._bar(ry, MX + 2, barw, (pw / 250) if pw is not None else None, "power", "%5s" % (
                ("%.0f W" % pw) if pw is not None else "--"), 3); ry += 1
        if room():
            self._bar(ry, MX + 2, barw, (util / 100) if util is not None else None, "gpu", "%5s" % (
                ("%.0f %%" % util) if util is not None else "--"), 2); ry += 1
        if room():
            total = self.vram_total
            if total is None:
                try:
                    import gpu_util
                    total = gpu_util.VRAM_TOTAL_MIB
                except Exception:
                    total = None
            frac = (vram / total) if (vram and total) else 0
            vtxt = ("%.0f/%.0f MiB" % (vram, total)) if (vram and total) else "--"
            self._bar(ry, MX + 2, barw, frac, "vram", vtxt, 6); ry += 1
        if room():
            cbarw = max(10, barw - 30)
            self._bar(ry, MX + 2, cbarw, self.sys_cpu / 100, "cpu", "%3.0f %%" % self.sys_cpu, 1)
            ram, ram_t = self.sys_ram
            extra = "  ram %.1f/%.1f GiB · load %.2f" % (ram, ram_t, self.sys_load)
            if self.sys_swap[1] > 0:
                extra += " · swap %.1f GiB" % self.sys_swap[0]
            self._put(ry, MX + 2 + cbarw + 14, extra, curses.color_pair(8) | curses.A_DIM)
            ry += 1
        if room() and self.sys_cores:
            x = MX + 2
            for i, c in enumerate(self.sys_cores[:16]):
                bar = "▁▂▃▄▅▆▇█"[min(7, int(max(0.0, min(100.0, c)) / 14.3))]
                self._put(ry, x, "%2d%s%3.0f" % (i, bar, c), curses.color_pair(8))
                x += 7
            ry += 1
        if room():
            chunks = []
            if t is not None:
                chunks.append("gpu %.0f°" % t)
            if self.csv_now.get("cpu_temp_c") is not None:
                chunks.append("cpu %.0f°" % self.csv_now["cpu_temp_c"])
            if self.csv_now.get("cpu_max_core_c") is not None:
                chunks.append("core-max %.0f°" % self.csv_now["cpu_max_core_c"])
            if self.csv_now.get("nvme_c") is not None:
                chunks.append("nvme %.0f°" % self.csv_now["nvme_c"])
            if self.csv_now.get("cpu_fan_rpm"):
                chunks.append("cpufan %.0frpm" % self.csv_now["cpu_fan_rpm"])
            enc, dec = self.csv_now.get("gpu_enc_pct"), self.csv_now.get("gpu_dec_pct")
            if enc is not None or dec is not None:
                chunks.append("enc/dec %s/%s%%" % (
                    ("%.0f" % enc) if enc is not None else "--",
                    ("%.0f" % dec) if dec is not None else "--"))
            self._put(ry, MX + 2, "temps  " + " · ".join(chunks), curses.color_pair(
                self._severity(max([v for v in (t, self.csv_now.get("cpu_temp_c"),
                                                self.csv_now.get("nvme_c")) if v is not None] or [0]))))
            ry += 1
        if room():
            clk = self.csv_now.get("sm_mhz")
            self._put(ry, MX + 2, "clock %s MHz · %s" % (
                ("%.0f" % clk) if clk is not None else "--", self.csv_now.get("pstate") or ""),
                curses.color_pair(8)); ry += 1
        if room():
            h, hot = [], False
            if self.csv_now.get("throttle") is not None:
                if self.csv_now["throttle"]:
                    h.append("THROTTLED"); hot = True
                else:
                    h.append("no throttle")
            if self.csv_now.get("temp_limit_c") is not None:
                h.append("limit %.0f°" % self.csv_now["temp_limit_c"])
            if self.csv_now.get("power_limit_w") is not None:
                h.append("cap %.0fW" % self.csv_now["power_limit_w"])
            if self.csv_now.get("cpu_pkg_w") is not None:
                h.append("cpu %.0fW" % self.csv_now["cpu_pkg_w"])
            if self.csv_now.get("pcie_gen") and self.csv_now.get("pcie_width"):
                h.append("pcie %.0fx%.0f" % (self.csv_now["pcie_gen"], self.csv_now["pcie_width"]))
            if self.csv_now.get("video_mhz") is not None:
                h.append("nvenc %.0f" % self.csv_now["video_mhz"])
            if self.csv_now.get("retired"):
                h.append("retired pages!"); hot = True
            if h:
                pair = 4 if hot else 8            # red when something is wrong
                self._put(ry, MX + 2, "health " + " · ".join(h),
                          curses.color_pair(pair) | (curses.A_BOLD if hot else curses.A_DIM))
                ry += 1
        if room():
            # whatever else the shared spec defines, so new sampler columns show
            # up here without touching the layout (disk/net rates, CPU MHz, PSI…)
            bits = []
            for k, lbl, dec in DASH_FIELDS:
                if k in _TUI_SHOWN:
                    continue
                v = self.csv_now.get(k)
                if v in (None, ""):
                    continue
                bits.append("%s %s" % (lbl.lower(),
                                       ("%.*f" % (dec, v)) if dec != "-" else str(v)))
                if len(bits) >= 4:
                    break
            if bits:
                self._put(ry, MX + 2, " · ".join(bits), curses.color_pair(8) | curses.A_DIM)
                ry += 1
        if room():
            self._spark(ry, MX + 2, barw, self.tel["temp"], 100.0, 4)
            self._put(ry, MX + barw + 4, "temp hist", curses.color_pair(8) | curses.A_DIM); ry += 1
        if room():
            self._spark(ry, MX + 2, barw, self.tel["power"], 250.0, 3)
            self._put(ry, MX + barw + 4, "pwr hist", curses.color_pair(8) | curses.A_DIM); ry += 1
        if room():
            self._spark(ry, MX + 2, barw, self.tel["util"], 100.0, 2)
            self._put(ry, MX + barw + 4, "gpu hist", curses.color_pair(8) | curses.A_DIM); ry += 1
        if room():
            self._put(ry, MX + 2, self.status[:MW - 4], curses.color_pair(self.status_attr or 5))

        # phases
        ph_h = left_h - tel_h
        if ph_h >= 3:
            self._box(top + tel_h, MX, ph_h, MW, "phases")
            for i, ph in enumerate(self.phases[-(ph_h - 2):]):
                y = top + tel_h + 1 + i
                icon, attr = {"run": ("▶", 2), "ok": ("✓", 1), "warn": ("!", 3)}.get(ph["status"], ("·", 8))
                elapsed = (ph["t1"] or time.time()) - ph["t0"]
                txt = "%-9s %-22s %s" % (ph["group"], ph["label"][:22],
                                         "%d:%02d" % (elapsed // 60, elapsed % 60))
                self._put(y, MX + 2, "%s %s" % (icon, txt[:MW - 4]), curses.color_pair(attr))
                if ph.get("detail") and MW >= 84:
                    self._put(y, MX + MW - 30, ph["detail"][-28:].rjust(28), curses.color_pair(8))

        # log tail
        lines = list(self.log)
        if self.follow:
            view = lines[-(log_h - 2):]
        else:
            start = max(0, len(lines) - (log_h - 2) - self.scroll)
            view = lines[start:start + log_h - 2]
        self._box(log_y, 0, log_h, W, "log")
        for i, line in enumerate(view):
            self._put(log_y + 1 + i, 2, line[:W - 4], curses.color_pair(8))

        keys = (" s start · x stop · c collect · u upload · f fan-curve · p profile · "
                "1-0 groups · r report · G gui · t shell · PgUp/PgDn · q quit ")
        self._put(H - 1, 0, keys.ljust(W - 1)[:W - 1], curses.color_pair(2) | curses.A_REVERSE)
        self.scr.refresh()

    # --------------------------------------------------------- report view --
    def _show_report(self):
        files = sorted(glob.glob(os.path.join(self.results_dir, "*-linux-*.json")),
                       key=os.path.getmtime)
        if not files:
            self._set("no report yet", 3)
            return
        md = files[-1][:-5] + ".md"
        try:
            if os.path.exists(md):
                with open(md, errors="replace") as fh:
                    lines = fh.read().splitlines()
            else:
                with open(files[-1], errors="replace") as fh:
                    lines = json.dumps(json.load(fh), indent=2).splitlines()
        except Exception as exc:
            self._set("could not load report: %s" % exc, 3)
            return
        offset = 0
        while True:
            self.scr.erase()
            H, W = self.scr.getmaxyx()
            self._box(0, 0, H - 1, W, os.path.basename(files[-1]))
            for i in range(H - 3):
                if offset + i < len(lines):
                    self._put(1 + i, 2, lines[offset + i][:W - 4], curses.color_pair(8))
            self._put(H - 1, 0, " PgUp/PgDn scroll · any other key close ".ljust(W - 1)[:W - 1],
                      curses.color_pair(2) | curses.A_REVERSE)
            self.scr.refresh()
            k = self.scr.getch()
            if k == -1:
                continue  # input timeout: keep showing the report
            if k == curses.KEY_RESIZE:
                continue
            if k == curses.KEY_PPAGE:
                offset = max(0, offset - 5)
            elif k == curses.KEY_NPAGE:
                offset = min(max(0, len(lines) - 1), offset + 5)
            elif k == curses.KEY_UP:
                offset = max(0, offset - 1)
            elif k == curses.KEY_DOWN:
                offset += 1
            else:
                return

    # -------------------------------------------------------------- keys ----
    def key(self, ch):
        if ch in (ord("q"), ord("Q")):
            if self.running:
                # closing cancels an in-flight run so it never gets stuck
                self._set("stopping run before exit...", 3)
                self.stop()
                for _ in range(30):
                    if self.proc and self.proc.poll() is not None:
                        break
                    time.sleep(0.1)
                try:
                    self._on_exit()
                except Exception:
                    pass
            # never exit with uncollected results: if the auto-collect has not
            # succeeded yet, do a final synchronous attempt before quitting
            if os.path.exists(RESULT_COLLECTOR) and not self.collected_ok:
                self._set("saving results to USB before exit...", 2)
                try:
                    r = subprocess.run([RESULT_COLLECTOR, self.results_dir],
                                       timeout=60, capture_output=True, text=True)
                    self.collected_ok = (r.returncode == 0)
                    lines = [ln for ln in ((r.stdout or "") + (r.stderr or "")).splitlines() if ln.strip()]
                    if lines:
                        self._set(lines[-1].replace("[collect] ", "")[:72],
                                  1 if self.collected_ok else 3)
                except Exception:
                    pass
            if self.pump:
                self.pump.stop()
            return False
        if ch == ord("s"):
            self.start()
        elif ch == ord("x"):
            self.stop()
        elif ch == ord("c"):
            self.collect()
        elif ch == ord("u"):
            self.upload()
        elif ch == ord("f"):
            self.toggle_curve()
        elif ch == ord("r"):
            self._show_report()
        elif ch == ord("p") and not self.running:
            self._apply_preset((self.preset + 1) % len(PRESETS))
            self._set("preset: %s" % PRESETS[self.preset][0], 2)
        elif not self.running and chr(ch) in _GROUP_KEYS:
            g = UI_GROUPS[_GROUP_KEYS.index(chr(ch))]
            self.groups[g] = not self.groups[g]
            self._set("%s: %s" % (g, "on" if self.groups[g] else "off"), 2)
        elif ch == curses.KEY_PPAGE:
            self.follow = False
            self.scroll = min(max(0, len(self.log) - 1), self.scroll + 5)
        elif ch == curses.KEY_NPAGE:
            self.scroll = max(0, self.scroll - 5)
            self.follow = self.scroll == 0
        elif ch == ord("G"):
            self.shell([GUI_LAUNCHER] if os.path.exists(GUI_LAUNCHER)
                       else [sys.executable, os.path.join(BENCH, "gui.py")])
        elif ch == ord("t"):
            self.shell([os.environ.get("SHELL", "/bin/bash")])
        return True

    def shell(self, cmd):
        curses.endwin()
        try:
            subprocess.call(cmd)
        except Exception:
            pass
        # full repaint after anything external (shell, GUI, VT switches) may
        # have written to the console
        self.scr.clearok(True)
        self.scr.refresh()
        self.scr.timeout(250)

    def run(self):
        curses.curs_set(0)
        self.scr.keypad(True)  # arrows/PgUp/PgDn as KEY_* in all views
        self.scr.timeout(250)
        deadline = time.time() + self.exit_after if self.exit_after else None
        frame = 0
        while True:
            self._drain()
            try:
                while True:
                    self._apply_remote(self._pending.get_nowait())
            except queue.Empty:
                pass
            self._read_telemetry()
            self._sample_system()
            try:
                import gpu_util
                self.gpu = gpu_util.sample() or {}
            except Exception:
                self.gpu = {}
            frame += 1
            if frame % 8 == 0:
                # pick up curve state changes made outside the TUI (the boot
                # auto-enable service, or a manual bench-fan-curve run)
                self.curve = self._curve_state()
            if frame % 40 == 0:
                # self-heal: something wrote outside curses (kernel/VT) and the
                # screen model desynced; force a full repaint occasionally
                self.scr.clearok(True)
            try:
                self.draw()
            except Exception as exc:
                self.log.append("tui draw: %s" % exc)
                self.scr.clearok(True)
            if deadline and time.time() > deadline:
                return
            try:
                ch = self.scr.getch()
                if ch == -1:
                    continue
                if ch == curses.KEY_RESIZE:
                    curses.resize_term(0, 0)
                    self.scr.clearok(True)
                    continue
                if not self.key(ch):
                    return
            except Exception as exc:
                self.log.append("tui: %s" % exc)
                self.scr.clearok(True)


def main():
    if "drop_stray_script_arg" in globals():     # absent on an older toolkit
        sys.argv = drop_stray_script_arg(me=__file__)
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-dir",
                    default=os.path.join(persist_root() if is_live() else REPO, "results"))
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--exit-after", type=float, default=0)
    ap.add_argument("--server", default=load_conf()[0],
                    help="benchd control-plane URL (report + remote control)")
    ap.add_argument("--token", default=load_conf()[1], help="control-plane token")
    ap.add_argument("--name", default="", help="runner name reported to the control plane")
    ap.add_argument("--instance", default="",
                    help="instance label (e.g. tty1) when several dashboards share a host")
    a = ap.parse_args()

    def wrapped(scr):
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_CYAN, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_RED, -1)
        curses.init_pair(5, curses.COLOR_WHITE, -1)
        curses.init_pair(6, curses.COLOR_MAGENTA, -1)
        curses.init_pair(7, curses.COLOR_BLUE, -1)
        curses.init_pair(8, curses.COLOR_WHITE, -1)
        app = BenchTUI(scr, {"results_dir": a.results_dir, "demo": a.demo, "exit_after": a.exit_after,
                             "server": a.server, "token": a.token, "name": a.name,
                             "instance": a.instance})
        try:
            app.run()
        except KeyboardInterrupt:
            pass
        finally:
            # whatever the exit path (q, Ctrl-C, crash), never leave the run tree
            try:
                app.teardown()
            except Exception:
                pass

    curses.wrapper(wrapped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
