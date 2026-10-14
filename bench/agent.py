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
import glob
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


def _repo_dir():
    cands = [os.environ.get("BENCH_REPO")]      # explicit override wins
    if getattr(sys, "frozen", False):
        cands.append(os.path.dirname(os.path.abspath(sys.executable)))
    cands.append(os.getcwd())
    for c in cands:
        if c and os.path.isdir(os.path.join(c, "bench")):
            return os.path.abspath(c)
    return os.path.dirname(BENCH)


REPO = _repo_dir()
BENCH = os.path.join(REPO, "bench")
sys.path.insert(0, BENCH)

from benchreport import (Reporter, PhaseTracker, TelemetryTail,  # noqa: E402
                         build_summary, build_summary_dir, telemetry_summary, read_runs_index,
                         load_identity, run_exec, system_info, read_inventory, load_conf,
                         power_action, supported_groups, group_keys,
                         update_toolkit, iso_fetch, iso_write, iso_write_slot,
                         self_update, restart_self,
                         toolkit_version, is_live, update_needs_restart,
                         auto_update, venv_python, upload_telemetry_csv, upload_run_files,
                         extra_runner_args, expand_preset, apply_sampler_liveness,
                         version_string, drop_stray_script_arg, work_dir)
import bootstrap  # stdlib-only local env helper (see bench/bootstrap.py)

# Derived from the benchreport registry (EVOL-P2-001), not a second copy.
DEFAULT_GROUPS = list(group_keys())


def run_hidden(cmd, **kw):
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    kw.setdefault("creationflags", flags)
    return subprocess.run(cmd, **kw)


def probe_capabilities():
    caps = {"os": platform.system(), "groups": supported_groups(),
            "platform": platform.platform(), "python": platform.python_version(),
            "toolkit": toolkit_version(REPO), "live": is_live()}
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
    """profile -> argv for run_win.py / Run-LinuxBench.sh.

    A recognised preset name is expanded first (groups + durations + tune, see
    benchreport.expand_preset), so a name-only payload runs the preset it asked
    for instead of the runner's long defaults.
    """
    profile = expand_preset(profile)
    args = []
    for g in (profile.get("skip") or []):
        args += ["--skip-" + g]
    dur = {"gpu_soak": "gpu-soak", "combined": "combined-seconds",
           "fullsoak": "fullsoak-seconds", "gfx": "gfx-seconds"}
    for k, flag in dur.items():
        if profile.get(k) not in (None, ""):
            args += ["--" + flag, str(profile[k])]
    args += extra_runner_args(profile)
    return args


