#!/usr/bin/env python3
"""agent.py - connect a bench-test machine to a benchd control plane.

Registers the machine, heartbeats, runs the existing battery on command from the
server, streams live phases + telemetry, and uploads the finished report.  Also
carries out update / fan-curve / power-limit commands.

    python bench/agent.py --server http://lab:8722 --token XXX
    python bench/agent.py --server http://lab:8722 --token XXX --self-test

Windows uses bench/run_win.py (wrapping Run-WorkstationBench.ps1); Linux uses
bench/Run-LinuxBench.sh.  Stdlib only.
"""
import argparse
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

BENCH = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(BENCH)
sys.path.insert(0, BENCH)

from benchreport import (Reporter, PhaseTracker, TelemetryTail,  # noqa: E402
                         build_summary, read_runs_index, load_identity)

DEFAULT_GROUPS = ["gpu", "cpu", "ram", "disk", "combined", "fullsoak", "net", "nvenc", "sys", "gfx"]


def run_hidden(cmd, **kw):
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    kw.setdefault("creationflags", flags)
    return subprocess.run(cmd, **kw)


def probe_capabilities():
    caps = {"os": platform.system(), "groups": list(DEFAULT_GROUPS),
            "platform": platform.platform(), "python": platform.python_version()}
    try:
        out = run_hidden(["nvidia-smi", "--query-gpu=name,driver_version,memory.total",
                          "--format=csv,noheader"], capture_output=True, text=True, timeout=10).stdout.strip()
        if out:
            parts = [p.strip() for p in out.splitlines()[0].split(",")]
            caps["gpu"] = parts[0]
            caps["driver"] = parts[1] if len(parts) > 1 else None
    except Exception:
        pass
    if os.name != "nt":
        try:
            caps["kernel"] = platform.release()
        except Exception:
            pass
    return caps


