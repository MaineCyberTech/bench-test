#!/usr/bin/env python3
"""import_results.py - backfill existing bench-test results into a benchd server.

Walks a results directory and creates one run per completed session:

  * Linux  - a run directory containing a `*-linux-*.json` report (and
             `runs-index.jsonl` for the summary row + `*.telemetry.csv`).
  * Windows- a `*.manifest.json` session; the sibling per-group reports are
             merged for the summary and the 1 Hz telemetry CSV is attached.

    python3 server/import_results.py --server http://lab:8722 --token XXX --dir results
    python3 server/import_results.py ... --dry-run
    python3 server/import_results.py ... --reset

Re-running the importer is a no-op: each unit gets a deterministic run id derived
from its host + start time + source, so a run already on the server is skipped
instead of inserted again. `--reset` deletes the existing runs first, which
forces a fresh re-import from scratch.

The token is visible in `ps` when passed as `--token`; prefer $BENCHD_TOKEN or
`--token-file PATH` (first line). Each unit gets a deterministic run id, so
re-running over the same directory is a no-op instead of duplicating every run;
`--reset` deletes every existing run first (all pages, not just the first 1000).

Stdlib only.
"""
import argparse
import base64
import csv
import glob
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request

# share the runner's alias table + merge/verdict logic so backfilled runs are
# identical to live ones (there used to be a second, thinner copy here)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "bench"))
from benchreport import (CSV_ALIASES as _ALIASES, _merge_into,  # noqa: E402
                         _hardware_verdicts)

SAMPLE_FIELDS = ["elapsed", "temp_c", "fan_pct", "power_w", "gpu_util",
                 "gpu_fan_rpm", "cpu_util", "ram_used", "sm_mhz"]
