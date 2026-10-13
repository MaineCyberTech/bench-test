"""Wave W23a alerting + telemetry-cache tests.

Covers the optional, disabled-by-default alert webhook (OBS-P2-001 / IR-P2-001 /
NOTIF-P2-001): it fires on a failed run finish and on a readiness failure, is
rate-limited per kind, counts in /metrics, and a dead webhook never slows or
alters the request. Also covers the per-run stats/CSV cache (PERF-P2-001):
identical responses on repeat requests, refreshed after new telemetry.

Stdlib only, reuses the shared harness.
"""
import io
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from harness import Checker, Server, TOKEN


class _AlertStub:
    """A tiny HTTP server that records the JSON alert POSTs it receives."""

    def __init__(self):
        self.reqs = []
        self.lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = 0
                raw = self.rfile.read(n) if n > 0 else b""
                try:
                    body = json.loads(raw.decode() or "{}")
                except Exception:
                    body = {}
                with outer.lock:
                    outer.reqs.append((self.path, body))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._t.start()
        self.url = "http://127.0.0.1:%d/alert" % self.httpd.server_address[1]

    def stop(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        finally:
            self._t.join(timeout=5)

    def alerts(self, kind=None):
        with self.lock:
            return [b for (_p, b) in self.reqs
                    if kind is None or b.get("alert") == kind]


def _free_port():
    """A TCP port that is (almost certainly) closed, for a dead-webhook test."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _raw(base, method, path, token=None):
    """A request -> (status, body_bytes)."""
    req = urllib.request.Request(base + path, method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _metric(text, name):
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.rsplit(" ", 1)
        if len(parts) == 2 and parts[0] == name:
            try:
                return float(parts[1])
            except ValueError:
                return None
    return None


def _wait_for(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(0.02)
    return None


def run():
    c = Checker("alerts")
    stub = _AlertStub()
    srv = Server()
    B = srv.benchd
    # point this instance's webhook at the stub
    B.Handler.store.alerter = B.Alerter(stub.url)
    cl = srv.client()

    st, reg = cl.call("POST", "/api/v1/register", {"name": "alerts", "kind": "agent"})
    rid = reg.get("runner_id")
    c.check("registered a runner", st == 200 and rid, reg)

    # ---- OBS-P2-001: a failed finish alerts ----
    st, run = cl.call("POST", "/api/v1/runs", {"runner_id": rid, "label": "boom"})
    run_id = run.get("run_id")
    c.check("created a run", st == 200 and run_id, run)

    st, body = cl.call("POST", "/api/v1/runs/%s/finish" % run_id,
                       {"status": "failed", "exit_code": 1})
    c.check("a failed finish is accepted", st == 200 and body.get("ok"), (st, body))
    got = _wait_for(lambda: stub.alerts("run_failed"))
    c.check("the webhook fires on a failed finish", bool(got), stub.reqs)
    c.check("the failed-finish alert carries the run id",
            bool(got) and got[0].get("run_id") == run_id, got[:1])

    # a clean finish must not alert
    st, run2 = cl.call("POST", "/api/v1/runs", {"runner_id": rid, "label": "ok"})
    cl.call("POST", "/api/v1/runs/%s/finish" % run2["run_id"], {"status": "done"})
    time.sleep(0.3)
    c.check("a clean finish does not alert", len(stub.alerts("run_failed")) == 1,
            stub.alerts("run_failed"))

    # ---- rate limit: a second alert of the same kind is suppressed ----
    st, run3 = cl.call("POST", "/api/v1/runs", {"runner_id": rid, "label": "boom2"})
    cl.call("POST", "/api/v1/runs/%s/finish" % run3["run_id"], {"status": "failed"})
    time.sleep(0.3)
    c.check("a second alert of the same kind is rate-limited",
            len(stub.alerts("run_failed")) == 1, stub.alerts("run_failed"))

    st, raw = _raw(srv.base, "GET", "/metrics")
    text = raw.decode()
    c.check("metrics counts delivered alerts",
            _metric(text, "benchd_alerts_fired_total") >= 1,
            _metric(text, "benchd_alerts_fired_total"))
    c.check("metrics counts rate-limited alerts",
            _metric(text, "benchd_alerts_suppressed_total") >= 1,
            _metric(text, "benchd_alerts_suppressed_total"))

    # ---- OBS-P2-001: a failing /ready alerts ----
    alerter = B.Handler.store.alerter

    class _BrokenStore:
        def __init__(self, a):
            self.alerter = a
            self.lock = threading.Lock()

            class _C:
                def execute(self, *a, **k):
                    raise RuntimeError("db down")

                def commit(self):
                    pass

            self.conn = _C()

        def one(self, *a, **k):
            raise RuntimeError("db down")

        def q(self, *a, **k):
            raise RuntimeError("db down")

    prev = B.Handler.store
    B.Handler.store = _BrokenStore(alerter)
    try:
        with redirect_stderr(io.StringIO()):
            st, _b = _raw(srv.base, "GET", "/ready")
        c.check("ready fails closed when the DB is down", st == 503, st)
    finally:
        B.Handler.store = prev
    got = _wait_for(lambda: stub.alerts("readiness"))
    c.check("the webhook fires on a readiness failure", bool(got), stub.reqs)

    # ---- OBS-P2-001: a burst of auth failures alerts ----
    for _ in range(12):
        _raw(srv.base, "GET", "/api/v1/state", token="wrong")
    got = _wait_for(lambda: stub.alerts("auth_failures"))
    c.check("the webhook fires on a burst of auth failures", bool(got), stub.reqs)

    # ---- OBS-P2-001: a capacity threshold alerts ----
    store = B.Handler.store
    store.alerter.db_bytes_limit = 1  # any non-empty db exceeds this
    with redirect_stderr(io.StringIO()):
        B.refresh_health(store, with_network=False)
    got = _wait_for(lambda: stub.alerts("capacity"))
    c.check("the webhook fires on a capacity threshold", bool(got), stub.reqs)
    c.check("the capacity alert carries the db size and the limit",
            bool(got) and got[0].get("db_bytes", 0) > 0 and got[0].get("limit") == 1,
            got[:1])
    srv.stop()
    stub.stop()

    # ---- OBS-P2-001: a dead webhook never slows or alters the request ----
    srv2 = Server()
    B2 = srv2.benchd
    B2.Handler.store.alerter = B2.Alerter(
        "http://127.0.0.1:%d/alert" % _free_port(), timeout=1.0)
    cl2 = srv2.client()
    st, reg = cl2.call("POST", "/api/v1/register", {"name": "dead", "kind": "agent"})
    rid2 = reg["runner_id"]
    st, run = cl2.call("POST", "/api/v1/runs", {"runner_id": rid2, "label": "x"})
    rid_run = run["run_id"]
    t0 = time.time()
    with redirect_stderr(io.StringIO()):
        st, body = cl2.call("POST", "/api/v1/runs/%s/finish" % rid_run, {"status": "failed"})
        elapsed = time.time() - t0
        _wait_for(lambda: B2.Handler.store.alerter.totals()["failed"] >= 1, timeout=3.0)
    c.check("a dead webhook does not slow the finish request", elapsed < 1.0,
            round(elapsed, 3))
    c.check("a dead webhook does not alter the finish response",
            st == 200 and body.get("ok") is True, (st, body))
    c.check("the failed webhook POST is counted, not raised",
            B2.Handler.store.alerter.totals()["failed"] >= 1,
            B2.Handler.store.alerter.totals())
    srv2.stop()

    # ---- PERF-P2-001: the per-run stats/CSV cache ----
    srv3 = Server()
    B3 = srv3.benchd
    B3.clear_stats_cache()
    c.check("alerting is disabled by default",
            B3.Handler.store.alerter.enabled is False, B3.Handler.store.alerter.url)
    cl3 = srv3.client()
    st, reg = cl3.call("POST", "/api/v1/register", {"name": "cache", "kind": "agent"})
    rid3 = reg["runner_id"]
    st, run = cl3.call("POST", "/api/v1/runs", {"runner_id": rid3, "label": "cache"})
    cid = run["run_id"]
    samples = [{"ts": 1000 + i, "elapsed": i, "temp_c": 40 + i, "power_w": 100 + i}
               for i in range(5)]
    st, _b = cl3.call("POST", "/api/v1/runs/%s/telemetry" % cid, {"samples": samples})
    c.check("posted telemetry", st == 200, st)

    calls = {"n": 0}
    orig_cs = B3.compute_stats

    def _count(*a, **k):
        calls["n"] += 1
        return orig_cs(*a, **k)

    B3.compute_stats = _count
    try:
        s1 = cl3.call("GET", "/api/v1/runs/%s/stats" % cid, raw=True)
        s2 = cl3.call("GET", "/api/v1/runs/%s/stats" % cid, raw=True)
        c.check("the stats endpoint returns identical JSON twice", s1 == s2, s1[0])
        c.check("the stats cache re-parses the rows only once", calls["n"] == 1, calls["n"])
    finally:
        B3.compute_stats = orig_cs

    c1 = cl3.call("GET", "/api/v1/runs/%s/telemetry.csv" % cid, raw=True)
    c2 = cl3.call("GET", "/api/v1/runs/%s/telemetry.csv" % cid, raw=True)
    c.check("the CSV endpoint returns identical bytes twice", c1 == c2, c1[0])

    t1 = cl3.call("GET", "/api/v1/runs/%s/telemetry" % cid, raw=True)
    t2 = cl3.call("GET", "/api/v1/runs/%s/telemetry" % cid, raw=True)
    c.check("the telemetry endpoint returns identical JSON twice", t1 == t2, t1[0])

    # new telemetry must invalidate and refresh every cached artifact
    st, _b = cl3.call("POST", "/api/v1/runs/%s/telemetry" % cid,
                      {"samples": [{"ts": 2000, "elapsed": 99, "temp_c": 88, "power_w": 300}]})
    c.check("posted more telemetry", st == 200, st)
    s3 = cl3.call("GET", "/api/v1/runs/%s/stats" % cid, raw=True)
    c.check("the stats cache refreshes after new rows", s3 != s1, (s1[1][:80], s3[1][:80]))
    c3 = cl3.call("GET", "/api/v1/runs/%s/telemetry.csv" % cid, raw=True)
    c.check("the CSV cache refreshes after new rows", c3 != c1, "csv changed")
    t3 = cl3.call("GET", "/api/v1/runs/%s/telemetry" % cid, raw=True)
    c.check("the telemetry cache refreshes after new rows", t3 != t1, "telemetry changed")

    B3.clear_stats_cache()
    srv3.stop()
    return c.fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
