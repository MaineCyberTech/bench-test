"""Wave W12 observability tests.

Covers: structured JSON logging + per-request ids (OBS-P2-002), error logging
with a traceback that never reaches the client (OBS-P2-003), the /health vs
/ready split (OBS-P2-004) and the stdlib /metrics endpoint (OBS-P2-005).
Stdlib only, reuse the shared harness.
"""
import io
import json
import re
import sys
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


def run():
    c = Checker("observability")
    srv = Server()
    B = srv.benchd

    # ---- OBS-P2-002: per-request id, echoed and honoured ----
    st, body, h = _raw(srv.base, "GET", "/health")
    minted = h.get("X-Request-ID")
    c.check("every response carries an X-Request-ID", st == 200 and bool(minted),
            (st, minted))

    st, _body, h = _raw(srv.base, "GET", "/health",
                        headers={"X-Request-ID": "trace-abc-123"})
    c.check("a supplied X-Request-ID is honoured",
            h.get("X-Request-ID") == "trace-abc-123", h.get("X-Request-ID"))

    st, _body, h2 = _raw(srv.base, "GET", "/health")
    c.check("a minted id is unique per request",
            h2.get("X-Request-ID") and h2.get("X-Request-ID") != minted,
            (minted, h2.get("X-Request-ID")))

    # ---- OBS-P2-004: /health stays cheap, /ready checks the DB ----
    st, hb, _h = _raw(srv.base, "GET", "/health")
    health = json.loads(hb.decode())
    c.check("health keeps its liveness fields",
            st == 200 and health.get("ok") and "version" in health
            and "protocol" in health and isinstance(health.get("capacity"), dict),
            health)

    st, rb, _h = _raw(srv.base, "GET", "/ready")
    ready = json.loads(rb.decode())
    c.check("ready reports the DB",
            st == 200 and ready.get("ok") is True
            and isinstance(ready.get("db"), dict) and ready["db"].get("ok") is True,
            (st, ready))
    c.check("ready includes the version/git compare",
            "version" in ready and "latest" in ready and "compare" in ready, ready)

    # /health must not touch the DB: point the handler at a broken store and the
    # liveness probe still answers 200, while /ready fails closed (503).
    class _BrokenStore:
        def one(self, *a, **k):
            raise RuntimeError("db down")

        def q(self, *a, **k):
            raise RuntimeError("db down")

    prev_store = B.Handler.store
    B.Handler.store = _BrokenStore()
    try:
        st, _b, _h = _raw(srv.base, "GET", "/health")
        c.check("health never touches the DB (still 200 with a dead store)",
                st == 200, st)
        # the readiness failure is logged; capture it so the suite output stays clean
        with redirect_stderr(io.StringIO()):
            st, _b, _h = _raw(srv.base, "GET", "/ready")
        c.check("ready fails closed when the DB is down", st == 503, st)
    finally:
        B.Handler.store = prev_store

    # ---- OBS-P2-003: a handler error is logged, not swallowed ----
    orig = B.Handler._api_get

    def _boom(self, path, q):
        raise RuntimeError("boom")

    B.Handler._api_get = _boom
    buf = io.StringIO()
    try:
        with redirect_stderr(buf):
            st, body, h = _raw(srv.base, "GET", "/api/v1/state",
                               headers={"X-Request-ID": "err-42"}, token=TOKEN)
    finally:
        B.Handler._api_get = orig
    c.check("a handler error still returns its normal 500", st == 500, st)
    c.check("the error response still carries the request id",
            h.get("X-Request-ID") == "err-42", h.get("X-Request-ID"))
    logged = None
    for line in buf.getvalue().splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("request_id") == "err-42":
            logged = rec
    c.check("the handler error is logged as JSON with the request id",
            logged is not None, buf.getvalue()[-300:])
    c.check("the error log has level=error",
            bool(logged) and logged.get("level") == "error", logged)
    tb = ((logged or {}).get("extras") or {}).get("traceback", "")
    c.check("the error log carries a traceback",
            "Traceback" in tb and "boom" in tb, tb[-200:])
    c.check("the traceback is never sent to the client",
            b"Traceback" not in body and b'File "' not in body, body[:120])
    srv.stop()

    # ---- OBS-P2-005: /metrics exposes runtime counters ----
    msrv = Server()
    mcl = msrv.client()
    st, reg = mcl.call("POST", "/api/v1/register", {"name": "obs", "kind": "agent"})
    mrid = reg.get("runner_id")
    c.check("registered a runner for metrics", st == 200 and mrid, reg)
    st, cmd = mcl.call("POST", "/api/v1/runners/%s/commands" % mrid, {"kind": "stop"})
    c.check("queued a command", st == 200 and cmd.get("id"), cmd)
    codes = [mcl.call("GET", "/api/v1/state", token="wrong")[0] for _ in range(20)]
    c.check("auth failures reach 401 then 429",
            401 in codes and 429 in codes, codes[-3:])

    st, body, h = _raw(msrv.base, "GET", "/metrics")
    text = body.decode()
    c.check("metrics is served as Prometheus text",
            st == 200 and "text/plain" in (h.get("Content-Type") or ""),
            (st, h.get("Content-Type")))
    c.check("requests by status class are counted",
            _metric(text, "benchd_requests_total", 'class="4xx"') >= 1
            and _metric(text, "benchd_requests_total", 'class="2xx"') >= 1,
            text[:200])
    c.check("auth failures are counted",
            _metric(text, "benchd_auth_failures_total") >= 1,
            _metric(text, "benchd_auth_failures_total"))
    c.check("rate-limit rejections are counted",
            _metric(text, "benchd_rate_limit_rejections_total") >= 1,
            _metric(text, "benchd_rate_limit_rejections_total"))
    c.check("audit rows are exposed",
            _metric(text, "benchd_audit_rows") >= 1,
            _metric(text, "benchd_audit_rows"))
    c.check("command queue depth is exposed",
            _metric(text, "benchd_command_queue_depth") >= 1,
            _metric(text, "benchd_command_queue_depth"))
    uptime = _metric(text, "benchd_uptime_seconds")
    c.check("uptime is exposed", uptime is not None and uptime >= 0, uptime)
    msrv.stop()

    return c.fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