class Agent:
    def __init__(self, a):
        self.a = a
        self.out_dir = os.path.abspath(a.out)
        ident = load_identity(a.server)
        self.rep = Reporter(a.server, a.token, name=a.name, kind="agent",
                            version=version_string(), git=git_commit(), log=self.log,
                            runner_id=ident.get("runner_id"), api_key=ident.get("api_key"),
                            persist=True)
        self.caps = probe_capabilities()
        self.run_t0 = None
        self.sysinfo = system_info()
        self.proc = None
        self.run_label = None
        self.stop_flag = False
        self.running = False
        # guards the single battery slot: self.running is only set once the child
        # is up, so two quick 'start' commands used to both pass the check
        self._start_lock = threading.Lock()
        self._claimed = False

    def log(self, msg):
        print("[agent] %s" % msg, flush=True)

    # -- runner ------------------------------------------------------------
    def runner_cmd(self, args):
        if os.name == "nt":
            # bootstrap provisions a cached interpreter + numpy/psutil for machines
            # without a checkout/.venv (the packaged app has no python otherwise).
            self._py = bootstrap.python_path(REPO, ensure=True, log=self.log) or sys.executable
            return [self._py, os.path.join(BENCH, "run_win.py")] + args + ["-o", self.out_dir]
        return ["bash", os.path.join(BENCH, "Run-LinuxBench.sh")] + args + ["-o", self.out_dir]

    def run_battery(self, label, profile, extra_args=None):
        # expand once here too: start_run must record the effective profile (the
        # preset's groups/durations/tune), not the raw name-only payload
        profile = expand_preset(profile)
        args = build_runner_args(profile) + list(extra_args or [])
        cmd = self.runner_cmd(args)
        self.log("run: %s" % " ".join(cmd))
        env = os.environ.copy()
        env.setdefault("BENCH_PY", getattr(self, "_py", "") or venv_python(REPO) or sys.executable)
        env["PATH"] = os.path.dirname(env["BENCH_PY"]) + os.pathsep + env.get("PATH", "")
        os.makedirs(self.out_dir, exist_ok=True)
        self.rep.start_run(label, profile, host=self.a.name or platform.node(),
                           platform=self.caps.get("platform"), gpu=self.caps.get("gpu"),
                           driver=self.caps.get("driver"), system=self.sysinfo,
                           preset=(profile or {}).get("preset") or label)
        self.run_t0 = time.time()
        self.rep.event("run started: %s" % label)
        self.proc = subprocess.Popen(cmd, cwd=work_dir(), env=env, stdout=subprocess.PIPE,
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
        logbuf = []
        # The same pipeline counters the dashboards get from ReporterPump:
        # produced counts rows read from the CSV; sent only what the server
        # ACKNOWLEDGED (a failed POST used to be counted as sent); failed what it
        # rejected. dropped stays 0: tailing a file has no bounded queue.
        tel = {"produced": 0, "sent": 0, "failed": 0, "dropped": 0}
        hbs = 0
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
                logbuf.append(ln)
                if len(pending) >= 40:
                    self.rep.event("\n".join(pending)); pending = []
                if len(logbuf) >= 40:
                    self.rep.log(logbuf); logbuf = []
            rows = tail.read()
            if rows:
                tel["produced"] += len(rows)
                # batch per tail.read() (<=200 per POST), like ReporterPump's
                # <=120 per tick: one POST per row was 4 req/s with a fresh
                # connection each, and a slow server blocked the run loop up to
                # its 6s timeout for every single row
                for i in range(0, len(rows), 200):
                    batch = rows[i:i + 200]
                    if self.rep.telemetry(batch) == 200:
                        tel["sent"] += len(batch)
                    else:
                        tel["failed"] += len(batch)
            now = time.time()
            if now - last_hb >= 5:
                last_hb = now
                if logbuf:
                    self.rep.log(logbuf); logbuf = []
                self.rep.heartbeat(status="busy")
                hbs += 1
                if hbs % 5 == 0 and (tel["produced"] or tel["failed"]):
                    self.log("[agent] telemetry %d produced, %d sent, %d failed, "
                             "%d dropped"
                             % (tel["produced"], tel["sent"], tel["failed"], tel["dropped"]))
        rc = self.proc.wait()
        self.running = False
        tracker.close()
        tail.close()
        if logbuf:
            self.rep.log(logbuf)
        if pending:
            self.rep.event("\n".join(pending))
        if tel["produced"] or tel["failed"]:
            self.log("[agent] telemetry %d produced, %d sent, %d failed, %d dropped"
                     % (tel["produced"], tel["sent"], tel["failed"], tel["dropped"]))
        self._finish(rc)

    def _finish(self, rc):
        summary = read_runs_index(self.out_dir)
        if not summary:
            summary = build_summary_dir(self.out_dir, since=self.run_t0, log=self.log)
            # only a CSV THIS run wrote: at run end the newest file can still be
            # the previous run's, which would give this run a stale telemetry block
            try:
                csvs = [p for p in glob.glob(os.path.join(self.out_dir, "*.telemetry.csv"))
                        if self.run_t0 is None or os.path.getmtime(p) >= self.run_t0 - 5]
            except OSError:
                csvs = []
            if csvs:
                ts = telemetry_summary(max(csvs, key=os.path.getmtime))
                if ts:
                    summary["telemetry"] = ts
        else:
            # the runner's own index row skips build_summary_dir, so apply the
            # sampler-liveness finding here too (warn only, never fails the run)
            apply_sampler_liveness(summary, self.out_dir, since=self.run_t0, log=self.log)
        summary.setdefault("host", self.a.name)
        status = "done" if rc == 0 else ("stopped" if self.stop_flag else "failed")
        system = read_inventory(self.out_dir) or self.sysinfo
        upload_telemetry_csv(self.rep, self.out_dir, since=self.run_t0)
        upload_run_files(self.rep, self.out_dir)
        self.rep.finish(status, rc, summary, platform=self.caps.get("platform"),
                        gpu=self.caps.get("gpu"), driver=self.caps.get("driver"),
                        system=system)
        self.rep.heartbeat(status="idle", current_run=None)
        self.log("run finished rc=%s status=%s" % (rc, status))
        self.stop_flag = False

    def _claim_start(self):
        """Atomically claim the single battery slot.

        ``self.running`` is only set once the child process is up, so two 'start'
        commands handled back to back both saw False and launched two batteries
        on one machine (a TOCTOU race). The claim closes that window: a second
        start is rejected until the first battery finishes.
        """
        with self._start_lock:
            if self.running or self._claimed:
                return False
            self._claimed = True
            return True

    def _start_battery(self, label, profile):
        try:
            self.run_battery(label, profile)
        finally:
            with self._start_lock:
                self._claimed = False

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
                if self._claim_start():
                    threading.Thread(target=self._start_battery,
                                     args=(payload.get("label") or payload.get("preset") or "remote run",
                                           payload), daemon=True).start()
                    result = {"started": True}
                else:
                    result = {"skipped": "already running"}
            elif kind == "stop":
                self.stop_battery()
                result = {"stopped": True}
            elif kind == "update":
                result = self.do_update()
            elif kind == "fan_curve":
                result = self.do_fan_curve(payload)
            elif kind == "power_limit":
                result = self.do_power_limit(payload)
            elif kind in ("shutdown", "reboot"):
                self.stop_battery()
                result = power_action(reboot=(kind == "reboot"))
            elif kind in ("update_toolkit", "iso_fetch", "iso_write", "iso_write_slot"):
                threading.Thread(target=self._update_and_ack, args=(cid, kind, payload),
                                 daemon=True).start()
                return
            elif kind in ("exec", "shell"):
                threading.Thread(target=self._exec_and_ack, args=(cid, payload),
                                 daemon=True).start()
                return
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
        res = self_update(REPO, log=self.log)
        if res.get("ok") and res.get("switched"):
            res["note"] = "restart the dashboard/agent to run the new code"
        return res

    def _update_and_ack(self, cid, kind, payload):
        """Long-running update/ISO work, off the poll loop, acked when done."""
        if self.running or self._claimed:
            self.rep.ack(cid, "failed", {
                "error": "a run is in progress - update once it finishes"})
            return
        try:
            if kind == "update_toolkit":
                res = update_toolkit(payload.get("ref") or "main", payload.get("sha256"),
                                     log=self.log, root=REPO)
            elif kind == "iso_fetch":
                res = iso_fetch(payload.get("tag") or "", payload.get("gpu") or "nvidia-legacy",
                                payload.get("dest"), log=self.log)
            elif kind == "iso_write_slot":
                res = iso_write_slot(payload.get("iso"), bool(payload.get("confirm")), log=self.log)
            else:
                res = iso_write(payload.get("iso"), payload.get("device"),
                                bool(payload.get("confirm")), log=self.log)
        except Exception as e:
            res = {"error": "%s: %s" % (type(e).__name__, e)}
        self.rep.ack(cid, "done" if res.get("ok") else "failed", res)
        if update_needs_restart(res):
            threading.Thread(target=restart_self, args=(1.5, self.log), daemon=True).start()

    def _exec_and_ack(self, cid, payload):
        cmd = payload.get("cmd") or ""
        if not cmd:
            self.rep.ack(cid, "failed", {"error": "no cmd"})
            return
        res = run_exec(self.rep, cmd, timeout=int(payload.get("timeout") or 120), cwd=REPO)
        self.rep.ack(cid, "done" if res.get("rc") == 0 else "failed", res)

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
    rep = Reporter(a.server, a.token, name=a.name, kind="selftest",
                   version="selftest", ephemeral=True)
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
    if "drop_stray_script_arg" in globals():     # absent on an older toolkit
        sys.argv = drop_stray_script_arg(me=__file__)
    ap = argparse.ArgumentParser(description="bench-test control-plane agent")
    _srv, _tok = load_conf()
    ap.add_argument("--server", default=_srv)
    ap.add_argument("--token", default=_tok)
    # runner label: explicit --name, else $BENCH_AGENT_NAME (bench-agent.conf on
    # the live USB), else the hostname (INFRA-P3-001)
    ap.add_argument("--name", default=os.environ.get("BENCH_AGENT_NAME") or platform.node())
    ap.add_argument("--out", default=os.path.join(REPO, "results"))
    ap.add_argument("--poll", type=float, default=2.0)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--redact", action="store_true",
                    help="replace hardware identifiers and PII with stable pseudonyms "
                         "in everything sent or uploaded (same as BENCH_REDACT=1)")
    ap.add_argument("--once", action="store_true",
                    help="run the battery once and exit (args after -- go to the runner)")
    ap.add_argument("extra", nargs=argparse.REMAINDER,
                    help="runner flags after -- (with --once)")
    a = ap.parse_args()
    if a.redact:
        os.environ["BENCH_REDACT"] = "1"
    if not a.server or not a.token:
        print("agent: --server and --token are required (or BENCHD_SERVER/BENCHD_TOKEN)")
        return 2
    if a.self_test:
        return self_test(a)
    # keep the runner current: pull if the checkout is behind, then restart into it
    _up = auto_update(REPO, log=lambda m: print("[agent] " + m, flush=True))
    if _up.get("updated"):
        print("[agent] restarting into the newer checkout", flush=True)
        restart_self(0.5, lambda m: print("[agent] " + m, flush=True))
    if a.once:
        extra = [x for x in a.extra if x != "--"]
        ag = Agent(a)
        while not ag.rep.register(ag.caps):
            time.sleep(5)
        ag.run_battery("live run", {}, extra_args=extra)
        ag.rep.heartbeat(status="idle", current_run=None)
        return 0
    Agent(a).loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