CSV_ALIASES = _ALIASES
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
    """Parse an ISO timestamp (incl. PowerShell 7-digit fractions and trailing Z)
    or a numeric epoch. Returns None when it cannot be understood."""
    if iso is None or iso == "":
        return None
    from datetime import datetime, timezone
    s = str(iso).strip()
    try:
        return float(s)                       # already an epoch
    except ValueError:
        pass
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)     # trim >6 fractional digits
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    if "T" not in s and " " in s:
        s = s.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _dur(s):
    s = int(s or 0)
    return ("%dh%02dm" % (s // 3600, (s % 3600) // 60) if s >= 3600
            else "%dm%02ds" % (s // 60, s % 60))


def import_id(kind, *parts):
    """Deterministic run id for one result unit (host + source + start).

    The server's POST /runs is idempotent on a client-supplied id, so a second
    import of the same directory adopts the existing run instead of minting a
    fresh UUID and duplicating it.
    """
    h = hashlib.sha1("|".join([kind] + [str(p) for p in parts]).encode("utf-8")).hexdigest()
    return "imp-%s-%s" % (kind, h[:20])


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


def read_telemetry(path, cap=1500, t0=None):
    """Parse a 1 Hz CSV into named samples (all mapped columns, not just the core).

    Timestamps are reconstructed from the run start + elapsed so the history and
    trend charts line up instead of collapsing onto the import wall-clock.
    """
    rows = []
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rdr = csv.reader(fh)
            header = next(rdr, None)
            if not header:
                return []
            cols = {}
            for i, n in enumerate(header):
                c = CSV_ALIASES.get(n.strip().lower())
                if c and c != "time":
                    cols[c] = i
            for v in rdr:
                if not v or v[0].strip() == "":
                    continue
                s = {}
                for c, i in cols.items():
                    if i < len(v) and v[i] != "":
                        try:
                            fv = float(v[i])
                        except ValueError:
                            continue
                        if not math.isfinite(fv):
                            # nan/inf from a CSV would be stored and then emitted
                            # as invalid JSON (the dashboard's JSON.parse throws)
                            continue
                        s[c] = fv
                if not s:
                    continue
                if t0 is not None and isinstance(s.get("elapsed"), float):
                    s["ts"] = t0 + s["elapsed"]
                rows.append(s)
    except OSError:
        return []
    if len(rows) > cap:
        step = len(rows) / float(cap)
        rows = [rows[min(len(rows) - 1, int(i * step))] for i in range(cap)]
    return rows


def _win_system(main, base):
    si = base.get("sysinfo") or {}
    os_ = si.get("OS") or {}
    cpu = si.get("CPU") or {}
    ram = si.get("RAM") or {}
    inf = (main or {}).get("info") or {}
    gpu = inf.get("device")
    driver = None
    nv = si.get("nvidia") or {}
    if isinstance(nv, dict):
        driver = nv.get("driver") or nv.get("DriverVersion") or nv.get("version")
    if not gpu:
        gpus = si.get("GPUs")
        if isinstance(gpus, list) and gpus and isinstance(gpus[0], dict):
            gpu = gpus[0].get("name") or gpus[0].get("Name")
    inv = si if si else {"source": "report", "gpu": gpu, "cuda": inf.get("cuda"),
                         "torch": inf.get("torch"), "arch": inf.get("arch")}
    return {"os": "Windows", "platform": os_.get("caption") or "Windows",
            "manufacturer": si.get("Manufacturer"), "model": si.get("Model"),
            "board": si.get("Board"), "cpu": cpu.get("name"),
            "cores": cpu.get("cores"), "threads": cpu.get("threads"),
            "ram_gib": ram.get("total_gib"), "gpu": gpu, "driver": driver,
            "inventory": inv}


def _linux_system(j):
    res = {}
    for ph in (j.get("phases") if isinstance(j.get("phases"), list) else []):
        if ph.get("group") == "sys" and isinstance(ph.get("result"), dict):
            res = ph["result"]
            break
    g = res.get("gpu") or {}
    c = res.get("cpu") or {}
    return {"os": "Linux", "platform": res.get("distro"), "kernel": res.get("kernel"),
            "cpu": c.get("model"), "threads": c.get("logical_cpus"),
            "ram_gib": res.get("memory_gib"), "gpu": g.get("name"),
            "driver": g.get("driver"), "vram": g.get("vram"),
            "storage": res.get("storage"), "network": res.get("network_ifaces"),
            "inventory": res}


def upload_run(server, token, run_id, phases, telemetry, artifacts, summary,
               ended=None, system=None, dry=False):
    failed = []

    def _post(label, path, body):
        st, _ = post(server, token, path, body)
        if st not in (200, 201):
            failed.append("%s -> %s" % (label, st))
        return st

    for i, ph in enumerate(phases):
        _post("phase", "/api/v1/runs/%s/phase" % run_id,
              {"seq": i + 1, "group": ph[0], "label": ph[1], "status": "done",
               "t0": ph[2], "t1": ph[3]})
    for j in range(0, len(telemetry), 200):
        _post("telemetry", "/api/v1/runs/%s/telemetry" % run_id,
              {"samples": telemetry[j:j + 200]})
    for name, kind, text in artifacts:
        _post("artifact", "/api/v1/runs/%s/artifact" % run_id,
              {"name": name, "kind": kind, "content": base64.b64encode(text.encode()).decode()})
    _post("finish", "/api/v1/runs/%s/finish" % run_id,
          {"status": "done", "exit_code": 0, "summary": summary,
           "ended": ended, "system": system or {}})
    return failed


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
    mtimes = []
    for k, p in arts.items():
        fp = os.path.join(root, os.path.basename(p))
        j = load(fp)
        if isinstance(j, dict):
            base[k] = j
            try:
                mtimes.append(os.path.getmtime(fp))
            except OSError:
                pass
    # the aggregate report is exactly <host>-<YYYYMMDD-HHMMSS>.json
    main = None
    main_mt = None
    main_re = re.compile(r"^%s-\d{8}-\d{6}\.json$" % re.escape(host))
    for p in glob.glob(os.path.join(root, host + "-*.json")):
        if not main_re.match(os.path.basename(p)):
            continue
        mt = os.path.getmtime(p)
        if started - 120 <= mt <= window_end:
            j = load(p)
            if isinstance(j, dict) and isinstance(j.get("phases"), list):
                if main is None or mt > (main_mt or 0):
                    main, main_mt = j, mt
    if main_mt:
        mtimes.append(main_mt)
    summary = {"host": host, "timestamp": m.get("timestamp"), "imported": True}
    reports = ([main] if main else []) + list(base.values())
    _merge_summary(summary, reports)
    vs = _hardware_verdicts(root)
    if vs:
        summary["verdicts"] = vs
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
        mt = os.path.getmtime(p)
        if started - 120 <= mt <= window_end:
            tele = read_telemetry(p, t0=started)
            mtimes.append(mt)
            break
    ended = max(mtimes) if mtimes else started + 60
    if ended <= started:
        ended = started + 60
    system = _win_system(main, base)
    summary["gpu"] = system.get("gpu")
    summary["driver"] = system.get("driver")
    if tele:
        def col(name):
            # read_telemetry returns a list of DICTS, not positional rows: indexing
            # one by number raised KeyError and aborted the whole import on any
            # Windows session that had telemetry in the window
            vals = [r.get(name) for r in tele if r.get(name) is not None]
            return [min(vals), round(sum(vals) / len(vals), 1), max(vals)] if vals else None
        summary["telemetry"] = {"samples": len(tele), "duration_s": round(ended - started, 1),
                                "temp_c": col("temp_c"), "fan_pct": col("fan_pct"),
                                "power_w": col("power_w"), "gpu_util": col("gpu_util")}
    arts_out = []
    if main:
        arts_out.append(("report.json", "json", json.dumps(main)))
    arts_out.append(("manifest.json", "json", json.dumps(m)))
    return phases, tele, arts_out, summary, ended, system


def _merge_summary(s, reports):
    """Merge the run's reports with the shared logic (same as a live run)."""
    for j in reports:
        _merge_into(s, j)


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
    dur = (j.get("telemetry") or {}).get("duration_s") or 0
    ended = started + dur if dur else started + 60
    # the CSV of THIS session: like the Windows path, only files whose mtime
    # falls inside the run's window count - the first glob match used to attach
    # an arbitrary (possibly stale) session's telemetry
    window_end = (started + dur + 120) if dur else (started + 8 * 3600)
    tele, best_mt = [], None
    for p in glob.glob(os.path.join(d, "*.telemetry.csv")):
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        if started - 120 <= mt <= window_end and (best_mt is None or mt > best_mt):
            tele, best_mt = read_telemetry(p, t0=started), mt
    _merge_into(summary, j)
    vs = _hardware_verdicts(d)
    if vs:
        summary["verdicts"] = vs
    arts = [("report.json", "json", json.dumps(j))]
    if os.path.isfile(idx):
        arts.append(("runs-index.jsonl", "text", open(idx, encoding="utf-8").read()))
    system = _linux_system(j)
    return host, started, phases, tele, arts, summary, ended, system


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=os.environ.get("BENCHD_SERVER", ""))
    ap.add_argument("--token", default=None,
                    help="operator token (prefer $BENCHD_TOKEN or --token-file: argv is "
                         "visible to every process on the host)")
    ap.add_argument("--token-file", default=os.environ.get("BENCHD_TOKEN_FILE") or "",
                    help="read the token from a file (first line); $BENCHD_TOKEN_FILE works too")
    ap.add_argument("--dir", default="results")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reset", action="store_true",
                    help="delete existing runs first, then re-import from scratch")
    a = ap.parse_args()
    if a.token_file and not a.token:
        try:
            with open(a.token_file, encoding="utf-8") as fh:
                text = fh.read().strip()
        except OSError as e:
            print("cannot read --token-file %s: %s" % (a.token_file, e))
            return 2
        a.token = text.splitlines()[0].strip() if text else ""
    a.token = a.token or os.environ.get("BENCHD_TOKEN", "")
    if not a.server or not a.token:
        print("need --server and a token (--token, --token-file or $BENCHD_TOKEN)")
        return 2
    root = os.path.abspath(a.dir)

    if a.reset and not a.dry_run:
        # page through everything: deleting one page shifted the next into view,
        # so the old single-page reset silently left runs behind. Always delete
        # offset 0 and stop when a page brings nothing new - which also
        # terminates if a delete keeps failing.
        deleted, seen = 0, set()
        while True:
            st, r = post(a.server, a.token, "/api/v1/runs?limit=1000", method="GET")
            if st != 200:
                print("reset: list failed (%s)" % st)
                break
            ids = [x["id"] for x in (r or {}).get("runs", [])
                   if isinstance(x, dict) and x.get("id") not in seen]
            if not ids:
                break
            seen.update(ids)
            for rid in ids:
                post(a.server, a.token, "/api/v1/runs/%s" % rid, method="DELETE")
            deleted += len(ids)
        print("reset: deleted %d existing runs" % deleted)

    # ids already on the server: a re-import skips these instead of duplicating
    existing = set()
    if not a.dry_run:
        off, total = 0, None
        while total is None or off < total:
            _, r = post(a.server, a.token,
                        "/api/v1/runs?limit=1000&offset=%d" % off, method="GET")
            rows = r.get("runs") or []
            existing |= {x.get("id") for x in rows}
            total = r.get("total", 0) or 0
            if not rows:
                break
            off += len(rows)

    wins = list(windows_units(root))
    win_starts = [w[3] for w in wins] + [float("inf")]
    made = 0
    for i, (kind, man, host, started, m) in enumerate(wins):
        window_end = min(win_starts[i + 1] - 1, started + 8 * 3600)
        phases, tele, arts, summary, ended, system = build_windows(root, man, host, started, m, window_end)
        label = "Windows battery"
        print("win  %-32s phases=%-3d tele=%-5d dur=%-6s %s" %
              (os.path.basename(man), len(phases), len(tele), _dur(ended - started),
               system.get("cpu") or ""))
        if a.dry_run:
            continue
        run_id = import_id("win", host, os.path.basename(man), int(started))
        st, r = post(a.server, a.token, "/api/v1/runs",
                     {"id": run_id, "host": host, "label": label,
                      "profile": {"imported": True},
                      "started": started, "platform": system.get("platform"),
                      "gpu": system.get("gpu"), "driver": system.get("driver"),
                      "system": system})
        if r.get("existing"):
            print("  skip (already imported)")
            continue
        if not r.get("run_id"):
            print("  FAIL create:", r); continue
        fail = upload_run(a.server, a.token, run_id, phases, tele, arts, summary,
                          ended=ended, system=system)
        if fail:
            print("  partial upload (%s)" % ", ".join(fail[:4]))
            continue
        made += 1

    for d, rep, j in linux_units(root):
        host, started, phases, tele, arts, summary, ended, system = build_linux(d, rep, j)
        if not host:
            print("linux %-32s skipped (no host in report)" % os.path.basename(d))
            continue
        print("linux %-32s phases=%-3d tele=%-5d dur=%-6s %s" %
              (os.path.basename(d), len(phases), len(tele), _dur(ended - started),
               system.get("cpu") or ""))
        if a.dry_run:
            continue
        run_id = import_id("lin", host, os.path.basename(d), int(started))
        st, r = post(a.server, a.token, "/api/v1/runs",
                     {"id": run_id, "host": host, "label": "Linux battery",
                      "profile": {"imported": True},
                      "started": started, "platform": system.get("platform"),
                      "gpu": summary.get("gpu") or system.get("gpu"),
                      "driver": summary.get("driver") or system.get("driver"),
                      "torch": summary.get("torch"), "system": system})
        if r.get("existing"):
            print("  skip (already imported)")
            continue
        if not r.get("run_id"):
            print("  FAIL create:", r); continue
        fail = upload_run(a.server, a.token, run_id, phases, tele, arts, summary,
                          ended=ended, system=system)
        if fail:
            print("  partial upload (%s)" % ", ".join(fail[:4]))
            continue
        made += 1

    print("imported %d runs%s" % (made, " (dry run)" if a.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