def git_commit():
    try:
        return subprocess.run(["git", "-C", REPO, "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return None


def build_runner_args(profile):
    """profile -> argv for run_win.py / Run-LinuxBench.sh."""
    args = []
    for g in (profile.get("skip") or []):
        args += ["--skip-" + g]
    dur = {"gpu_soak": "gpu-soak", "combined": "combined-seconds",
           "fullsoak": "fullsoak-seconds", "gfx": "gfx-seconds"}
    for k, flag in dur.items():
        if profile.get(k) not in (None, ""):
            args += ["--" + flag, str(profile[k])]
    return args


class Agent:
    def __init__(self, a):
        self.a = a
        self.out_dir = os.path.abspath(a.out)
        ident = load_identity(a.server)
        self.rep = Reporter(a.server, a.token, name=a.name, kind="agent",
                            version=git_commit(), git=git_commit(), log=self.log,
                            runner_id=ident.get("runner_id"), api_key=ident.get("api_key"),
                            persist=True)
        self.caps = probe_capabilities()
        self.proc = None
        self.run_label = None
        self.stop_flag = False
        self.running = False

    def log(self, msg):
        print("[agent] %s" % msg, flush=True)

    # -- runner ------------------------------------------------------------
    def runner_cmd(self, args):
        if os.name == "nt":
            return [sys.executable, os.path.join(BENCH, "run_win.py")] + args + ["-o", self.out_dir]
        return ["bash", os.path.join(BENCH, "Run-LinuxBench.sh")] + args + ["-o", self.out_dir]

    def run_battery(self, label, profile):
        args = build_runner_args(profile)
        cmd = self.runner_cmd(args)
        self.log("run: %s" % " ".join(cmd))
        env = os.environ.copy()
        env.setdefault("BENCH_PY", sys.executable)
        env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
        os.makedirs(self.out_dir, exist_ok=True)
        self.rep.start_run(label, profile, host=self.a.name or platform.node(),
                           platform=self.caps.get("platform"), gpu=self.caps.get("gpu"),
                           driver=self.caps.get("driver"))
        self.rep.event("run started: %s" % label)
        self.proc = subprocess.Popen(cmd, cwd=REPO, env=env, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, bufsize=1,
                                     creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                                    if os.name == "nt" else 0))
        self.running = True
        tracker = PhaseTracker(self.rep)
        tail = TelemetryTail(self.out_dir)
        line_q = queue.Queue()

        def reader():
            try:
                for ln in self.proc.stdout:
                    line_q.put(ln.rstrip("\n"))
            except Exception:
                pass
            line_q.put(None)
        threading.Thread(target=reader, daemon=True).start()

        last_hb = 0.0
        pending = []
        while True:
            try:
                ln = line_q.get(timeout=0.5)
            except queue.Empty:
                ln = ""
            if ln is None:
                break
            if ln:
                tracker.feed(ln)
                pending.append(ln)
                if len(pending) >= 40:
                    self.rep.event("\n".join(pending)); pending = []
            for row in tail.read():
                self.rep.telemetry([row])
            now = time.time()
            if now - last_hb >= 5:
                last_hb = now
                self.rep.heartbeat(status="busy")
        rc = self.proc.wait()
        self.running = False
        tracker.close()
        tail.close()
        if pending:
            self.rep.event("\n".join(pending))
        self._finish(rc)

    def _finish(self, rc):
        summary = read_runs_index(self.out_dir)
        if not summary:
            from benchreport import newest_report
            rep_path = newest_report(self.out_dir)
            summary = build_summary(rep_path) if rep_path else {}
        status = "done" if rc == 0 else ("stopped" if self.stop_flag else "failed")
        self.rep.finish(status, rc, summary, platform=self.caps.get("platform"),
                        gpu=self.caps.get("gpu"), driver=self.caps.get("driver"))
        self.rep.heartbeat(status="idle", current_run=None)
        self.log("run finished rc=%s status=%s" % (rc, status))
        self.stop_flag = False

    def stop_battery(self):
        if not self.proc or self.proc.poll() is not None:
            return
        self.stop_flag = True
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                               capture_output=True)
            else:
                self.proc.terminate()
        except Exception:
            pass

    # -- control -----------------------------------------------------------
    def handle(self, cmd):
        kind = cmd.get("kind")
        payload = cmd.get("payload") or {}
        cid = cmd.get("id")
        self.log("command: %s %s" % (kind, payload))
        result = {}
        try:
            if kind == "start":
                if self.running:
                    result = {"skipped": "already running"}
                else:
                    threading.Thread(target=self.run_battery,
                                     args=(payload.get("label") or payload.get("preset") or "remote run",
                                           payload), daemon=True).start()
                    result = {"started": True}
            elif kind == "stop":
                self.stop_battery()
                result = {"stopped": True}
            elif kind == "update":
                result = self.do_update()
            elif kind == "fan_curve":
                result = self.do_fan_curve(payload)
            elif kind == "power_limit":
                result = self.do_power_limit(payload)
            elif kind == "restart":
                result = {"restart": "scheduled"}
                self.rep.ack(cid, "done", result)
                time.sleep(0.5)
                os.execv(sys.executable, [sys.executable] + sys.argv)
            else:
                result = {"error": "unknown kind"}
        except Exception as e:
            self.rep.ack(cid, "failed", {"error": str(e)})
            return
        self.rep.ack(cid, "done", result)

    def do_update(self):
        if not os.path.isdir(os.path.join(REPO, ".git")):
            return {"error": "not a git checkout"}
        r = subprocess.run(["git", "-C", REPO, "pull", "--ff-only"],
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout or "") + (r.stderr or "")
        return {"rc": r.returncode, "commit": git_commit(), "out": out.strip()[-2000:]}

    def do_fan_curve(self, payload):
        on = bool(payload.get("on", True))
        if os.name == "nt":
            script = os.path.join(BENCH, "Install-GpuFanCurve.ps1")
            args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script]
            args += ["-Uninstall"] if not on else []
        else:
            script = os.path.join(BENCH, "fan_curve.py")
            args = [sys.executable, script, "set" if on else "off"]
        r = run_hidden(args, capture_output=True, text=True, timeout=120)
        return {"rc": r.returncode, "out": (r.stdout or r.stderr).strip()[-1000:]}

    def do_power_limit(self, payload):
        watts = payload.get("watts")
        if os.name == "nt":
            script = os.path.join(BENCH, "Set-GpuPowerLimit.ps1")
            args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script]
            args += ["-Info"] if watts is None else ["-Watts", str(watts)]
        else:
            args = ["nvidia-smi"] + (["-q", "-d", "POWER"] if watts is None
                                     else ["-pl", str(watts)])
        r = run_hidden(args, capture_output=True, text=True, timeout=120)
        return {"rc": r.returncode, "out": (r.stdout or r.stderr).strip()[-1000:]}

    # -- main --------------------------------------------------------------
    def loop(self):
        while not self.rep.register(self.caps):
            time.sleep(5)
        self.log("online; polling %s" % self.a.server)
        last_hb = 0.0
        while True:
            try:
                cmds = self.rep.poll_commands()
                for c in cmds:
                    self.handle(c)
                now = time.time()
                if now - last_hb >= self.a.poll:
                    last_hb = now
                    self.rep.heartbeat(status="busy" if self.running else "idle",
                                       capabilities=self.caps)
            except KeyboardInterrupt:
                self.stop_battery()
                return
            except Exception as e:
                self.log("loop error: %s" % e)
            time.sleep(self.a.poll)


def self_test(a):
    """Register, run a tiny no-op 'run', stream a phase + telemetry, finish."""
    rep = Reporter(a.server, a.token, name=a.name, kind="agent", version="selftest")
    if not rep.register({"self_test": True}):
        print("SELF-TEST: register failed")
        return 1
    rep.start_run("self-test", {"note": "no battery"})
    tr = PhaseTracker(rep)
    tr.feed("[sys] self-test inventory")
    time.sleep(0.3)
    tr.feed("[gpu] self-test soak")
    for i in range(5):
        rep.telemetry([[float(i), 50 + i, 40.0, 200.0, 90.0, 1800.0, 20.0, 4.0, 2700.0, 3.0]])
        time.sleep(0.2)
    tr.close()
    rep.finish("done", 0, {"host": a.name, "self_test": True, "gpu_soak_tflops": 12.3})
    print("SELF-TEST: ok")
    return 0


def main():
    ap = argparse.ArgumentParser(description="bench-test control-plane agent")
    ap.add_argument("--server", default=os.environ.get("BENCHD_SERVER", ""))
    ap.add_argument("--token", default=os.environ.get("BENCHD_TOKEN", ""))
    ap.add_argument("--name", default=platform.node())
    ap.add_argument("--out", default=os.path.join(REPO, "results"))
    ap.add_argument("--poll", type=float, default=2.0)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if not a.server or not a.token:
        print("agent: --server and --token are required (or BENCHD_SERVER/BENCHD_TOKEN)")
        return 2
    if a.self_test:
        return self_test(a)
    Agent(a).loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
