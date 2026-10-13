"""Wave W16 control-plane performance tests.

Covers: the non-blocking, TTL-cached GitHub version compare (PERF-P2-004) and the
bounded / coalesced realtime fan-out (PERF-P2-002). Stdlib only, reuse the shared
harness.
"""
import io
import sys
import time
import urllib.error
import urllib.request
from contextlib import redirect_stderr

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
        if len(parts) != 2 or parts[0] != want:
            continue
        try:
            return float(parts[1])
        except ValueError:
            return None
    return None


def _wait_for(pred, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(0.05)
    return None


def run():
    c = Checker("perf")
    srv = Server()
    B = srv.benchd

    orig_compare = B._compare_fetch
    orig_latest = B._latest_fetch
    ref_backup = dict(B._REF)

    # ---- PERF-P2-004a: the compare is non-blocking and cached ----
    base, head = "perfbase01", "perfhead01"
    calls = {"n": 0}

    def slow_compare(_b, _h):
        calls["n"] += 1
        time.sleep(2.0)
        return "ahead", ["bench/gui.py"]

    B._compare_fetch = slow_compare
    try:
        t0 = time.time()
        v = B.compare_detail(base, head)
        elapsed = time.time() - t0
        c.check("a cold compare answers 'unknown' without waiting on GitHub",
                v == ("", []) and elapsed < 0.5, (v, round(elapsed, 3)))

        cached = _wait_for(lambda: B._CMP.get((base, head)))
        c.check("a background daemon refreshes the compare cache",
                bool(cached) and cached["v"] == ("ahead", ["bench/gui.py"]), cached)

        t0 = time.time()
        v2 = B.compare_detail(base, head)
        elapsed2 = time.time() - t0
        c.check("a warm compare is served from cache immediately",
                v2 == ("ahead", ["bench/gui.py"]) and elapsed2 < 0.2,
                (v2, round(elapsed2, 3)))
        c.check("the cached compare does not re-invoke GitHub", calls["n"] == 1, calls["n"])
    finally:
        B._compare_fetch = orig_compare
        with B._CMP_LOCK:
            B._CMP.pop((base, head), None)
            B._CMP_PENDING.discard((base, head))

    # ---- PERF-P2-004b: a heartbeat is prompt while the compare call is slow ----
    st, reg = srv.client().call("POST", "/api/v1/register",
                                {"name": "perf-hb", "kind": "agent",
                                 "capabilities": {"toolkit": {"mode": "git",
                                                              "commit": "perfhb001"}}})
    rid, key = reg.get("runner_id"), reg.get("api_key")
    c.check("registered a runner for the heartbeat check", st == 200 and rid and key, reg)

    def slow_latest():
        time.sleep(4.0)
        return "deadbeef"

    def slower_compare(_b, _h):
        time.sleep(4.0)
        return "behind", []

    B._latest_fetch = slow_latest
    B._compare_fetch = slower_compare
    with B._CMP_LOCK:
        B._REF["t"] = 0.0  # make the cached tip stale so a refresh is queued
    try:
        t0 = time.time()
        st, body = srv.client().call("POST", "/api/v1/runners/%s/heartbeat" % rid,
                                     {"status": "idle"}, token=key)
        elapsed = time.time() - t0
        c.check("the heartbeat still succeeds while the compare call is slow",
                st == 200 and body.get("ok") is True, (st, body))
        c.check("the heartbeat is not blocked by the slow GitHub compare",
                elapsed < 1.5, round(elapsed, 3))
    finally:
        B._latest_fetch = orig_latest
        B._compare_fetch = orig_compare
        with B._CMP_LOCK:
            B._REF.clear()
            B._REF.update(ref_backup)

    # ---- PERF-P2-002a: bounded SSE fan-out ----
    cap = 2
    B.Handler.bus = B.Bus(max_subscribers=cap)
    opened = []
    try:
        with redirect_stderr(io.StringIO()):
            for _ in range(cap):
                r = urllib.request.urlopen(
                    urllib.request.Request(srv.base + "/api/v1/stream?token=" + TOKEN),
                    timeout=5)
                first = r.readline()
                opened.append((r, first))
            c.check("the capped SSE subscribers connect",
                    all(x[1].startswith(b"event: snapshot") for x in opened),
                    [x[1][:40] for x in opened])
            try:
                urllib.request.urlopen(
                    urllib.request.Request(srv.base + "/api/v1/stream?token=" + TOKEN),
                    timeout=5)
                code = 200
            except urllib.error.HTTPError as e:
                code = e.code
            c.check("the (N+1)th SSE subscriber is rejected with 503", code == 503, code)
    finally:
        for r, _first in opened:
            try:
                r.close()
            except Exception:
                pass
        B.Handler.bus = B.Bus()

    # ---- PERF-P2-002b: telemetry coalescing + dropped counter ----
    bus = B.Handler.bus
    sub = bus.subscribe()
    for i in range(3):
        bus.publish({"event": "telemetry",
                     "data": {"run_id": "perf-run", "sample": {"elapsed": i},
                              "samples": [{"elapsed": i}], "n": 1}})
    c.check("telemetry coalesces to one pending batch per subscriber",
            sub.pending() == 1, sub.pending())
    ev = sub.get(1)
    c.check("the delivered batch is the coalesced merge",
            ev.get("event") == "telemetry" and len(ev["data"]["samples"]) == 3
            and ev["data"]["n"] == 3, ev)
    c.check("merged telemetry batches are counted as drops",
            bus.dropped_count() == 2, bus.dropped_count())
    c.check("a drained subscriber has nothing pending", sub.pending() == 0, sub.pending())
    bus.unsubscribe(sub)

    st, body, _h = _raw(srv.base, "GET", "/metrics")
    text = body.decode()
    c.check("metrics exposes the SSE dropped-events counter",
            _metric(text, "benchd_sse_dropped_events_total") >= 2,
            _metric(text, "benchd_sse_dropped_events_total"))
    c.check("metrics exposes the active subscriber gauge",
            _metric(text, "benchd_sse_subscribers") is not None,
            _metric(text, "benchd_sse_subscribers"))

    srv.stop()
    return c.fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
