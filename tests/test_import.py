"""Backfill importer tests (server/import_results.py).

Stdlib only. Covers the pieces that had no coverage at all: timestamp parsing,
telemetry CSV parsing, and the summary/verdict builders — which must match the
live-run path (`benchreport._merge_into` / `_hardware_verdicts`).
"""
import json
import os
import re
import sys
import tempfile

from harness import Checker, tmpdir
import import_results as imp


def run():
    c = Checker("import")

    # ---- epoch: 7-digit PowerShell fractions, trailing Z, numeric ----
    e7 = imp.epoch("2026-10-11T23:01:30.3477639-04:00")
    c.check("epoch parses 7-digit fraction", e7 is not None and e7 > 1.7e9, e7)
    z = imp.epoch("2026-10-11T23:01:30Z")
    c.check("epoch parses trailing Z", z is not None and z > 1.7e9, z)
    c.check("epoch accepts a numeric epoch", imp.epoch(1791779608.5) == 1791779608.5, "")
    c.check("epoch rejects junk", imp.epoch("not-a-date") is None, "")
    c.check("epoch handles empty", imp.epoch(None) is None and imp.epoch("") is None, "")

    # ---- read_telemetry: named samples, extended columns, reconstructed ts ----
    d = tmpdir()
    csv_path = os.path.join(d, "h-win-20260101-000000.telemetry.csv")
    with open(csv_path, "w", encoding="utf-8") as fh:
        fh.write("elapsed_s,gpu_util_pct,temp_c,power_w,mem_util_pct,vram_used_mib,throttle\n")
        fh.write("0,10,40,50,5,100,0\n")
        fh.write("1,99,65,250,60,9000,1\n")
    rows = imp.read_telemetry(csv_path, t0=1000.0)
    c.check("read_telemetry rows", len(rows) == 2, rows)
    c.check("read_telemetry maps core fields",
            rows[1]["gpu_util"] == 99 and rows[1]["temp_c"] == 65, rows[1])
    c.check("read_telemetry keeps extended columns",
            rows[1]["vram_used_mib"] == 9000 and rows[1]["throttle"] == 1, rows[1])
    c.check("read_telemetry reconstructs ts from start+elapsed",
            abs(rows[1]["ts"] - 1001.0) < 1e-6, rows[1].get("ts"))
    c.check("read_telemetry downsamples over cap",
            len(imp.read_telemetry(csv_path, cap=1)) == 1, "")

    # nan/inf from a CSV must not pass through: the server stores them and would
    # then emit invalid JSON (the dashboard's JSON.parse throws)
    csv2 = os.path.join(d, "h-win-20260101-000001.telemetry.csv")
    with open(csv2, "w", encoding="utf-8") as fh:
        fh.write("elapsed_s,temp_c,power_w\n")
        fh.write("0,nan,inf\n")
        fh.write("1,55,200\n")
    rows2 = imp.read_telemetry(csv2, t0=2000.0)
    c.check("read_telemetry drops non-finite CSV values",
            len(rows2) == 2 and rows2[0].get("temp_c") is None
            and rows2[0].get("power_w") is None and rows2[1]["temp_c"] == 55, rows2)

    # ---- _merge_summary shares the live-run merge (both platform shapes) ----
    s = {}
    imp._merge_summary(s, [
        {"phases": [{"group": "gpu", "phase": "pcie", "result": {"h2d_gbps": 12.0, "d2h_gbps": 13.0}},
                    {"group": "nvenc", "phase": "bench",
                     "result": {"ffmpeg": "x", "results": [{"codec": "h264_nvenc", "fps": 150}]}},
                    {"group": "disk", "phase": "latency",
                     "result": {"iops": 900, "us_p50": 0.3, "us_p99": 1.1}}]},
        {"results": [{"model": "m", "tok_per_s": 21.0}]},
        {"gpu": {"matmul_tflops": 80.0}, "cpu": {"gflops": 400.0}},
    ])
    c.check("import: pcie merged", (s.get("pcie") or {}).get("h2d_gbps") == 12.0, s.get("pcie"))
    c.check("import: windows nvenc list normalised",
            ((s.get("nvenc") or {}).get("results") or {}).get("h264_nvenc", {}).get("fps") == 150,
            s.get("nvenc"))
    c.check("import: llm merged", (s.get("llm") or {}).get("results", [{}])[0].get("tok_per_s") == 21.0,
            s.get("llm"))
    c.check("import: disk latency keys normalised (us_p99 -> p99)",
            (s.get("disk") or {}).get("latency", {}).get("p99") == 1.1, s.get("disk"))
    c.check("import: combined merged", (s.get("combined") or {}).get("gpu", {}).get("matmul_tflops") == 80.0,
            s.get("combined"))

    # Linux combined/fullsoak shapes
    s2 = {}
    imp._merge_summary(s2, [{"phases": [
        {"group": "combined", "phase": "stress", "result": {"seconds": 60}},
        {"group": "fullsoak", "phase": "soak", "result": {"verdict": {"clean": False}}},
    ]}])
    c.check("import: linux combined merged", (s2.get("combined") or {}).get("seconds") == 60,
            s2.get("combined"))
    c.check("import: linux fullsoak verdict merged",
            (s2.get("fullsoak") or {}).get("result", {}).get("verdict", {}).get("clean") is False,
            s2.get("fullsoak"))

    # ---- verdicts come from the shared builder ----
    hv = tmpdir()
    with open(os.path.join(hv, "h-20260101-000000.health.json"), "w") as fh:
        json.dump({"summary": [{"provider": "nvlddmkm", "id": "4101", "count": 2}]}, fh)
    vs = imp._hardware_verdicts(hv)
    c.check("import: verdicts from health report",
            any("TDR" in v["text"] for v in vs) and
            all(v["level"] in ("good", "info", "warn", "bad") for v in vs), vs)

    # ---- deterministic ids make a re-import idempotent ----
    c.check("import_id is deterministic",
            imp.import_id("win", "h", "m.json", 1) == imp.import_id("win", "h", "m.json", 1), "")
    c.check("import_id differs per unit",
            imp.import_id("win", "h", "a.json", 1) != imp.import_id("win", "h", "b.json", 1), "")
    c.check("import_id is a safe server id",
            bool(re.match(r"^[A-Za-z0-9_.:-]{1,64}$", imp.import_id("lin", "h", "d", 2))), "")

    # ---- the Linux telemetry CSV is scoped to the run's window ----
    _ld = tmpdir()
    _lrep = os.path.join(_ld, "lh-20260101-000000-linux-abc.json")
    _lj = {"host": "lh", "timestamp": "2026-01-01T00:00:00Z", "phases": [],
           "telemetry": {"duration_s": 60}}
    with open(_lrep, "w", encoding="utf-8") as fh:
        json.dump(_lj, fh)
    with open(os.path.join(_ld, "runs-index.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"host": "lh"}) + "\n")
    _stale = os.path.join(_ld, "aaa-stale.telemetry.csv")
    _fresh = os.path.join(_ld, "zzz-fresh.telemetry.csv")
    for _p, _temp in ((_stale, 999.0), (_fresh, 55.0)):
        with open(_p, "w", encoding="utf-8") as fh:
            fh.write("elapsed_s,temp_c\n0,%s\n" % _temp)
    _lstarted = imp.epoch(_lj["timestamp"])
    os.utime(_stale, (_lstarted - 3600, _lstarted - 3600))
    os.utime(_fresh, (_lstarted + 30, _lstarted + 30))
    _lhost, _lst, _lph, _ltele, _larts, _lsum, _lend, _lsys = imp.build_linux(_ld, _lrep, _lj)
    c.check("the Linux telemetry CSV is scoped to the run's window, not the first glob match",
            len(_ltele) == 1 and _ltele[0].get("temp_c") == 55.0, _ltele)

    # ---- main(): --reset pages through everything, a re-import is a no-op, and
    # the token can come from a file or the environment instead of argv ----
    _empty = tmpdir()
    _runs = [{"id": "r%d" % i} for i in range(2500)]
    _deleted, _created, _existing, _tokens = [], {}, [], []

    def _fake_post(server, token, path, body=None, method="POST"):
        _tokens.append(token)
        if method == "GET" and path.startswith("/api/v1/runs?"):
            return 200, {"runs": _runs[:1000], "total": len(_runs)}
        if method == "DELETE" and path.startswith("/api/v1/runs/"):
            rid = path.rsplit("/", 1)[-1]
            _deleted.append(rid)
            _runs[:] = [r for r in _runs if r["id"] != rid]
            return 200, {"ok": True}
        if method == "POST" and path == "/api/v1/runs":
            rid = (body or {}).get("id")
            if rid in _created:
                _existing.append(rid)
                return 200, {"run_id": rid, "existing": True}
            _created[rid] = body
            return 200, {"run_id": rid}
        return 200, {"ok": True}

    _res = tmpdir()
    with open(os.path.join(_res, "impbox-20260101-000000.manifest.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"host": "impbox", "timestamp": "2026-01-01T00:00:00Z",
                   "artifacts": {}}, fh)
    _real_post = imp.post
    _argv = sys.argv
    imp.post = _fake_post
    try:
        sys.argv = ["import_results.py", "--server", "http://x", "--token", "t",
                    "--dir", _empty, "--reset"]
        _rc1 = imp.main()
        c.check("--reset deletes every page, not just the first 1000",
                _rc1 == 0 and len(_deleted) == 2500 and _runs == [],
                (_rc1, len(_deleted), len(_runs)))
        sys.argv = ["import_results.py", "--server", "http://x", "--token", "t",
                    "--dir", _res]
        _rc2 = imp.main()
        _rc3 = imp.main()
        c.check("a second import of the same directory creates no duplicate",
                _rc2 == 0 and _rc3 == 0 and len(_created) == 1 and len(_existing) == 1,
                (len(_created), _existing))

        _tokf = os.path.join(tmpdir(), "token.txt")
        with open(_tokf, "w", encoding="utf-8") as fh:
            fh.write("file-token\nignored-second-line\n")
        _saved = {k: os.environ.pop(k, None) for k in ("BENCHD_TOKEN", "BENCHD_TOKEN_FILE")}
        try:
            _tokens.clear()
            sys.argv = ["import_results.py", "--server", "http://x", "--dir", _res,
                        "--token-file", _tokf]
            _rc = imp.main()
            c.check("the token can come from --token-file",
                    _rc == 0 and _tokens and all(t == "file-token" for t in _tokens),
                    _tokens[:3])
            _tokens.clear()
            os.environ["BENCHD_TOKEN"] = "env-token"
            sys.argv = ["import_results.py", "--server", "http://x", "--dir", _res]
            _rc = imp.main()
            c.check("the token can come from $BENCHD_TOKEN",
                    _rc == 0 and _tokens and all(t == "env-token" for t in _tokens),
                    _tokens[:3])
            _tokens.clear()
            os.environ.pop("BENCHD_TOKEN", None)
            os.environ["BENCHD_TOKEN_FILE"] = _tokf
            sys.argv = ["import_results.py", "--server", "http://x", "--dir", _res]
            _rc = imp.main()
            c.check("$BENCHD_TOKEN_FILE works like --token-file",
                    _rc == 0 and _tokens and all(t == "file-token" for t in _tokens),
                    _tokens[:3])
            _tokens.clear()
            os.environ.pop("BENCHD_TOKEN_FILE", None)
            sys.argv = ["import_results.py", "--server", "http://x", "--dir", _res]
            _rc = imp.main()
            c.check("without a token the importer refuses (exit 2)",
                    _rc == 2 and not _tokens, _rc)
        finally:
            for _k, _v in _saved.items():
                if _v is None:
                    os.environ.pop(_k, None)
                else:
                    os.environ[_k] = _v
    finally:
        imp.post = _real_post
        sys.argv = _argv

    return c.fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
