"""Reporting-client tests: Reporter, PhaseTracker, ReporterPump, system info,
summary/inventory builders, and the remote-exec helper."""
import json
import os
import sys
import tempfile
import time

from harness import Checker, Server, REPO, BENCH, tmpdir
from benchreport import (Reporter, PhaseTracker, ReporterPump, build_summary_dir,
                         telemetry_summary, system_info, read_inventory, run_exec,
                         upload_telemetry_csv, upload_run_files, build_summary)


def run():
    c = Checker("reporting")
    srv = Server()
    cl = srv.client()

    # ---- Reporter + PhaseTracker ----
    rep = Reporter(srv.base, srv.token, name="unit", kind="agent")
    c.check("register", rep.register({"os": "test"}))
    c.check("start_run", rep.start_run("unit run", {"gpu_soak": 5}) is not None)
    tr = PhaseTracker(rep)
    for ln in ["[sys] inventory", "[gpu] info", "[gpu] soak", "[cpu] soak"]:
        tr.feed(ln)
    _, detail = cl.call("GET", "/api/v1/runs/" + rep.run_id)
    st = {p["grp"]: p["status"] for p in detail["phases"]}
    c.check("4 phases recorded", len(detail["phases"]) == 4, detail["phases"])
    c.check("prior phases closed on group change",
            st.get("sys") == "done" and st.get("gpu") == "done" and st.get("cpu") == "running", st)

    # ---- a phase that crashed must not be reported as done ----
    rep2 = Reporter(srv.base, srv.token, name="unit-crash", kind="agent")
    rep2.register({"os": "test"})
    rep2.start_run("crashy", {"gpu": 5})
    tr2 = PhaseTracker(rep2)
    for ln in ["[gpu] matmul", "Traceback (most recent call last):",
               '  File "/opt/bench-test/bench/gpu_bench.py", line 19, in <module>',
               "ModuleNotFoundError: No module named 'torch'", "[cpu] soak"]:
        tr2.feed(ln)
    tr2.close()
    _, detail2 = cl.call("GET", "/api/v1/runs/" + rep2.run_id)
    st2 = {p["grp"]: p["status"] for p in detail2["phases"]}
    c.check("a phase with a traceback is reported failed", st2.get("gpu") == "failed", st2)
    c.check("the phase after a crash still reports done", st2.get("cpu") == "done", st2)
    # leave the run list as the later checks expect it
    cl.call("DELETE", "/api/v1/runs/" + rep2.run_id)

    # ---- ReporterPump: telemetry + logs + commands ----
    got = []
    pump = ReporterPump(rep, on_command=lambda cmd: (got.append(cmd["kind"]),
                                                     rep.ack(cmd["id"], "done", {"ok": True})),
                        interval=0.3)
    for i in range(3):
        pump.push_sample({"elapsed": float(i), "temp_c": 50 + i, "gpu_util": 90})
    pump.push_log("[gpu] hello")
    pump.start()
    time.sleep(0.9)
    _, tel = cl.call("GET", "/api/v1/runs/%s/telemetry" % rep.run_id)
    c.check("pump flushed telemetry", len(tel["rows"]) == 3, len(tel["rows"]))
    _, logs = cl.call("GET", "/api/v1/runs/%s/logs" % rep.run_id)
    c.check("pump flushed logs", any("hello" in l["line"] for l in logs["lines"]), logs)
    cl.call("POST", "/api/v1/runners/%s/commands" % rep.runner_id, {"kind": "stop"})
    time.sleep(0.9)
    c.check("pump received command", got == ["stop"], got)

    tr.close()
    rep.finish("done", 0, {"gpu_soak_tflops": 42.0})
    time.sleep(0.2)
    _, runs = cl.call("GET", "/api/v1/runs")
    c.check("run finished", runs["runs"][0]["status"] == "done", runs["runs"][0])
    pump.stop()

    # ---- system_info ----
    si = system_info()
    c.check("system_info cpu is a real name", bool(si.get("cpu")) and "Family" not in si.get("cpu", ""),
            si.get("cpu"))

    # ---- summary / telemetry / inventory builders ----
    d = tmpdir()
    with open(os.path.join(d, "host-20260101-000000.json"), "w") as fh:
        json.dump({"host": "h", "phases": [
            {"group": "gpu", "phase": "matmul", "result": {"results": {"fp16": {"tflops": 90.6}}}},
            {"group": "gpu", "phase": "soak", "result": {"matmul_tflops": 81.1,
                                                         "telemetry": {"temp_c": [60, 65, 67]}}}], }, fh)
    with open(os.path.join(d, "host-cpu-20260101-000010.json"), "w") as fh:
        json.dump({"host": "h", "phases": {"soak": {"phase": "soak", "gflops": 152.0}}}, fh)
    with open(os.path.join(d, "host-win-20260101-000000.telemetry.csv"), "w") as fh:
        fh.write("elapsed_s,temp_c,fan_pct,power_w,gpu_util_pct\n")
        for i in range(5):
            fh.write("%d,%d,%d,%d,%d\n" % (i, 50 + i, 40, 200 + i, 99))
    summ = build_summary_dir(d)
    c.check("build_summary_dir matmul", summ.get("gpu_matmul_tflops", {}).get("fp16") == 90.6, summ)
    c.check("build_summary_dir soak", summ.get("gpu_soak_tflops") == 81.1, summ)
    c.check("build_summary_dir cpu soak", summ.get("cpu_soak_gflops") == 152.0, summ)
    ts = telemetry_summary(os.path.join(d, "host-win-20260101-000000.telemetry.csv"))
    c.check("telemetry_summary", ts and ts["samples"] == 5 and ts["temp_c"] == [50, 52, 54], ts)

    # ---- several gfx records: the one that scored must win ----
    # A monitor-less stick writes a furmark "skipped" record and then a glmark2
    # fallback that actually scored. Whichever record came last used to win, so
    # the summary reported "skipped" while a real score existed.
    gd = tmpdir()
    with open(os.path.join(gd, "host-20260101-000000.json"), "w") as fh:
        json.dump({"host": "h", "phases": [
            {"group": "gfx", "phase": "furmark",
             "result": {"phase": "furmark", "skipped": True,
                        "reason": "no monitor on the X display"}},
            {"group": "gfx", "phase": "glmark2",
             "result": {"phase": "glmark2", "score": 31723, "scenes": 8}},
            {"group": "gfx", "phase": "furmark",
             "result": {"phase": "furmark", "skipped": True, "reason": "furmark exited 1"}},
        ]}, fh)
    g = build_summary_dir(gd)
    c.check("summary keeps the gfx record that scored, not the later skipped one",
            (g.get("gfx") or {}).get("score") == 31723, g.get("gfx"))
    gd2 = tmpdir()
    with open(os.path.join(gd2, "host-20260101-000000.json"), "w") as fh:
        json.dump({"host": "h", "phases": [
            {"group": "gfx", "phase": "furmark",
             "result": {"phase": "furmark", "skipped": True,
                        "reason": "no monitor on the X display"}}]}, fh)
    g2 = build_summary_dir(gd2)
    c.check("summary still reports a skipped gfx record when nothing scored",
            (g2.get("gfx") or {}).get("skipped") is True, g2.get("gfx"))
    # a summary build must never fail silently into {} - that hid a NameError and
    # made a real run look like it had measured nothing
    for f in ("tui.py", "gui.py"):
        src = open(os.path.join(BENCH, f), encoding="utf-8").read()
        c.check("%s surfaces a summary build failure" % f,
                "summary_error" in src, f)

    # ---- run_exec ----
    rep2 = Reporter(srv.base, srv.token, name="exec", kind="test")
    rep2.register({})
    rep2.start_run("exec", {})
    res = run_exec(rep2, "echo bench-exec-ok")
    _, logs = cl.call("GET", "/api/v1/runs/%s/logs" % rep2.run_id)
    c.check("run_exec streams output", res.get("rc") == 0
            and any("bench-exec-ok" in l["line"] for l in logs["lines"]), (res, logs))
    rep2.finish("done", 0, {})

    # ---- sampler columns all map to canonical names (no silently dropped data) ----
    import re
    from benchreport import CSV_ALIASES
    win_src = open(os.path.join(BENCH, "run_win.py"), encoding="utf-8").read()
    win_cols = re.findall(r'"([^"]+)"', re.search(r"^COLUMNS = \[(.*?)\]", win_src, re.S | re.M).group(1))
    ln_src = open(os.path.join(BENCH, "telemetry_log.py"), encoding="utf-8").read()
    hdr = re.search(r"^HEADER = \((.*?)\)\n", ln_src, re.S | re.M).group(1)
    ln_cols = "".join(re.findall(r'"([^"]+)"', hdr)).split(",")
    allow = {"time"}
    for name, cols in (("run_win", win_cols), ("telemetry_log", ln_cols)):
        missing = [x for x in cols if x not in CSV_ALIASES and x not in allow]
        c.check("%s columns mapped" % name, not missing, missing)
    c.check("samplers collect headroom/health",
            all(k in win_cols for k in ("throttle", "throttle_bits", "temp_limit_c", "video_mhz",
                                        "pcie_gen", "pcie_width", "power_max_w"))
            and all(k in ln_cols for k in ("throttle", "temp_limit_c", "video_mhz", "pcie_gen")),
            (win_cols[-9:], ln_cols[-9:]))

    # ---- the CSV must stay rectangular (a short row shifts every column) ----
    # Per-core columns are appended dynamically, so this is the check that the
    # header and every row still line up on both platforms.
    import csv as _csv
    import subprocess as _sp
    import threading as _th
    import time as _t
    import run_win as _rw
    _out = os.path.join(tmpdir(), "align-win.telemetry.csv")
    _stop = _th.Event()
    _th.Thread(target=_rw.sampler, args=(_out, _stop, 0.25, 0.5), daemon=True).start()
    _t.sleep(2.5)
    _stop.set()
    _t.sleep(0.5)
    _rows = [r for r in _csv.reader(open(_out, newline="")) if r]
    c.check("run_win rows match the header width",
            bool(_rows) and all(len(r) == len(_rows[0]) for r in _rows),
            [(i + 1, len(r)) for i, r in enumerate(_rows) if len(r) != len(_rows[0])][:4])
    c.check("run_win writes per-core columns",
            len([h for h in _rows[0] if h.startswith("cpu_core")]) >= 1, _rows[0][-4:])
    _lout = os.path.join(tmpdir(), "align-linux.telemetry.csv")
    _sp.run([sys.executable, os.path.join(BENCH, "telemetry_log.py"), "--out", _lout,
             "--interval", "0.25", "--duration", "2"],
            capture_output=True, timeout=90)
    _lrows = [r for r in _csv.reader(open(_lout, newline="")) if r]
    c.check("telemetry_log rows match the header width",
            bool(_lrows) and all(len(r) == len(_lrows[0]) for r in _lrows),
            [(i + 1, len(r)) for i, r in enumerate(_lrows) if len(r) != len(_lrows[0])][:4])

    # ---- LibreHardwareMonitor line -> telemetry mapping (AMD/Intel Windows) ----
    _line = ("1791870637 line=7 elevated=0 gpu_temp=58 gpu_power=26 gpu_clk=800 mem_clk=800 "
             "gpu_util=8 gpu_3d=8.4 gpu_copy=0.2 gpu_enc= gpu_dec=0 vram_used=387 "
             "vram_total=512 mem_util=75.6 gpu_fan_rpm=1184 gpu_fan_pct=30 cpu_temp= "
             "cpu_power= cpu_util=23.8 cpu_max_core=37.1 cpu_mhz= storage_temp=58 "
             "ncores=16 gpu_name=AMD_Radeon_Graphics cpu_name=AMD_Ryzen_Z1_Extreme "
             "c1=28.6 c2=20 c16=37.1")
    _u = _rw._parse_lhm_line(_line)
    c.check("lhm: gpu temp/power/clocks mapped",
            (_u["gpu_temp"], _u["gpu_power"], _u["gpu_clk"]) == (58.0, 26.0, 800.0), _u)
    c.check("lhm: vram used + total + load mapped",
            (_u["vram_used"], _u["vram_total"], _u["mem_util"]) == (387.0, 512.0, 75.6), _u)
    c.check("lhm: fan rpm + pct mapped (previously dropped on the floor)",
            (_u["gpu_fan_rpm"], _u["gpu_fan_pct"]) == (1184.0, 30.0), _u)
    c.check("lhm: engine loads mapped", (_u["gpu_3d"], _u["gpu_copy"]) == (8.4, 0.2), _u)
    c.check("lhm: an unreadable value stays blank, not zero",
            _u["cpu_temp"] is None and _u["cpu_power"] is None, _u)
    c.check("lhm: per-core loads become cpu_coreN keys",
            _u["cores"] == {"cpu_core1": 28.6, "cpu_core2": 20.0, "cpu_core16": 37.1},
            _u.get("cores"))
    c.check("lhm: the hardware name comes through for the GPU identity",
            _u["gpu_name"] == "AMD_Radeon_Graphics" and _u["cpu_name"] == "AMD_Ryzen_Z1_Extreme",
            (_u["gpu_name"], _u["cpu_name"]))
    c.check("lhm: an unknown key is ignored rather than fatal",
            "no_such_key" not in _rw._parse_lhm_line("a=1 no_such_key=2"), "")

    # ---- hardware verdicts from the health report + inventory ----
    hv = tmpdir()
    with open(os.path.join(hv, "host-20260101-000000.health.json"), "w") as fh:
        json.dump({"summary": [{"provider": "Microsoft-Windows-WHEA-Logger", "id": "17", "count": 2},
                               {"provider": "nvlddmkm", "id": "4101", "count": 1},
                               {"provider": "Microsoft-Windows-Kernel-Power", "id": "41", "count": 1}],
                   "disks": [{"name": "X", "wear": 85}]}, fh)
    with open(os.path.join(hv, "host-info-20260101-000000.info.json"), "w") as fh:
        json.dump({"hardware_errors": ["Xid 31 ..."], "ecc": {"ce": 4},
                   "disk_health": {"/dev/nvme0n1": {"used_pct": 90, "media_errors": 0}}}, fh)
    with open(os.path.join(hv, "host-win-20260101-000001.sysinfo.json"), "w") as fh:
        json.dump({"Host": "W", "Elevated": False,
                   "SmartPredict": [{"instance": "SCSI\\Disk", "predict_failure": True}],
                   "Disks": [{"name": "D0", "health": "Healthy", "wmi_status": "Pred Fail",
                              "temp_c": None, "wear": None}]}, fh)
    vs = build_summary_dir(hv).get("verdicts") or []
    txt = " ".join(v.get("text", "") for v in vs)
    c.check("verdict: WHEA reported", "WHEA" in txt, vs)
    c.check("verdict: TDR reported", "TDR" in txt, vs)
    c.check("verdict: ECC reported", "ECC" in txt, vs)
    c.check("verdict: unexpected shutdown reported", "Kernel-Power 41" in txt, vs)
    c.check("verdict: disk wear reported", "wear" in txt, vs)
    c.check("verdict: predictive failure reported", "predictive failure" in txt, vs)
    c.check("verdict: elevation gap reported", "not elevated" in txt, vs)
    c.check("verdicts carry a level",
            bool(vs) and all(v.get("level") in ("good", "info", "warn", "bad") for v in vs), vs)

    # a results dir accumulates one health report per phase, so the same finding
    # arrives repeatedly with a drifting count - a real desktop run produced ~50
    # entries for four findings, which made the list unreadable
    hv2 = tmpdir()
    for i, (kp, stor) in enumerate(((4, 5), (6, 7), (8, 14))):
        with open(os.path.join(hv2, "host-20260101-00000%d.health.json" % i), "w") as fh:
            json.dump({"summary": [
                {"provider": "Microsoft-Windows-Kernel-Power", "id": "41", "count": kp},
                {"provider": "disk", "id": "153", "count": stor}]}, fh)
    vs2 = build_summary_dir(hv2).get("verdicts") or []
    c.check("repeated findings collapse to one verdict per kind",
            len([v for v in vs2 if "Kernel-Power 41" in v["text"]]) == 1, vs2)
    c.check("the surviving verdict keeps the widest window",
            any(v["text"].endswith(": 8") for v in vs2 if "Kernel-Power 41" in v["text"]), vs2)
    c.check("the storage verdict keeps its own widest count too",
            any(v["text"].endswith(": 14") for v in vs2 if "storage events" in v["text"]), vs2)
    c.check("deduped verdicts keep their level",
            all(v["level"] == "bad" for v in vs2 if "Kernel-Power 41" in v["text"]), vs2)
    c.check("no internal bookkeeping leaks into the verdicts",
            all(set(v) == {"level", "text"} for v in vs2), vs2)

    # ---- a stale Valley screenshot must not be attributed to this run ----
    gd3 = tmpdir()
    with open(os.path.join(gd3, "host-20260101-000000.json"), "w") as fh:
        json.dump({"host": "h", "phases": [{"group": "gfx", "phase": "furmark",
                                            "result": {"skipped": True,
                                                       "reason": "no monitor"}}]}, fh)
    stale = os.path.join(gd3, "valley-host-old.png")
    with open(stale, "wb") as fh:
        fh.write(b"png")
    os.utime(stale, (time.time() - 86400, time.time() - 86400))
    c.check("a stale valley screenshot is not adopted as this run's gfx",
            "gfx" not in (build_summary_dir(gd3, since=time.time() - 60) or {}),
            build_summary_dir(gd3, since=time.time() - 60))
    fresh = os.path.join(gd3, "valley-host-new.png")
    with open(fresh, "wb") as fh:
        fh.write(b"png")
    c.check("a screenshot from this run is still picked up",
            (build_summary_dir(gd3, since=time.time() - 60) or {}).get("gfx", {}).get("screenshot")
            == os.path.basename(fresh),
            build_summary_dir(gd3, since=time.time() - 60).get("gfx"))

    # ---- phase results that used to be dropped are merged into the summary ----
    d2 = tmpdir()
    for name, obj in {
        "h-gpu-x.json": {"phases": [{"group": "gpu", "phase": "pcie",
                                     "result": {"h2d_gbps": 12.3, "d2h_gbps": 13.1}}]},
        "h-nvenc.json": {"phases": [{"group": "nvenc", "phase": "bench",
                                     "result": {"results": {"h264_nvenc": {"fps": 150, "ssim": 0.98}}}}]},
        "h-llm.json": {"results": [{"model": "qwen3:8b", "tok_per_s": 42.0, "eval_tokens": 128}]},
        "h-disk.json": {"phases": [{"group": "disk", "phase": "latency",
                                    "result": {"iops": 1200, "p50": 0.3, "p99": 1.2}}]},
        "h-ram.json": {"phases": [{"group": "ram", "phase": "bandwidth",
                                   "result": {"results": {"copy": {"gbps": 12.3}}}}]},
    }.items():
        with open(os.path.join(d2, name), "w") as fh:
            json.dump(obj, fh)
    s2 = build_summary_dir(d2)
    c.check("summary: pcie merged", (s2.get("pcie") or {}).get("h2d_gbps") == 12.3, s2.get("pcie"))
    c.check("summary: nvenc fps merged",
            (((s2.get("nvenc") or {}).get("results") or {}).get("h264_nvenc") or {}).get("fps") == 150,
            s2.get("nvenc"))
    c.check("summary: llm tok/s merged",
            (s2.get("llm") or {}).get("results", [{}])[0].get("tok_per_s") == 42.0, s2.get("llm"))
    c.check("summary: disk latency merged",
            (s2.get("disk") or {}).get("latency", {}).get("p99") == 1.2, s2.get("disk"))
    c.check("summary: ram bandwidth merged",
            (((s2.get("ram") or {}).get("bandwidth") or {}).get("copy") or {}).get("gbps") == 12.3,
            s2.get("ram"))

    # ---- power command helper exists (never invoked in tests) ----
    from benchreport import power_action
    c.check("power_action is callable", callable(power_action), power_action)

    # ---- dashboard parity helpers (one spec for GUI + TUI) ----
    from benchreport import (DASH_FIELDS, SAMPLE_FIELDS as _SF, CSV_ALIASES as _CA,
                             hardware_line)
    canon = set(_SF) | set(_CA.values())
    missing = [k for k, _l, _d in DASH_FIELDS if k not in canon]
    c.check("DASH_FIELDS keys are real telemetry fields", not missing, missing)
    c.check("hardware_line never renders None",
            "None" not in hardware_line({"cpu": None, "ram_gib": None, "gpu": None}),
            hardware_line({"cpu": None}))
    full = hardware_line({"cpu": "Intel(R) Core(TM) i7-6800K CPU @ 3.40GHz", "ram_gib": 32,
                          "gpu": "NVIDIA GeForce RTX 4070 Ti SUPER", "driver": "617.42",
                          "cores": 6, "threads": 12})
    c.check("hardware_line has cpu + ram + gpu + driver",
            "i7-6800K" in full and "32 GiB" in full and "RTX 4070" in full and "617.42" in full,
            full)
    c.check("hardware_line tags cores/threads", "(6c/12t)" in full, full)
    c.check("hardware_line copes with an empty dict", hardware_line({}) == "", hardware_line({}))
    for f in ("gui.py", "tui.py"):
        src = open(os.path.join(BENCH, f), encoding="utf-8").read()
        c.check("%s uses the shared dashboard spec" % f,
                "DASH_FIELDS" in src and "hardware_line" in src, "")

    for f in ("gui.py", "tui.py"):
        src = open(os.path.join(BENCH, f), encoding="utf-8").read()
        c.check("%s pushes every telemetry column (incl. elapsed)" % f,
                ("for k in self.tel_cols" in src) or ("for n in self._cols" in src), f)
        c.check("%s keeps results off the toolkit copy on a live stick" % f,
                "persist_root() if is_live()" in src, f)
    for f in ("gui.py", "tui.py", "agent.py"):
        src = open(os.path.join(BENCH, f), encoding="utf-8").read()
        c.check("%s refuses a toolkit update while a run is in progress" % f,
                "a run is in progress - update once it finishes" in src, f)

    # ---- the run's full CSV must reach the server when it ends ----
    class _FakeRep:
        run_id = "run-x"

        def __init__(self):
            self.samples, self.arts = [], []

        def telemetry(self, samples):
            self.samples.extend(samples)

        def artifact(self, name, text, kind="json"):
            self.arts.append((name, kind, len(text)))

    tdir = tmpdir()
    with open(os.path.join(tdir, "host-win-1.telemetry.csv"), "w") as fh:
        fh.write("elapsed_s,gpu_util_pct,temp_c,disk_read_mbs,pstate\n")
        fh.write("0.0,10.0,45.0,1.5,P0\n")
        fh.write("1.0,20.0,46.0,2.5,P0\n")
    fr = _FakeRep()
    n = upload_telemetry_csv(fr, tdir)
    c.check("upload_telemetry_csv sends one sample per row", n == 2 and len(fr.samples) == 2,
            (n, fr.samples))
    c.check("upload_telemetry_csv canonicalises the column names",
            fr.samples[0].get("gpu_util") == 10.0 and fr.samples[0].get("elapsed") == 0.0,
            fr.samples[0])
    c.check("upload_telemetry_csv keeps what the live stream drops",
            fr.samples[1].get("disk_read_mbs") == 2.5 and "pstate" not in fr.samples[1],
            fr.samples[1])
    c.check("upload_telemetry_csv attaches the raw CSV",
            bool(fr.arts) and fr.arts[0][0].endswith(".telemetry.csv") and fr.arts[0][1] == "csv",
            fr.arts)
    c.check("upload_telemetry_csv is a no-op without a run",
            upload_telemetry_csv(None, tdir) == 0, "")
    for f in ("gui.py", "tui.py", "agent.py"):
        src = open(os.path.join(BENCH, f), encoding="utf-8").read()
        c.check("%s uploads the telemetry CSV at the end of a run" % f,
                "upload_telemetry_csv(" in src, f)

    # ...but only the rows the dashboard did NOT already stream live. Without
    # this every sample was stored twice: the live stream posted the run, then
    # this upload posted the whole file again at the end (the charts drew each
    # point twice and the stored rate looked ~2x the configured one).
    fr2 = _FakeRep()
    n2 = upload_telemetry_csv(fr2, tdir, sent={"host-win-1.telemetry.csv": {0.0, 1.0}})
    c.check("upload_telemetry_csv skips rows the live stream already sent",
            n2 == 0 and fr2.samples == [], (n2, fr2.samples))
    c.check("upload_telemetry_csv still attaches the CSV when it skips every row",
            bool(fr2.arts), fr2.arts)
    fr3 = _FakeRep()
    n3 = upload_telemetry_csv(fr3, tdir, sent={"host-win-1.telemetry.csv": {0.0}})
    c.check("upload_telemetry_csv fills exactly the gap, dropping nothing",
            n3 == 1 and [s["elapsed"] for s in fr3.samples] == [1.0], (n3, fr3.samples))
    fr4 = _FakeRep()
    n4 = upload_telemetry_csv(fr4, tdir, sent={"some-other.telemetry.csv": {0.0, 1.0}})
    c.check("upload_telemetry_csv keys the watermark per file, so another run cannot silence it",
            n4 == 2, n4)
    for f in ("gui.py", "tui.py"):
        src = open(os.path.join(BENCH, f), encoding="utf-8").read()
        c.check("%s tells the upload what it already streamed" % f,
                "upload_telemetry_csv(self.rep, self.results_dir, sent=" in src, f)

    # ---- the net phase must not record a 403 as a 0 Mbps measurement ----
    netps = open(os.path.join(BENCH, "Invoke-NetBench.ps1"), encoding="utf-8").read()
    c.check("net phase checks the HTTP status before trusting the speed",
            "http_code" in netps and "download_error" in netps, "")
    c.check("net phase steps down the download size",
            "@($DownMB, 50, 25, 10)" in netps, "")
    c.check("the Python net phase chunks its download",
            "chunk_mb" in open(os.path.join(BENCH, "net_bench.py"), encoding="utf-8").read(), "")

    # ---- nvenc/gfx results must reach the summary, and the run's files the server ----
    ndir = tmpdir()
    with open(os.path.join(ndir, "HOST-20260101-000000.nvenc.json"), "w") as fh:
        json.dump({"ffmpeg": "ffmpeg.exe",
                   "results": [{"codec": "h264_nvenc", "fps": 273, "enc_util_max": 48},
                               {"codec": "hevc_nvenc", "fps": 394, "enc_util_max": 47}]}, fh)
    with open(os.path.join(ndir, "valley-HOST-20260101-000000-f01.png"), "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    s = build_summary(os.path.join(ndir, "HOST-20260101-000000.nvenc.json"))
    c.check("nvenc list shape reaches the summary",
            (s.get("nvenc") or {}).get("fps") == {"h264_nvenc": 273, "hevc_nvenc": 394}, s.get("nvenc"))
    c.check("nvenc encoder utilisation is kept",
            (s.get("nvenc") or {}).get("enc_util_max") == {"h264_nvenc": 48, "hevc_nvenc": 47},
            s.get("nvenc"))
    c.check("the gfx screenshot is recorded", (s.get("gfx") or {}).get("screenshot", "").endswith(".png"),
            s.get("gfx"))
    ldir = tmpdir()
    with open(os.path.join(ldir, "HOST-linux-20260101-000000.json"), "w") as fh:
        json.dump({"host": "HOST", "phases": [
            {"phase": "bench", "group": "nvenc",
             "result": {"phase": "bench", "results": {"h264_nvenc": {"fps": 210.0},
                                                      "av1_nvenc": {"skipped": True,
                                                                    "reason": "no av1 support"}}}}]}, fh)
    s2 = build_summary(os.path.join(ldir, "HOST-linux-20260101-000000.json"))
    c.check("nvenc dict shape reaches the summary",
            (s2.get("nvenc") or {}).get("fps") == {"h264_nvenc": 210.0}, s2.get("nvenc"))
    c.check("a skipped codec is recorded with its reason",
            "no av1 support" in json.dumps(s2.get("nvenc")), s2.get("nvenc"))

    class _FakeRep2:
        run_id = "run-y"

        def __init__(self):
            self.arts = []

        def artifact(self, name, text, kind="json"):
            self.arts.append((name, kind, len(text)))

    fdir = tmpdir()
    open(os.path.join(fdir, "HOST-net-1.json"), "w").write("{}")
    open(os.path.join(fdir, "HOST-net-1.md"), "w").write("# x")
    open(os.path.join(fdir, "big.json"), "w").write("x" * (600 * 1024))
    fr2 = _FakeRep2()
    n2 = upload_run_files(fr2, fdir)
    c.check("upload_run_files attaches the run's reports",
            n2 == 2 and sorted(a[0] for a in fr2.arts) == ["HOST-net-1.json", "HOST-net-1.md"],
            fr2.arts)
    c.check("upload_run_files skips oversized files",
            all("big.json" not in a[0] for a in fr2.arts), fr2.arts)
    c.check("upload_run_files is a no-op without a run", upload_run_files(None, fdir) == 0, "")

    ldir2 = tmpdir()
    os.makedirs(os.path.join(ldir2, "HOST-linux-1-logs"))
    open(os.path.join(ldir2, "HOST-linux-1-logs", "gfx-diag.txt"), "w").write("display: :9")
    open(os.path.join(ldir2, "HOST-linux-1-logs", "gfx-furmark.log"), "w").write("rc=139")
    open(os.path.join(ldir2, "HOST-linux-1.json"), "w").write("{}")
    fr3 = _FakeRep2()
    n3 = upload_run_files(fr3, ldir2)
    c.check("upload_run_files attaches the per-phase logs and gfx diagnostics",
            n3 == 3 and any(a[0] == "gfx-diag.txt" for a in fr3.arts), fr3.arts)
    for f in ("gui.py", "tui.py", "agent.py"):
        src = open(os.path.join(BENCH, f), encoding="utf-8").read()
        c.check("%s attaches the run's files" % f, "upload_run_files(" in src, f)

    # ---- FurMark must survive a toolkit re-point (like PyTorch) ----
    root = os.path.dirname(BENCH)
    envsh = open(os.path.join(root, "live", "airootfs", "usr", "local", "bin",
                              "bench-agent-env"), encoding="utf-8").read()
    c.check("the boot env exports FURMARK from the persisted copy",
            "export FURMARK" in envsh, "")
    runsh = open(os.path.join(BENCH, "Run-LinuxBench.sh"), encoding="utf-8").read()
    c.check("the gfx phase looks outside the toolkit for furmark",
            "BENCH_PERSIST_DIR:-/mnt/benchdata}/tools/FurMark_linux64/furmark" in runsh, "")
    buildsh = open(os.path.join(root, "live", "Build-LiveUSB.sh"), encoding="utf-8").read()
    c.check("the ISO bakes FurMark outside the toolkit dir",
            "opt/bench-tools/furmark" in buildsh, "")

    # ---- a summary must only cover the run that produced it ----
    sdir = tmpdir()
    old = os.path.join(sdir, "HOST-disk-20260101-000000.json")
    with open(old, "w") as fh:
        json.dump({"host": "HOST", "phases": [{"phase": "seqwrite", "group": "disk",
                                               "result": {"mbps": 999.0}}]}, fh)
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    c.check("an old report is merged when no run start is given",
            (build_summary_dir(sdir) or {}).get("disk_seqwrite_mbps") == 999.0,
            build_summary_dir(sdir))
    c.check("an old report is excluded when the run's start is given",
            "disk_seqwrite_mbps" not in (build_summary_dir(sdir, since=time.time() - 60) or {}),
            build_summary_dir(sdir, since=time.time() - 60))
    for f in ("gui.py", "tui.py", "agent.py"):
        src = open(os.path.join(BENCH, f), encoding="utf-8").read()
        c.check("%s scopes the summary to its own run" % f, "since=self.run_t0" in src, f)

    srv.stop()
    return c.fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
