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
    for f in ("gui.py", "tui.py", "agent.py"):
        src = open(os.path.join(BENCH, f), encoding="utf-8").read()
        c.check("%s attaches the run's files" % f, "upload_run_files(" in src, f)

    srv.stop()
    return c.fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
