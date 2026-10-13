"""Wave W9 control-plane hardening tests.

Covers: auth rate limiting (SEC-P2-003), the audit trail (ACM-P2-003/005,
ADMIN-P2-001, PRIV-P2-002, IR-P2-003, CHAIN-P2-001), CORS (SEC-P2-002),
write-body validation (API-P2-001) and the runner-delete cascade
(SEARCH-P2-002 / PRIV-P2-003). Stdlib only, reuse the shared harness.
"""
import json
import sys
import urllib.error
import urllib.request

from harness import Checker, Server, TOKEN


def _raw(base, method, path, data=None, headers=None):
    """A request with raw bytes/headers -> (status, body_bytes, response headers)."""
    req = urllib.request.Request(base + path, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def run():
    c = Checker("hardening")
    srv = Server()
    cl = srv.client()
    B = srv.benchd
    S = B.Handler.store

    # ---- API-P2-001: malformed JSON body is a 400, not a silent {} ----
    st, body, _h = _raw(srv.base, "POST", "/api/v1/register", data=b"{not json",
                        headers={"Authorization": "Bearer " + TOKEN,
                                 "Content-Type": "application/json"})
    try:
        j = json.loads(body.decode())
    except Exception:
        j = {}
    c.check("malformed JSON body -> 400 invalid json",
            st == 400 and j.get("error") == "invalid json", (st, body[:80]))

    # ---- audit trail: register / command / schedule / delete ----
    st, reg = cl.call("POST", "/api/v1/register", {"name": "aud", "kind": "agent"})
    arid, akey = reg.get("runner_id"), reg.get("api_key")
    c.check("register still works", st == 200 and arid, reg)

    st, cmd = cl.call("POST", "/api/v1/runners/%s/commands" % arid,
                      {"kind": "exec", "payload": {"cmd": "id"}})
    c.check("exec command accepted", st == 200 and cmd.get("id"), cmd)

    st, sc = cl.call("POST", "/api/v1/schedules", {"runner_id": arid, "label": "aud-sched"})
    c.check("schedule created", st == 200 and sc.get("id"), sc)
    cl.call("DELETE", "/api/v1/schedules/" + sc["id"])

    st, run = cl.call("POST", "/api/v1/runs", {"runner_id": arid, "host": "aud"}, token=akey)
    arun = run.get("run_id")
    c.check("run created", st == 200 and arun, run)
    cl.call("DELETE", "/api/v1/runs/" + arun)

    st, au = cl.call("GET", "/api/v1/audit?limit=500")
    rows = au.get("audit", []) if st == 200 else []
    acts = [(r.get("action"), r.get("detail")) for r in rows]
    actors = {r.get("actor") for r in rows}
    c.check("audit endpoint lists the register", any(a[0] == "register" for a in acts), rows[:3])
    c.check("audit records a command with its kind",
            any(a[0] == "command" and a[1] == "exec" for a in acts), rows[:5])
    c.check("audit records schedule create and delete",
            any(a[0] == "schedule_create" for a in acts)
            and any(a[0] == "schedule_delete" for a in acts), rows[:8])
    c.check("audit records a run delete", any(a[0] == "run_delete" for a in acts), rows[:8])
    c.check("audit rows carry the operator actor", "operator" in actors, actors)

    st, _ = cl.call("GET", "/api/v1/audit", token=None)
    c.check("audit is operator-only", st == 401, st)

    # ---- runner delete cascade (SEARCH-P2-002 / PRIV-P2-003) ----
    st, r2 = cl.call("POST", "/api/v1/register", {"name": "cascade", "kind": "agent"})
    crid, ckey = r2.get("runner_id"), r2.get("api_key")
    st, rr = cl.call("POST", "/api/v1/runs", {"runner_id": crid, "host": "cascade"}, token=ckey)
    crun = rr.get("run_id")
    cl.call("POST", "/api/v1/runs/%s/phase" % crun,
            {"seq": 1, "group": "g", "label": "x"}, token=ckey)
    cl.call("POST", "/api/v1/runs/%s/telemetry" % crun,
            {"samples": [{"elapsed": 1, "temp_c": 50}]}, token=ckey)
    cl.call("POST", "/api/v1/runs/%s/log" % crun, {"lines": ["hello"]}, token=ckey)
    cl.call("POST", "/api/v1/runs/%s/event" % crun,
            {"level": "info", "message": "e"}, token=ckey)
    cl.call("POST", "/api/v1/runs/%s/artifact" % crun,
            {"name": "a.json", "kind": "json", "content": ""}, token=ckey)
    cl.call("POST", "/api/v1/schedules", {"runner_id": crid, "label": "cascade-sched"})

    st, _ = cl.call("DELETE", "/api/v1/runners/" + crid)
    c.check("runner delete ok", st == 200, st)
    c.check("runner row gone", S.one("SELECT id FROM runners WHERE id=?", (crid,)) is None, "")
    c.check("its run row gone", S.one("SELECT id FROM runs WHERE id=?", (crun,)) is None, "")
    for table in ("phases", "telemetry", "logs", "events", "artifacts", "commands"):
        c.check("dependent %s rows gone" % table,
                S.one("SELECT COUNT(*) c FROM %s WHERE run_id=?" % table, (crun,))["c"] == 0, "")
    c.check("its commands gone",
            S.one("SELECT COUNT(*) c FROM commands WHERE runner_id=?", (crid,))["c"] == 0, "")
    c.check("its schedules gone",
            S.one("SELECT COUNT(*) c FROM schedules WHERE runner_id=?", (crid,))["c"] == 0, "")
    st, _ = cl.call("GET", "/api/v1/runs/" + crun)
    c.check("deleted run detail is 404", st == 404, st)

    # ---- SEC-P2-002: CORS ----
    st, _b, h = _raw(srv.base, "GET", "/health", headers={"Origin": "http://evil.example"})
    c.check("foreign Origin gets no CORS header",
            h.get("Access-Control-Allow-Origin") is None, h.get("Access-Control-Allow-Origin"))
    st, _b, h = _raw(srv.base, "GET", "/health", headers={"Origin": srv.base})
    c.check("same-origin Origin is echoed",
            h.get("Access-Control-Allow-Origin") == srv.base,
            h.get("Access-Control-Allow-Origin"))
    prev = B.Handler.cors_origin
    B.Handler.cors_origin = "http://dash.example"
    try:
        st, _b, h = _raw(srv.base, "GET", "/health", headers={"Origin": "http://dash.example"})
        c.check("configured cors-origin is echoed",
                h.get("Access-Control-Allow-Origin") == "http://dash.example",
                h.get("Access-Control-Allow-Origin"))
    finally:
        B.Handler.cors_origin = prev
    st, _b, h = _raw(srv.base, "OPTIONS", "/api/v1/state",
                     headers={"Origin": "http://evil.example"})
    c.check("preflight still answers 204 without a wildcard",
            st == 204 and h.get("Access-Control-Allow-Origin") is None,
            (st, h.get("Access-Control-Allow-Origin")))

    # ---- SEC-P2-003: rate limiting (last: it exhausts the auth bucket) ----
    codes = [cl.call("GET", "/api/v1/state", token="wrong")[0] for _ in range(25)]
    c.check("repeated auth failures eventually 429", 429 in codes, codes[-5:])
    c.check("the first failures are 401", codes[0] == 401, codes[:3])
    c.check("a valid operator request is not throttled",
            cl.call("GET", "/api/v1/state")[0] == 200, "")

    srv.stop()
    return c.fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
