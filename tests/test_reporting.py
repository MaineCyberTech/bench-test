"""Reporting-client tests: Reporter, PhaseTracker, ReporterPump, system info,
summary/inventory builders, and the remote-exec helper."""
import json
import os
import sys
import tempfile
import time

from harness import Checker, Server, REPO, BENCH, tmpdir, skip, wait_for, wait_for_rows
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
                        interval=0.1)
    for i in range(3):
        pump.push_sample({"elapsed": float(i), "temp_c": 50 + i, "gpu_util": 90})
    pump.push_log("[gpu] hello")
    pump.start()

    def _telemetry_rows():
        _, t = cl.call("GET", "/api/v1/runs/%s/telemetry" % rep.run_id)
        rows = t.get("rows", [])
        return rows if len(rows) == 3 else None

    def _has_log():
        _, lg = cl.call("GET", "/api/v1/runs/%s/logs" % rep.run_id)
        return any("hello" in l["line"] for l in lg.get("lines", [])) or None

    # poll-until rather than a fixed sleep: a slow host is not flaked by a too-short
    # sleep, and a fast one is not slowed by a too-long one (TEST-P3-002)
    tel = wait_for(_telemetry_rows, timeout=5) or []
    c.check("pump flushed telemetry", len(tel) == 3, len(tel))
    c.check("pump flushed logs", bool(wait_for(_has_log, timeout=5)), "")
    cl.call("POST", "/api/v1/runners/%s/commands" % rep.runner_id, {"kind": "stop"})
    c.check("pump received command", bool(wait_for(lambda: got == ["stop"] or None, timeout=5)), got)

    tr.close()
    rep.finish("done", 0, {"gpu_soak_tflops": 42.0})

    def _finished():
        _, runs = cl.call("GET", "/api/v1/runs")
        rs = runs.get("runs", [])
        return rs if rs and rs[0]["status"] == "done" else None

    runs = wait_for(_finished, timeout=5) or []
    c.check("run finished", bool(runs) and runs[0]["status"] == "done", runs[0] if runs else runs)
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

    # the server drops an impossible fan_pct from LIVE samples, but the summary
    # is built from the raw CSV: without the same rule a stored summary carried
    # fan_pct [20.0, 48.6, 572.0]. Dropped, never clamped to 100.
    badd = tmpdir()
    with open(os.path.join(badd, "host-bad.telemetry.csv"), "w") as fh:
        fh.write("elapsed_s,fan_pct,gpu_util_pct\n")
        fh.write("0.0,20.0,50.0\n")
        fh.write("1.0,48.6,60.0\n")
        fh.write("2.0,572.0,70.0\n")
        fh.write("3.0,-3.0,80.0\n")
    ts_bad = telemetry_summary(os.path.join(badd, "host-bad.telemetry.csv"))
    c.check("an impossible fan_pct is dropped from the summary, not clamped",
            ts_bad and ts_bad["samples"] == 4 and ts_bad["fan_pct"] == [20.0, 34.3, 48.6]
            and ts_bad["gpu_util"] == [50, 65, 80], ts_bad)

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

    # ---- the soak metric must be interpretable across matrix sizes ----
    # the same card reports very different TFLOPS at N=4096 vs N=10240, and the
    # matmul-only number omits the conv work the soak loop also does
    sd = tmpdir()
    with open(os.path.join(sd, "host-20260101-000000.json"), "w") as fh:
        json.dump({"host": "h", "phases": [
            {"group": "gpu", "phase": "soak",
             "result": {"matmul_tflops": 32.7, "total_tflops": 69.5, "size": 4096,
                        "seconds": 183.4, "matmuls": 43628}}]}, fh)
    s4 = build_summary_dir(sd)
    c.check("the soak keeps its matmul-only metric",
            s4.get("gpu_soak_tflops") == 32.7, s4.get("gpu_soak_tflops"))
    c.check("the soak records the matrix size that produced it",
            s4.get("gpu_soak_size") == 4096, s4.get("gpu_soak_size"))
    c.check("the soak reports the total throughput too (conv included)",
            s4.get("gpu_soak_total_tflops") == 69.5, s4.get("gpu_soak_total_tflops"))
    _gsrc = open(os.path.join(BENCH, "gpu_bench.py"), encoding="utf-8").read()
    c.check("both soak backends report size + total",
            _gsrc.count('"total_tflops": round') == 2 and _gsrc.count('"size": N') == 1
            and _gsrc.count('"size": n') == 1,
            (_gsrc.count('"total_tflops": round'), _gsrc.count('"size": N'), _gsrc.count('"size": n')))
    gd2 = tmpdir()
    with open(os.path.join(gd2, "host-20260101-000000.json"), "w") as fh:
        json.dump({"host": "h", "phases": [
            {"group": "gfx", "phase": "furmark",
             "result": {"phase": "furmark", "skipped": True,
                        "reason": "no monitor on the X display"}}]}, fh)
    g2 = build_summary_dir(gd2)
    c.check("summary still reports a skipped gfx record when nothing scored",
            (g2.get("gfx") or {}).get("skipped") is True, g2.get("gfx"))

    # a Windows gfx phase writes a FLAT report (host/timestamp/engine/result) with no
    # phases array; the AMD run's summary had no gfx at all because of it
    gd4 = tmpdir()
    with open(os.path.join(gd4, "host-furmark-20260101-000000.json"), "w") as fh:
        json.dump({"host": "h", "timestamp": "x", "engine": "furmark",
                   "result": {"phase": "furmark", "score": 983, "fps_avg": 33}}, fh)
    with open(os.path.join(gd4, "host-20260101-000000.json"), "w") as fh:
        json.dump({"host": "h", "phases": [{"group": "gpu", "phase": "matmul",
                                            "result": {"tflops": 1.0}}]}, fh)
    g4 = build_summary_dir(gd4)
    c.check("a flat gfx report reaches the summary",
            (g4.get("gfx") or {}).get("score") == 983, g4.get("gfx"))
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
    c.check("run_exec returns the output so an idle runner's exec is readable",
            "bench-exec-ok" in (res.get("out") or ""), res)
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
    # columns that were only ever on one platform, so a tile was permanently blank
    c.check("the Linux sampler reports total VRAM too (for a percentage)",
            "vram_total_mib" in ln_cols and "memory.total" in ln_src, "")
    c.check("the Windows sampler reports load1 like Linux does", "load1" in win_cols, "")
    # a single impossible value flattens a whole chart, so the samplers bound what
    # they derive: RAPL produced 6-9 kW single-row spikes on the stick
    c.check("the Linux sampler rejects an impossible package wattage",
            "w <= 1000" in ln_src, "")

    # ---- the CSV must stay rectangular (a short row shifts every column) ----
    # Per-core columns are appended dynamically, so this is the check that the
    # header and every row still line up on both platforms.
    import csv as _csv
    import subprocess as _sp
    import threading as _th
    import run_win as _rw
    _out = os.path.join(tmpdir(), "align-win.telemetry.csv")
    _stop = _th.Event()
    _thr = _th.Thread(target=_rw.sampler, args=(_out, _stop, 0.1, 0.2), daemon=True)
    _thr.start()
    wait_for_rows(_out, 3, timeout=10)          # header + two samples, then stop
    _stop.set()
    _thr.join(timeout=5)
    _rows = [r for r in _csv.reader(open(_out, newline="")) if r]
    c.check("run_win rows match the header width",
            bool(_rows) and all(len(r) == len(_rows[0]) for r in _rows),
            [(i + 1, len(r)) for i, r in enumerate(_rows) if len(r) != len(_rows[0])][:4])
    c.check("run_win writes per-core columns",
            len([h for h in _rows[0] if h.startswith("cpu_core")]) >= 1, _rows[0][-4:])

    # the AMD "GPU Core" load sensor is intermittent (blank in ~half the rows on a
    # Ryzen APU), so the AMD branch must fall back to the D3D engine counter rather
    # than writing a blank gpu_util
    _amd_out = os.path.join(tmpdir(), "amd-branch.telemetry.csv")
    _real_gpu_row = _rw._gpu_row
    _real_lhm = dict(_rw._LHM)
    _real_eng = dict(_rw._GPU_ENGINE)
    try:
        _rw._gpu_row = lambda: None                      # no nvidia-smi answer
        _rw._LHM.clear()
        _rw._LHM.update({"gpu_temp": 58.0, "gpu_power": 26.0, "mem_util": 75.6,
                         "vram_used": 387.0, "gpu_util": None})
        _rw._GPU_ENGINE.update({"3d": 42.0, "copy": 1.0})
        _stop2 = _th.Event()
        _thr2 = _th.Thread(target=_rw.sampler, args=(_amd_out, _stop2, 0.1, 0.2), daemon=True)
        _thr2.start()
        wait_for_rows(_amd_out, 3, timeout=10)
        _stop2.set()
        _thr2.join(timeout=5)
    finally:
        _rw._gpu_row = _real_gpu_row
        _rw._LHM.clear()
        _rw._LHM.update(_real_lhm)
        _rw._GPU_ENGINE.update(_real_eng)
    _arows = [r for r in _csv.reader(open(_amd_out, newline="")) if r]
    _ai = _arows[0].index("gpu_util_pct")
    _avals = [r[_ai] for r in _arows[1:]]
    c.check("the AMD branch falls back to the engine counter for gpu_util",
            bool(_avals) and all(v == "42.0" for v in _avals), set(_avals))
    c.check("the AMD branch still keeps the LHM values it does have",
            _arows[1][_arows[0].index("mem_util_pct")] == "75.6", _arows[1][:6])
    _lout = os.path.join(tmpdir(), "align-linux.telemetry.csv")
    _lp = _sp.Popen([sys.executable, os.path.join(BENCH, "telemetry_log.py"), "--out", _lout,
                     "--interval", "0.1", "--duration", "1"],
                    stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
    wait_for_rows(_lout, 3, timeout=15)         # as soon as rows land, not a fixed 2 s
    _lp.wait(timeout=30)
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
    # (the report must have no gfx record, or that would win over the screenshot)
    gd3 = tmpdir()
    with open(os.path.join(gd3, "host-20260101-000000.json"), "w") as fh:
        json.dump({"host": "h", "phases": [{"group": "gpu", "phase": "matmul",
                                            "result": {"tflops": 1.0}}]}, fh)
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
        # $BENCH_REPO is the explicit override, so it is tried before the bundle dir
        c.check("%s tries $BENCH_REPO before the bundle dir (source order)" % f,
                'cands = [os.environ.get("BENCH_REPO")]' in src
                and "cands.append(os.path.dirname(os.path.abspath(sys.executable)))" in src
                and "cands.insert(0," not in src, f)

    # ---- no canonical field may be a phantom ----
    # mem_used stayed in SAMPLE_FIELDS long after the samplers stopped emitting it: every
    # row carried a permanently null column and the docs advertised it as canonical.
    _seen = set()
    for f in ("run_win.py", "telemetry_log.py"):
        src = open(os.path.join(BENCH, f), encoding="utf-8").read()
        _seen |= {k for k in _SF if k in src}
    _orphan = [k for k in _SF if k not in _seen]
    c.check("every canonical telemetry field is mentioned by a sampler", not _orphan, _orphan)

    # ---- the run's full CSV must reach the server when it ends ----
    class _FakeRep:
        run_id = "run-x"

        def __init__(self):
            self.samples, self.arts = [], []

        def telemetry(self, samples):
            self.samples.extend(samples)
            return 200          # ack, like Reporter.telemetry now does

        def artifact(self, name, text, kind="json"):
            self.arts.append((name, kind, len(text)))
            return 200

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

    # ---- the upload is scoped to THIS run's CSV ----
    # Field failure: a run that died in 4 seconds uploaded the PREVIOUS run's
    # telemetry - 4,995 rows, the previous run's exact count - under its own run
    # id, because the end-of-run upload picked the newest CSV and the failed run
    # wrote none of its own. Scope it with the run's start, like build_summary_dir.
    sdir = tmpdir()
    oldc = os.path.join(sdir, "host-win-prev.telemetry.csv")
    with open(oldc, "w") as fh:
        fh.write("elapsed_s,temp_c\n8.0,40.0\n8.25,41.0\n")
    past = time.time() - 3600
    os.utime(oldc, (past, past))
    fr5 = _FakeRep()
    n5 = upload_telemetry_csv(fr5, sdir, since=time.time() - 5)
    c.check("a run with no CSV of its own uploads nothing",
            n5 == 0 and fr5.samples == [] and fr5.arts == [], (n5, fr5.samples, fr5.arts))
    ownc = os.path.join(sdir, "host-win-own.telemetry.csv")
    with open(ownc, "w") as fh:
        fh.write("elapsed_s,temp_c\n0.0,50.0\n0.25,51.0\n")
    fr6 = _FakeRep()
    n6 = upload_telemetry_csv(fr6, sdir, since=time.time() - 5)
    c.check("a run with its own CSV uploads exactly its rows",
            n6 == 2 and [s["temp_c"] for s in fr6.samples] == [50.0, 51.0], (n6, fr6.samples))
    # a reporter that remembers its start is scoped even without an explicit
    # `since` (the dashboards pass none), so a failed run cannot borrow a CSV
    fr7 = _FakeRep()
    fr7.run_started = time.time() + 60
    c.check("a reporter's own run start scopes the upload",
            upload_telemetry_csv(fr7, sdir) == 0 and fr7.arts == [], fr7.arts)

    # ---- a long CSV is paged; truncation is never silent ----
    bdir = tmpdir()
    with open(os.path.join(bdir, "host-big.telemetry.csv"), "w") as fh:
        fh.write("elapsed_s,temp_c\n")
        for i in range(8):
            fh.write("%d,%d\n" % (i, 40 + i))
    fr8 = _FakeRep()
    n8 = upload_telemetry_csv(fr8, bdir, chunk_rows=3)
    names8 = [a[0] for a in fr8.arts]
    c.check("a long CSV is paged into artifacts named so they sort",
            n8 == 8 and len(names8) == 3 and names8 == sorted(names8)
            and all(nm.startswith("host-big.telemetry.csv") for nm in names8), names8)
    c.check("a small CSV still goes up as one artifact under its own name",
            [a[0] for a in fr6.arts] == ["host-win-own.telemetry.csv"], fr6.arts)
    notes9 = []
    fr9 = _FakeRep()
    n9 = upload_telemetry_csv(fr9, bdir, cap=2, log=notes9.append)
    c.check("a capped upload truncates and says so",
            n9 == 2 and any("truncat" in m for m in notes9), (n9, notes9))

    # a rejected POST/artifact must not be reported as a successful upload
    class _RejectRep(_FakeRep):
        def telemetry(self, batch):
            self.samples.extend(batch)
            return 500

        def artifact(self, name, text, kind="json"):
            self.arts.append((name, kind, len(text)))
            return 500

    notes10 = []
    fr10 = _RejectRep()
    n10 = upload_telemetry_csv(fr10, bdir, log=notes10.append)
    c.check("a rejected upload is not counted as stored",
            n10 == 0 and any("REJECTED" in m for m in notes10), (n10, notes10))

    # the agent scopes its own upload and batches like the pump (no per-row POST)
    asrc = open(os.path.join(BENCH, "agent.py"), encoding="utf-8").read()
    c.check("agent.py scopes the upload to its own run start",
            "upload_telemetry_csv(self.rep, self.out_dir, since=self.run_t0)" in asrc, "")
    c.check("agent.py batches telemetry instead of one POST per row",
            "self.rep.telemetry([row])" not in asrc and "tail.read()" in asrc, "")
    c.check("agent.py reports produced/sent/failed/dropped like the pump",
            all(k in asrc for k in ('"produced"', '"sent"', '"failed"', '"dropped"')), "")

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

    # ---- a RAM integrity result is a verdict, not a number ----
    # It was measured and dropped: the summary had the soak bandwidth but nothing that
    # said whether the memory actually verified.
    rd = tmpdir()
    with open(os.path.join(rd, "host-ram.json"), "w") as fh:
        json.dump({"host": "h", "phases": [
            {"group": "ram", "phase": "integrity",
             "result": {"phase": "integrity", "bad_elements": 0, "chunks": 4,
                        "passes": 2, "planned_chunks": 4, "planned_passes": 2}},
        ]}, fh)
    ri = (build_summary_dir(rd) or {}).get("ram_integrity") or {}
    c.check("the summary carries the RAM integrity result",
            ri.get("chunks") == 4 and ri.get("passes") == 2 and ri.get("bad_elements") == 0,
            ri)
    rd2 = tmpdir()
    with open(os.path.join(rd2, "host-ram.json"), "w") as fh:
        json.dump({"host": "h", "phases": [
            {"group": "ram", "phase": "integrity",
             "result": {"phase": "integrity", "bad_elements": 0, "chunks": 2, "passes": 0,
                        "planned_chunks": 8, "planned_passes": 2,
                        "short": "MemoryError: filled 2 of 8 chunks over 0 of 2 passes"}},
        ]}, fh)
    ri2 = (build_summary_dir(rd2) or {}).get("ram_integrity") or {}
    c.check("a run cut short says so in the summary instead of reading as verified",
            ri2.get("chunks") == 2 and ri2.get("passes") == 0 and ri2.get("short"), ri2)

    # ---- the RAM integrity phase must report what it filled, not what it planned ----
    # A MemoryError broke out of the loop and the record still carried the plan.
    try:
        import ram_bench as rb
    except Exception as e:                      # numpy is not needed by the suite
        skip("the RAM integrity record check", e)
    else:
        real_empty, real_jline = rb.np.empty, rb.jline
        calls = {"n": 0}
        recs = []

        def boom(*a, **k):
            calls["n"] += 1
            if calls["n"] > 3:
                raise MemoryError("simulated")
            return real_empty(*a, **k)

        class _Args:
            fill_pct = 100
            passes = 2

        rb.np.empty = boom
        rb.jline = recs.append
        try:
            rb.phase_integrity(_Args())
        finally:
            rb.np.empty, rb.jline = real_empty, real_jline
        rec = recs[-1]
        c.check("a MemoryError records the chunks it actually filled",
                rec["chunks"] == 3 and rec["passes"] == 0 and rec["planned_chunks"] > 3,
                rec)
        c.check("and it says the run was cut short", bool(rec.get("short")), rec)

    # ---- the closing fsync of the random-IO phases is durability, not speed ----
    # It sat inside the timed block, so an 8-second run carried its own flush in the
    # denominator and reported fewer IOPS than it did.
    try:
        import disk_bench as dbm
    except Exception as e:
        skip("the random-IO timing check", e)
    else:
        class _A:
            seconds = 1.0
            size_mb = 8
            path = tmpdir()

        dbm.ensure_file(_A())                   # so only the closing fsync is slow
        real_fsync = os.fsync

        def slow_fsync(fd):
            time.sleep(0.35)
            return real_fsync(fd)

        os.fsync = slow_fsync
        t0 = time.time()
        try:
            ops, reads, writes, dt = dbm.rand_loop(_A(), 50)
        finally:
            os.fsync = real_fsync
        wall = time.time() - t0
        c.check("random IO runs for its full window and does work",
                dt >= _A.seconds - 0.05 and ops > 0, (dt, ops))
        c.check("the closing fsync is outside the measured time",
                wall - dt >= 0.3, (round(wall, 3), round(dt, 3)))

    # ---- hardware verdicts: this run's reports, and say which window they cover ----
    # The health report scans the last 7 days (Get-EventHealth.ps1 -Days 7), so
    # "unexpected shutdown (Kernel-Power 41): 8" on a run that saw none reads as if it
    # happened during the run - and the counts came from every health report in the
    # results dir, previous runs included.
    from benchreport import _hardware_verdicts
    hd = tmpdir()
    with open(os.path.join(hd, "host.health.json"), "w") as fh:
        json.dump({"host": "h", "days": 7,
                   "summary": [{"id": 41, "provider": "Microsoft-Windows-Kernel-Power",
                                "count": 8},
                               {"id": 153, "provider": "nvlddmkm", "count": 7}]}, fh)
    v = _hardware_verdicts(hd)
    txt = " | ".join(x["text"] for x in v)
    c.check("hardware verdicts say which window the counts cover",
            "in the last 7 days" in txt, txt)
    c.check("and the counts are still reported", "8" in txt and "7" in txt, txt)
    c.check("a health report older than the run is ignored",
            _hardware_verdicts(hd, since=time.time() + 10) == [], "")
    c.check("the good case names the window too",
            "in the last 7 days" in " | ".join(
                x["text"] for x in _hardware_verdicts(hd) if x["level"] == "good") or
            not [x for x in v if x["level"] == "good"], v)

    # ---- the sequential write must flush as it goes ----
    # With one flush at the end the page cache absorbs the whole file and the number
    # measures free RAM: 512 MiB reported 45 MB/s that way and 128 MiB 279 MB/s, while
    # the same machine measured 257-278 MB/s at either size when flushed every 32 MiB.
    try:
        import disk_bench as _dbm
    except Exception as e:
        print("  SKIP the sequential-write flush check (%s)" % e)
    else:
        real_fsync, real_jline = os.fsync, _dbm.jline
        calls = {"n": 0}

        def counting(fd):
            calls["n"] += 1
            return real_fsync(fd)

        class _W:
            size_mb = 96                 # 3 windows of FLUSH_EVERY_MB plus the close
            path = tmpdir()

        os.fsync, _dbm.jline = counting, (lambda o: None)
        try:
            _dbm.phase_seqwrite(_W())
        finally:
            os.fsync, _dbm.jline = real_fsync, real_jline
        c.check("the sequential write flushes as it goes, not once at the end",
                calls["n"] >= 4, calls)

    # ---- the CPU soak plateau decision (mirrors gpu_bench) ----
    try:
        import cpu_bench as _cb
    except Exception as e:
        print("  SKIP the CPU plateau check (%s)" % e)
    else:
        flat = [70.0 + (0.2 if i % 2 else -0.2) for i in range(8)]
        climb = [60.0 + i for i in range(8)]
        c.check("cpu plateau: flat temps are stable", _cb._temp_stable(flat) is True, "")
        c.check("cpu plateau: climbing temps are not stable", _cb._temp_stable(climb) is False, "")
        c.check("cpu plateau: needs a full window", _cb._temp_stable(flat[:7]) is False, "")

        class _SoakArgs:
            seconds = 300
            until_stable = True
            stable_hold = 60.0

        class _NoStable:
            seconds = 300
            until_stable = False
            stable_hold = 60.0

        t0 = time.time() - 50
        keep, ss = _cb._soak_decision(t0, _SoakArgs(), flat, None)
        c.check("cpu soak: a plateau starts the hold, it does not stop the soak",
                keep is True and ss is not None, (keep, ss))
        c.check("cpu soak: keeps going through the hold",
                _cb._soak_decision(t0, _SoakArgs(), flat, ss) == (True, ss), "")
        c.check("cpu soak: stops once the hold has elapsed",
                _cb._soak_decision(t0, _SoakArgs(), flat, ss - 60)[0] is False, "")
        c.check("cpu soak: moving temps reset the hold",
                _cb._soak_decision(t0, _SoakArgs(), climb, ss) == (True, None), "")
        c.check("cpu soak: honours the ceiling",
                _cb._soak_decision(time.time() - 400, _SoakArgs(), flat, None)[0] is False, "")
        c.check("cpu soak: no early stop when not requested",
                _cb._soak_decision(t0, _NoStable(), flat, None) == (True, None), "")
        c.check("cpu soak: never stops before the minimum",
                _cb._soak_decision(time.time() - 10, _SoakArgs(), flat, None) == (True, None), "")
        _cbsrc = open(os.path.join(BENCH, "cpu_bench.py"), encoding="utf-8").read()
        c.check("the CPU soak records whether the plateau was reached and the hold",
                all(k in _cbsrc for k in ('"until_stable"', '"stable":', '"stable_at_s"',
                                          '"stable_hold_s"')), "")

    # ---- the sustained write keeps every fsync-bounded window ----
    try:
        import disk_bench as _sdb
    except Exception as e:
        print("  SKIP the sustained-write check (%s)" % e)
    else:
        wb = 64 << 20
        st = _sdb._window_stats([1.0, 1.0, 2.0, 8.0], wb)
        c.check("sustained windows: one throughput per flush-bounded window",
                st["windows"] == 4 and st["windows_mbps"] == [67.1, 67.1, 33.6, 8.4], st)
        c.check("sustained windows: first/last are the window edges, not the average",
                st["first_mbps"] == 67.1 and st["last_mbps"] == 8.4, st)
        c.check("sustained windows: the post-cache collapse shows as a drop",
                st["drop_pct"] == 87.5 and st["min_mbps"] == 8.4, st)
        c.check("sustained windows: an empty run has no fabricated numbers",
                _sdb._window_stats([], wb) == {} and _sdb._window_stats([0.0], wb) == {}, "")
        # off by default: the phase refuses to run without --sustain-gb
        recs = []
        real_jline = _sdb.jline
        _sdb.jline = recs.append
        try:
            class _Off:
                sustain_gb = 0.0
                path = tmpdir()
            _sdb.phase_sustainwrite(_Off())
        finally:
            _sdb.jline = real_jline
        c.check("sustained write is opt-in (off by default)",
                bool(recs) and recs[-1].get("skipped") is True
                and "sustain-gb" in (recs[-1].get("reason") or ""), recs[-1:])

    # ---- the thread-scaling curve: GFLOPS per count + efficiency vs 1 thread ----
    try:
        import cpu_bench as _sb
    except Exception as e:
        print("  SKIP the scaling-curve check (%s)" % e)
    else:
        c.check("scaling efficiency: linear scaling is 1.0",
                _sb._scaling_efficiency(100.0, 4, 25.0) == 1.0, "")
        c.check("scaling efficiency: half-linear is 0.5",
                _sb._scaling_efficiency(50.0, 4, 25.0) == 0.5, "")
        c.check("scaling efficiency: no 1-thread baseline is None, not zero",
                _sb._scaling_efficiency(50.0, 4, None) is None, "")

        class _FakeTime:
            def __init__(self):
                self.t = 0.0

            def time(self):
                self.t += 1.0
                return self.t

        real_time = _sb.time
        _sb.time = _FakeTime()
        try:
            r = _sb.timed(3.0, lambda: None, flops=3e9)
        finally:
            _sb.time = real_time
        c.check("scaling curve maths: a fake timer gives iters/seconds/GFLOPS",
                r["iters"] == 2 and r["seconds"] == 4.0 and r["gflops"] == 1.5, r)

        fake = {1: 20.0, 2: 38.0, 4: 40.0}
        real_child, real_jline = _sb._run_scaling_child, _sb.jline
        recs = []
        _sb._run_scaling_child = lambda n, seconds, k: {
            "phase": "scaling-one", "threads": k, "n": n, "gflops": fake.get(k)}
        _sb.jline = recs.append
        try:
            class _Sc:
                size = 1024
                seconds = 1.0
            _sb.phase_scaling(_Sc())
        finally:
            _sb._run_scaling_child, _sb.jline = real_child, real_jline
        rec = recs[-1]
        c.check("scaling curve: per-count GFLOPS are recorded with the size",
                rec["n"] == 1024 and rec["results"]["2"]["gflops"] == 38.0
                and rec["all_cores"] == (os.cpu_count() or 1), rec)
        c.check("scaling curve: efficiency is relative to the 1-thread run",
                rec["efficiency"]["1"] == 1.0 and rec["efficiency"]["2"] == 0.95
                and rec["efficiency"]["4"] == 0.5, rec.get("efficiency"))

    # ---- a gfx tail reaches the summary; a missing tail is missing, not zero ----
    td = tmpdir()
    with open(os.path.join(td, "host-furmark-20260101-000000.json"), "w") as fh:
        json.dump({"host": "h", "timestamp": "x", "engine": "furmark",
                   "result": {"phase": "furmark", "score": 900, "fps_min": 18.5,
                              "fps_avg": 44.0, "fps_max": 61.0}}, fh)
    g = build_summary_dir(td).get("gfx") or {}
    c.check("the FurMark min/avg/max tail reaches the summary",
            (g.get("fps_min"), g.get("fps_avg"), g.get("fps_max")) == (18.5, 44.0, 61.0), g)
    td2 = tmpdir()
    with open(os.path.join(td2, "host-linux-20260101-000000.json"), "w") as fh:
        json.dump({"host": "h", "phases": [
            {"group": "gfx", "phase": "furmark",
             "result": {"phase": "furmark", "score": 500, "fps_min": 10.0,
                        "fps_avg": 30.0, "fps_max": 45.0}}]}, fh)
    g2 = build_summary_dir(td2).get("gfx") or {}
    c.check("the Linux-shaped gfx record keeps the tail too",
            (g2.get("fps_min"), g2.get("fps_max")) == (10.0, 45.0), g2)
    # an engine that reports only an average must not gain a fabricated tail
    td3 = tmpdir()
    with open(os.path.join(td3, "host-valley-20260101-000000.json"), "w") as fh:
        json.dump({"host": "h", "phases": [
            {"group": "gfx", "phase": "valley",
             "result": {"phase": "valley", "score": 100, "fps_avg": 30.0}}]}, fh)
    g3 = build_summary_dir(td3).get("gfx") or {}
    c.check("an average-only engine records no tail rather than zero",
            g3.get("fps_min") is None and g3.get("min_fps") is None and "min_fps" not in g3, g3)
    c.check("the average it does have is still kept", g3.get("fps_avg") == 30.0, g3)

    # ---- the runners wire the new switches through ----
    cb = open(os.path.join(BENCH, "Invoke-CpuBench.ps1"), encoding="utf-8").read()
    c.check("the CPU runner offers the plateau switch",
            "-UntilStable" in cb and "-StableHold" in cb
            and "--until-stable" in cb and "--stable-hold" in cb, "")
    c.check("the CPU runner runs the scaling curve", "$phases.scaling" in cb, "")
    wsb = open(os.path.join(BENCH, "Run-WorkstationBench.ps1"), encoding="utf-8").read()
    c.check("the battery forwards the plateau flags to the CPU bench",
            "-UntilStable:$SoakUntilStable -StableHold $StableHold" in wsb, "")
    db = open(os.path.join(BENCH, "Invoke-DiskBench.ps1"), encoding="utf-8").read()
    c.check("the disk runner exposes -SustainGB as an opt-in",
            "$SustainGB" in db and "--sustain-gb" in db and "sustainwrite" in db, "")
    c.check("the battery manifest carries the gfx frame-time tail",
            "fps_min = $gr.fps_min" in wsb and "min/avg/max" in wsb, "")
    c.check("an average-only engine is marked, not given a tail",
            "unavailable (average only)" in wsb, "")

    # ---- the telemetry pipeline must not lose rows silently ----
    import queue as _q

    class _FakeRep:
        def __init__(self):
            self.batches = []

        def telemetry(self, batch):
            self.batches.append(list(batch))
            return 200          # ack, like Reporter.telemetry now does

        def log(self, lines, stream="out"):
            pass

        def heartbeat(self):
            pass

        def poll_commands(self):
            return []

    fp = _FakeRep()
    pump = ReporterPump(fp)
    pump.samples = _q.Queue(maxsize=3)
    for i in range(5):
        pump.push_sample({"elapsed": i})
    c.check("a full telemetry queue drops the oldest and counts it",
            pump.dropped == 2 and pump.produced == 5 and pump.samples.qsize() == 3,
            pump.stats())
    pump._drain()
    c.check("the drain sends every queued row and counts it",
            pump.sent == 3 and len(fp.batches) == 1 and len(fp.batches[0]) == 3,
            pump.stats())
    c.check("stats() reports the pipeline honestly",
            pump.stats()["queued"] == 0 and pump.stats()["dropped"] == 2, pump.stats())

    # telemetry drains every tick; heartbeat + commands every other tick
    class _Cadence:
        def __init__(self):
            self.drains, self.beats, self.polls = 0, 0, 0

        def telemetry(self, batch):
            self.drains += 1

        def log(self, lines, stream="out"):
            pass

        def heartbeat(self):
            self.beats += 1

        def poll_commands(self):
            self.polls += 1
            return []

    cad = _Cadence()
    p3 = ReporterPump(cad, interval=0.05)
    calls = {"drain": 0}
    real_drain = p3._drain

    def counting_drain():
        calls["drain"] += 1
        real_drain()

    p3._drain = counting_drain
    c.check("the pump ticks telemetry at 1s by default (source default)",
            ReporterPump(_FakeRep()).interval == 1.0, ReporterPump(_FakeRep()).interval)
    p3.start()
    time.sleep(0.55)
    p3.stop()
    c.check("telemetry drains on every tick, the heartbeat every other tick",
            calls["drain"] > cad.beats and cad.polls == cad.beats,
            (calls["drain"], cad.beats, cad.polls))

    # `sent` must count ACKNOWLEDGED rows: Reporter.telemetry fails soft (status
    # 0 when unreachable, 4xx/5xx on rejection), and the old pump counted the
    # attempted POST as sent while the server stored nothing.
    class _RejectRep2:
        def __init__(self):
            self.batches = []

        def telemetry(self, batch):
            self.batches.append(list(batch))
            return 504

        def log(self, lines, stream="out"):
            pass

        def heartbeat(self):
            pass

        def poll_commands(self):
            return []

    rp = _RejectRep2()
    pump2 = ReporterPump(rp)
    pump2.push_sample({"elapsed": 0})
    pump2.push_sample({"elapsed": 1})
    pump2._drain()
    c.check("a failed POST is counted as failed, not sent",
            pump2.sent == 0 and pump2.failed == 2 and pump2.stats()["failed"] == 2,
            pump2.stats())

    # ---- a phase that reports its own elapsed time is believed ----
    class _Rec:
        def __init__(self):
            self.phases = []

        def phase(self, seq, group, label, status="running", t0=None, t1=None, detail=None):
            self.phases.append((group, label, status, t0, t1))

    rec = _Rec()
    tr = PhaseTracker(rec)
    tr.feed("[gpu] soak")
    time.sleep(0.2)
    tr.feed('RESULT_JSON:{"phase": "soak", "seconds": 622.0}')
    tr.feed("[gpu] streams")
    closed = [p for p in rec.phases if p[2] != "running"]
    dur = (closed[0][4] - closed[0][3]) if closed and closed[0][3] else None
    c.check("a phase that reports its own elapsed time is believed",
            dur is not None and abs(dur - 622.0) < 0.01, (closed, dur))

    rec2 = _Rec()
    tr2 = PhaseTracker(rec2)
    t_start = time.time()
    tr2.feed("[gpu] matmul")
    time.sleep(0.15)
    tr2.feed("[gpu] membw")
    closed2 = [p for p in rec2.phases if p[2] != "running"]
    c.check("a phase with no own clock still closes on the wall clock",
            closed2 and closed2[0][4] - t_start >= 0.1, (closed2, t_start))

    rec3 = _Rec()
    tr3 = PhaseTracker(rec3)
    tr3.feed("[gpu] soak")
    tr3.feed("RESULT_JSON:not json at all")
    tr3.feed("[gpu] streams")
    c.check("a malformed RESULT_JSON does not break the tracker",
            any(p[2] != "running" for p in rec3.phases), rec3.phases)

    srv.stop()
    return c.fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
