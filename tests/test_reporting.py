"""Reporting-client tests: Reporter, PhaseTracker, ReporterPump, system info,
summary/inventory builders, and the remote-exec helper."""
import json
import os
import sys
import tempfile
import time

from harness import Checker, Server, REPO, BENCH
from benchreport import (Reporter, PhaseTracker, ReporterPump, build_summary_dir,
                         telemetry_summary, system_info, read_inventory, run_exec)


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
    d = tempfile.mkdtemp()
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
    hv = tempfile.mkdtemp()
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
    d2 = tempfile.mkdtemp()
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

    srv.stop()
    return c.fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
