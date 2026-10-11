#!/usr/bin/env python3
"""import_results.py - backfill existing bench-test results into a benchd server.

Walks a results directory and creates one run per completed session:

  * Linux  - a run directory containing a `*-linux-*.json` report (and
             `runs-index.jsonl` for the summary row + `*.telemetry.csv`).
  * Windows- a `*.manifest.json` session; the sibling per-group reports are
             merged for the summary and the 1 Hz telemetry CSV is attached.

    python3 server/import_results.py --server http://lab:8722 --token XXX --dir results
    python3 server/import_results.py ... --dry-run

Stdlib only.
"""
import argparse
import base64
import csv
import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

SAMPLE_FIELDS = ["elapsed", "temp_c", "fan_pct", "power_w", "gpu_util",
                 "gpu_fan_rpm", "cpu_util", "ram_used", "sm_mhz", "mem_used"]
CSV_ALIASES = {
    "elapsed_s": "elapsed", "elapsed": "elapsed", "temp_c": "temp_c", "gpu_temp_c": "temp_c",
    "fan_pct": "fan_pct", "gpu_fan_pct": "fan_pct", "power_w": "power_w", "gpu_power_w": "power_w",
    "gpu_util_pct": "gpu_util", "util_pct": "gpu_util", "gpu_util": "gpu_util",
    "gpu_fan_rpm": "gpu_fan_rpm", "fan_rpm": "gpu_fan_rpm", "cpu_util_pct": "cpu_util",
    "cpu_util": "cpu_util", "ram_used_gib": "ram_used", "ram_used": "ram_used",
    "sm_mhz": "sm_mhz", "sm_clock_mhz": "sm_mhz", "mem_used_gib": "mem_used", "vram_used_gib": "mem_used",
}
INFRA = re.compile(r"\.(info|sysinfo|sensors|pcie|health|env)\.json$")


def post(server, token, path, body=None, method="POST"):
    req = urllib.request.Request(server.rstrip("/") + path, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("User-Agent", "bench-import")
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data, timeout=60) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:200]}
    except Exception as e:
        return 0, {"error": str(e)}


