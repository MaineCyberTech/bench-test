"""Wave W18 control-plane P3 tests.

Covers: invalid client input returns a generic 4xx (never a 500, never a leaked
exception type/traceback) - API-P3-001 / DATA-P3-001; a late /finish cannot
overwrite a reaped run - DATA-P3-002; the at-most-once command queue re-queues a
fetched-but-unacked command once and then expires it - RES-P3-001; artifact
sha256 is recomputed and verified - FILE-P3-001; the hot-path indexes exist -
SEARCH-P3-002; static assets carry a cache validator - PERF-P3-001.

Stdlib only, reuse the shared harness.
"""
import base64
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request

from harness import Checker, Server, TOKEN


def _raw(base, method, path, data=None, headers=None, token=None):
    """A request with raw bytes/headers -> (status, body_bytes, response headers)."""
    req = urllib.request.Request(base + path, data=data, method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def _metric(text, name, labels=None):
    """Value of a Prometheus text metric line, or None."""
    want = name + ("{%s}" % labels if labels else "")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.rsplit(" ", 1)
        if len(parts) == 2 and parts[0] == want:
            try:
                return float(parts[1])
            except ValueError:
                return None
    return None


def _leaks(text):
    """True when a response body exposes an internal type or a traceback."""
    low = text.lower()
    return ("traceback" in low or "typeerror" in low or "attributeerror" in low
            or "keyerror" in low or "valueerror" in low or 'file "' in low)


def run():
    c = Checker("server_p3")
    srv = Server()
    cl = srv.client()
    B = srv.benchd
    S = B.Handler.store

    # ---- register a runner + run to write against ----
    st, reg = cl.call("POST", "/api/v1/register", {"name": "p3", "kind": "agent"})
    rid, key = reg.get("runner_id"), reg.get("api_key")
    c.check("registered a runner", st == 200 and rid and key, reg)
    st, rr = cl.call("POST", "/api/v1/runs", {"runner_id": rid, "host": "p3"}, token=key)
    run_id = rr.get("run_id")
    c.check("opened a run", st == 200 and run_id, rr)

    # ---- API-P3-001 / DATA-P3-001: bad input is a 4xx, leaking nothing ----
    bads = [
        ("/api/v1/runs/%s/telemetry" % run_id, {"samples": 5}),
        ("/api/v1/runs/%s/telemetry" % run_id, {"samples": [1, 2]}),
        ("/api/v1/runs/%s/log" % run_id, {"lines": 5}),
        ("/api/v1/runs/%s/phase" % run_id, {"seq": 1, "group": {"x": 1}}),
        ("/api/v1/runs/%s/event" % run_id, {"level": {"x": 1}}),
        ("/api/v1/runs/%s/artifact" % run_id, {"content": {"x": 1}}),
        ("/api/v1/runners/%s/commands" % rid, {"kind": {"x": 1}}),
        ("/api/v1/runs", {"runner_id": rid, "profile": [1, 2]}),
        ("/api/v1/schedules", {"runner_id": rid, "at_time": 5}),
        ("/api/v1/register", {"machine_id": 123}),
    ]
    for path, body in bads:
        st, raw, _h = _raw(srv.base, "POST", path, data=json.dumps(body).encode(),
                           headers={"Authorization": "Bearer " + TOKEN,
                                    "Content-Type": "application/json"})
        txt = raw.decode("utf-8", "replace")
        c.check("bad input to %s -> 4xx" % path, 400 <= st < 500, (st, txt[:80]))
        c.check("bad input to %s leaks no internals" % path, not _leaks(txt), txt[:120])

    st, raw, _h = _raw(srv.base, "POST", "/api/v1/register", data=b"{not json",
                       headers={"Authorization": "Bearer " + TOKEN,
                                "Content-Type": "application/json"})
    c.check("a malformed JSON body -> 400", st == 400, st)
    c.check("a malformed JSON body leaks no internals", not _leaks(raw.decode()), raw[:80])

    # ---- DATA-P3-002: a late finish cannot overwrite a reaped run ----
    st, rr2 = cl.call("POST", "/api/v1/runs", {"runner_id": rid, "host": "p3"}, token=key)
    run2 = rr2["run_id"]
    S.ex("UPDATE runs SET status='failed' WHERE id=?", (run2,))
    st, body = cl.call("POST", "/api/v1/runs/%s/finish" % run2,
                       {"status": "done", "summary": {"gpu_soak_tflops": 1.0}}, token=key)
    c.check("a late finish on a reaped run -> 409", st == 409, (st, body))
    c.check("the reaped status is preserved",
            S.one("SELECT status FROM runs WHERE id=?", (run2,))["status"] == "failed", "")
    c.check("no late summary is written to the reaped run",
            not S.one("SELECT summary FROM runs WHERE id=?", (run2,))["summary"], "")

    # ---- RES-P3-001: re-queue once, then expire ----
    st, cmd = cl.call("POST", "/api/v1/runners/%s/commands" % rid, {"kind": "stop"})
    cid = cmd.get("id")
    c.check("issued a command", st == 200 and cid, cmd)
    st, polled = cl.call("GET", "/api/v1/runners/%s/commands" % rid, token=key)
    c.check("the command is fetched (queue drained)",
            len(polled["commands"]) == 1 and polled["commands"][0]["id"] == cid, polled)
    S.ex("UPDATE commands SET sent=? WHERE id=?", (time.time() - 400, cid))
    B.reap_stale_commands(S, B.Handler.bus)
    row = S.one("SELECT status, attempts FROM commands WHERE id=?", (cid,))
    c.check("an unacked command is re-queued once",
            row["status"] == "queued" and row["attempts"] == 1, row)
    st, again = cl.call("GET", "/api/v1/runners/%s/commands" % rid, token=key)
    c.check("the re-queued command is delivered again",
            any(x["id"] == cid for x in again["commands"]), again)
    S.ex("UPDATE commands SET sent=? WHERE id=?", (time.time() - 400, cid))
    B.reap_stale_commands(S, B.Handler.bus)
    c.check("a second timeout expires the command",
            S.one("SELECT status FROM commands WHERE id=?", (cid,))["status"] == "expired", "")
    st, raw, _h = _raw(srv.base, "GET", "/metrics")
    text = raw.decode()
    c.check("metrics surfaces the expired-command count",
            _metric(text, "benchd_commands_expired_total") >= 1,
            _metric(text, "benchd_commands_expired_total"))
    c.check("metrics surfaces the queued-command depth",
            _metric(text, "benchd_command_queue_depth") is not None,
            _metric(text, "benchd_command_queue_depth"))

    # ---- FILE-P3-001: the artifact sha256 is recomputed and verified ----
    blob = b"hello benchd"
    b64 = base64.b64encode(blob).decode()
    good = hashlib.sha256(blob).hexdigest()
    st, body = cl.call("POST", "/api/v1/runs/%s/artifact" % run_id,
                       {"name": "good.bin", "kind": "bin", "content": b64,
                        "sha256": "0" * 64}, token=key)
    c.check("an artifact with a wrong sha256 is refused with 400", st == 400, (st, body))
    st, body = cl.call("POST", "/api/v1/runs/%s/artifact" % run_id,
                       {"name": "good.bin", "kind": "bin", "content": b64,
                        "sha256": good}, token=key)
    c.check("an artifact with a matching sha256 is accepted", st == 200, (st, body))
    c.check("the upload response serves the recomputed digest",
            body.get("sha256") == good, body)
    stored = S.one("SELECT sha256 FROM artifacts WHERE run_id=? AND name='good.bin'",
                   (run_id,))
    c.check("the stored sha256 is the recomputed one",
            bool(stored) and stored["sha256"] == good, stored)
    st, dl = cl.call("GET", "/api/v1/runs/%s/artifact/good.bin?token=%s" % (run_id, TOKEN),
                     raw=True)
    c.check("the verified artifact downloads intact", st == 200 and dl == blob, (st, len(dl)))
    st, body = cl.call("POST", "/api/v1/runs/%s/artifact" % run_id,
                       {"name": "nohash.bin", "content": b64}, token=key)
    c.check("an artifact without a claimed hash is accepted and recomputed",
            st == 200 and body.get("sha256") == good, body)

    # ---- SEARCH-P3-002: the hot-path indexes exist ----
    names = {r["name"] for r in S.q("SELECT name FROM sqlite_master WHERE type='index'")}
    for idx in ("idx_runs_status_started", "idx_tele_run", "idx_logs_run_ts",
                "idx_events_run"):
        c.check("index %s exists" % idx, idx in names, sorted(names))
    for idx, want in (("idx_runs_status_started", ["status", "started"]),
                      ("idx_tele_run", ["run_id", "ts"]),
                      ("idx_logs_run_ts", ["run_id", "ts"]),
                      ("idx_events_run", ["run_id", "ts"])):
        cols = [r["name"] for r in S.q("PRAGMA index_info(%s)" % idx)]
        c.check("%s covers %s" % (idx, "+".join(want)), cols == want, cols)

    # ---- PERF-P3-001: static assets carry a validator, the API stays no-store ----
    st, raw, h = _raw(srv.base, "GET", "/index.html")
    c.check("a static asset is served", st == 200 and len(raw) > 0, st)
    c.check("a static asset carries a cache validator",
            bool(h.get("ETag")) or bool(h.get("Last-Modified")),
            (h.get("ETag"), h.get("Last-Modified")))
    c.check("a static asset is not no-store",
            "no-store" not in (h.get("Cache-Control") or ""), h.get("Cache-Control"))
    etag = h.get("ETag")
    if etag:
        st2, _b, h2 = _raw(srv.base, "GET", "/index.html",
                           headers={"If-None-Match": etag})
        c.check("a matching If-None-Match revalidates as 304", st2 == 304, st2)
    st, _b, h = _raw(srv.base, "GET", "/api/v1/state", token=TOKEN)
    c.check("API responses stay no-store",
            h.get("Cache-Control") == "no-store", h.get("Cache-Control"))

    srv.stop()
    return c.fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