def load(p):
    try:
        with open(p, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:
        return None


def epoch(iso):
    if not iso:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return None


def read_telemetry(path, cap=1500):
    rows = []
    try:
        with open(path, encoding="utf-8-sig") as fh:
            header = fh.readline().strip().split(",")
            cols = {}
            for i, n in enumerate(header):
                c = CSV_ALIASES.get(n.strip().lower())
                if c:
                    cols[c] = i
            for line in fh:
                v = line.rstrip("\n").split(",")
                if not v or v[0].strip() == "":
                    continue
                row = []
                for f in SAMPLE_FIELDS:
                    i = cols.get(f)
                    try:
                        row.append(float(v[i]) if (i is not None and i < len(v) and v[i] != "") else None)
                    except ValueError:
                        row.append(None)
                rows.append(row)
    except OSError:
        return []
    if len(rows) > cap:
        step = len(rows) / float(cap)
        rows = [rows[min(len(rows) - 1, int(i * step))] for i in range(cap)]
    return rows


def upload_run(server, token, run_id, phases, telemetry, artifacts, summary, dry=False):
    for i, ph in enumerate(phases):
        post(server, token, "/api/v1/runs/%s/phase" % run_id,
             {"seq": i + 1, "group": ph[0], "label": ph[1], "status": "done",
              "t0": ph[2], "t1": ph[3]})
    for j in range(0, len(telemetry), 200):
        post(server, token, "/api/v1/runs/%s/telemetry" % run_id, {"samples": telemetry[j:j + 200]})
    for name, kind, text in artifacts:
        post(server, token, "/api/v1/runs/%s/artifact" % run_id,
             {"name": name, "kind": kind, "content": base64.b64encode(text.encode()).decode()})
    post(server, token, "/api/v1/runs/%s/finish" % run_id,
         {"status": "done", "exit_code": 0, "summary": summary})


# --------------------------------------------------------------- windows -----
def windows_units(root):
    for man in sorted(glob.glob(os.path.join(root, "*.manifest.json"))):
        m = load(man)
        if not m:
            continue
        host = m.get("host") or "win"
        started = epoch(m.get("timestamp")) or os.path.getmtime(man)
        yield ("win", man, host, started, m)


def build_windows(root, man, host, started, m, window_end):
    arts = {k: v for k, v in (m.get("artifacts") or {}).items() if isinstance(v, str)}
    base = {}
    for k, p in arts.items():
        j = load(os.path.join(root, os.path.basename(p)))
        if isinstance(j, dict):
            base[k] = j
    # the aggregate report is a top-level <host>-<stamp>.json not in the manifest
    main = None
    for p in glob.glob(os.path.join(root, host + "-*.json")):
        if INFRA.search(p) or p.endswith(".manifest.json"):
            continue
        mt = os.path.getmtime(p)
        if started - 120 <= mt <= window_end:
            j = load(p)
            if isinstance(j, dict) and isinstance(j.get("phases"), list):
                main = j
    summary = {"host": host, "timestamp": m.get("timestamp"), "imported": True}
    reports = ([main] if main else []) + list(base.values())
    _merge_summary(summary, reports)
    # phases: gpu sub-phases from the main report, else one per group artifact
    phases = []
    t = started
    if main:
        for ph in main.get("phases", []):
            phases.append(("gpu", ph.get("phase"), t, t + 15))
            t += 15
    else:
        phases.append(("sys", "system + GPU inventory", t, t + 20))
        t += 20
    for grp, key, label in [("cpu", "cpu", "matmul + soak"), ("combined", "combined", "GPU+CPU"),
                            ("nvenc", "nvenc", "video encode"), ("ram", "ram", "bandwidth + soak"),
                            ("disk", "disk", "sequential + random"), ("net", "net", "latency + throughput"),
                            ("fullsoak", "fullsoak", "full-system soak")]:
        if key in base:
            phases.append((grp, label, t, t + 30))
            t += 30
    if any("valley" in k for k in arts) or glob.glob(os.path.join(root, "valley-*.png")):
        phases.append(("gfx", "graphics (Unigine Valley)", t, t + 60))
    tele = []
    for p in glob.glob(os.path.join(root, host + "-win-*.telemetry.csv")):
        if started - 120 <= os.path.getmtime(p) <= window_end:
            tele = read_telemetry(p)
            break
    arts_out = []
    if main:
        arts_out.append(("report.json", "json", json.dumps(main)))
    arts_out.append(("manifest.json", "json", json.dumps(m)))
    return phases, tele, arts_out, summary


def _merge_summary(s, reports):
    def setd(k, v):
        if v is not None and k not in s:
            s[k] = v
    for j in reports:
        if not isinstance(j, dict):
            continue
        # flat network report
        if j.get("download_mbps") is not None and "net" not in s:
            s["net"] = {k: j.get(k) for k in ("download_mbps", "upload_mbps", "ping_target",
                                              "ping_target_ms", "ping_gateway_ms")}
        ph = j.get("phases")
        if isinstance(ph, list):
            items = ph
        elif isinstance(ph, dict):
            items = list(ph.values())
        else:
            items = []
        for p in items:
            if not isinstance(p, dict):
                continue
            r = p.get("result") if isinstance(p.get("result"), dict) else p
            phase = p.get("phase") or r.get("phase")
            if phase == "matmul" and isinstance(r.get("results"), dict):
                sub = r["results"]
                first = next(iter(sub.values()), {})
                if isinstance(first, dict) and "tflops" in first:
                    setd("gpu_matmul_tflops", {k: v.get("tflops") for k, v in sub.items()})
                elif isinstance(first, dict) and "gflops" in first:
                    setd("cpu_matmul_gflops", {k: v.get("gflops") for k, v in sub.items()})
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
        if isinstance(j.get("verdict"), dict) and "fullsoak" not in s:
            s["fullsoak"] = {"result": {"verdict": j["verdict"]}}
        elif isinstance(j.get("gpu"), dict) and isinstance(j.get("cpu"), dict) and "combined" not in s:
            s["combined"] = {"gpu": j.get("gpu"), "cpu": j.get("cpu")}


# ----------------------------------------------------------------- linux -----
def linux_units(root):
    for d in sorted(glob.glob(os.path.join(root, "**"), recursive=True)):
        if not os.path.isdir(d) or not os.path.isfile(os.path.join(d, "runs-index.jsonl")):
            continue
        reports = [p for p in glob.glob(os.path.join(d, "*-linux-*.json"))
                   if not INFRA.search(p) and not p.endswith(".manifest.json")]
        if not reports:
            continue
        rep = max(reports, key=os.path.getmtime)
        j = load(rep)
        if not isinstance(j, dict):
            continue
        yield (d, rep, j)


def build_linux(d, rep, j):
    host = j.get("host")
    started = epoch(j.get("timestamp")) or os.path.getmtime(rep)
    summary = {}
    idx = os.path.join(d, "runs-index.jsonl")
    if os.path.isfile(idx):
        try:
            lines = [ln for ln in open(idx, encoding="utf-8") if ln.strip()]
            if lines:
                summary = json.loads(lines[-1])
        except Exception:
            summary = {}
    if not summary:
        summary = {"host": host, "timestamp": j.get("timestamp")}
    summary["imported"] = True
    phases = []
    t = started
    for ph in j.get("phases", []) if isinstance(j.get("phases"), list) else []:
        phases.append((ph.get("group") or "gpu", ph.get("phase"), t, t + 15))
        t += 15
    tele = []
    for p in glob.glob(os.path.join(d, "*.telemetry.csv")):
        tele = read_telemetry(p)
        break
    arts = [("report.json", "json", json.dumps(j))]
    if os.path.isfile(idx):
        arts.append(("runs-index.jsonl", "text", open(idx, encoding="utf-8").read()))
    return host, started, phases, tele, arts, summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=os.environ.get("BENCHD_SERVER", ""))
    ap.add_argument("--token", default=os.environ.get("BENCHD_TOKEN", ""))
    ap.add_argument("--dir", default="results")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reset", action="store_true", help="delete existing runs first")
    a = ap.parse_args()
    if not a.server or not a.token:
        print("need --server and --token"); return 2
    root = os.path.abspath(a.dir)

    if a.reset and not a.dry_run:
        _, r = post(a.server, a.token, "/api/v1/runs?limit=1000", method="GET")
        ids = [x["id"] for x in r.get("runs", [])]
        for rid in ids:
            post(a.server, a.token, "/api/v1/runs/%s" % rid, method="DELETE")
        print("reset: deleted %d existing runs" % len(ids))

    wins = list(windows_units(root))
    win_starts = [w[3] for w in wins] + [float("inf")]
    made = 0
    for i, (kind, man, host, started, m) in enumerate(wins):
        window_end = win_starts[i + 1] - 1
        phases, tele, arts, summary = build_windows(root, man, host, started, m, window_end)
        label = "Windows battery"
        print("win  %-32s phases=%-3d tele=%-5d metrics=%d" %
              (os.path.basename(man), len(phases), len(tele), len(summary) - 3))
        if a.dry_run:
            continue
        st, r = post(a.server, a.token, "/api/v1/runs",
                     {"host": host, "label": label, "profile": {"imported": True},
                      "started": started, "platform": "Windows"})
        run_id = r.get("run_id")
        if not run_id:
            print("  FAIL create:", r); continue
        upload_run(a.server, a.token, run_id, phases, tele, arts, summary)
        made += 1

    for d, rep, j in linux_units(root):
        host, started, phases, tele, arts, summary = build_linux(d, rep, j)
        if not host:
            print("linux %-32s skipped (no host in report)" % os.path.basename(d))
            continue
        print("linux %-32s phases=%-3d tele=%-5d" % (os.path.basename(d), len(phases), len(tele)))
        if a.dry_run:
            continue
        st, r = post(a.server, a.token, "/api/v1/runs",
                     {"host": host, "label": "Linux battery", "profile": {"imported": True},
                      "started": started, "platform": "Linux",
                      "gpu": summary.get("gpu"), "driver": summary.get("driver"),
                      "torch": summary.get("torch")})
        run_id = r.get("run_id")
        if not run_id:
            print("  FAIL create:", r); continue
        upload_run(a.server, a.token, run_id, phases, tele, arts, summary)
        made += 1

    print("imported %d runs%s" % (made, " (dry run)" if a.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
