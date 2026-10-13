"""Control-plane (benchd) API tests: auth, runners, runs, telemetry, logs,
artifacts, stats, commands, schedules, delete, reaper, SSE."""
import importlib.util
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

from harness import Checker, Server, TOKEN, REPO, BENCH, tmpdir


def run():
    c = Checker("control_plane")
    srv = Server()
    cl = srv.client()
    B = srv.benchd

    # ---- health + auth ----
    st, r = cl.call("GET", "/health", token=None)
    c.check("health public", st == 200 and r.get("ok"), r)
    st, _ = cl.call("GET", "/api/v1/state", token=None)
    c.check("state needs token", st == 401, st)
    st, _ = cl.call("GET", "/api/v1/state")
    c.check("state with token", st == 200, st)

    # ---- per-runner keys ----
    st, reg = cl.call("POST", "/api/v1/register", {"name": "lab-1", "os": "Linux", "kind": "agent"})
    rid, key = reg.get("runner_id"), reg.get("api_key")
    c.check("register issues api_key", st == 200 and rid and key, reg)
    st, _ = cl.call("POST", "/api/v1/runners/%s/heartbeat" % rid, {"status": "idle"}, token=key)
    c.check("runner key authenticates", st == 200, st)
    st, _ = cl.call("POST", "/api/v1/runners/%s/heartbeat" % rid, {"status": "idle"}, token="bogus")
    c.check("bogus key rejected", st == 401, st)
    st, reg2 = cl.call("POST", "/api/v1/register", {"runner_id": rid, "api_key": key, "name": "lab-1"})
    c.check("re-register keeps id+key", reg2.get("runner_id") == rid and reg2.get("api_key") == key, reg2)

    # ---- stable machine identity (dedupe across lost config / live boots) ----
    st, m1 = cl.call("POST", "/api/v1/register",
                     {"name": "box", "os": "Linux", "kind": "agent", "machine_id": "dmi-abc123"})
    st, m2 = cl.call("POST", "/api/v1/register",
                     {"name": "box", "os": "Linux", "kind": "agent", "machine_id": "dmi-abc123"})
    c.check("machine_id dedupes a re-registering runner",
            m1.get("runner_id") == m2.get("runner_id") and m1.get("api_key") == m2.get("api_key"),
            (m1, m2))

    # a runner must not be able to take over another runner's row by naming its id:
    # any valid runner key passes the POST gate, and the response used to hand back
    # that runner's api_key
    st, victim = cl.call("POST", "/api/v1/register",
                         {"name": "victim", "os": "Linux", "kind": "agent",
                          "machine_id": "dmi-victim"})
    st, attacker = cl.call("POST", "/api/v1/register",
                           {"name": "attacker", "os": "Linux", "kind": "agent",
                            "machine_id": "dmi-attacker"})
    st, steal = cl.call("POST", "/api/v1/register",
                        {"name": "attacker", "os": "Linux", "kind": "agent",
                         "runner_id": victim.get("runner_id")}, token=attacker.get("api_key"))
    c.check("a runner cannot claim another runner's id and get its key",
            steal.get("api_key") != victim.get("api_key"), steal)
    c.check("the takeover attempt does not reuse the victim's row",
            steal.get("runner_id") != victim.get("runner_id"), steal)
    # ...while the owner still can, and keeps its key
    st, again = cl.call("POST", "/api/v1/register",
                        {"name": "victim", "os": "Linux", "kind": "agent",
                         "runner_id": victim.get("runner_id")}, token=victim.get("api_key"))
    c.check("the owner can re-register and keeps its key",
            again.get("runner_id") == victim.get("runner_id")
            and again.get("api_key") == victim.get("api_key"), again)

    # SEC-P1-002: machine_id adoption must not hand an ACTIVE runner's key to a
    # caller that cannot prove ownership. Registration accepts any valid runner
    # bearer, so replaying a victim's machine_id used to return its api_key.
    st, msteal = cl.call("POST", "/api/v1/register",
                         {"name": "attacker", "os": "Linux", "kind": "agent",
                          "machine_id": "dmi-victim"}, token=attacker.get("api_key"))
    c.check("a runner cannot claim another runner's machine_id and get its key",
            msteal.get("api_key") != victim.get("api_key")
            and msteal.get("runner_id") != victim.get("runner_id"), msteal)
    # ...while an OFFLINE row is still reclaimed (a live-USB boot whose config did
    # not persist), rotated so the old credential cannot be replayed either.
    st, dead = cl.call("POST", "/api/v1/register",
                       {"name": "dead", "os": "Linux", "kind": "agent", "machine_id": "dmi-dead"})
    B.Handler.store.ex("UPDATE runners SET last_seen=? WHERE id=?",
                       (B.now() - 3600, dead.get("runner_id")))
    st, revived = cl.call("POST", "/api/v1/register",
                          {"name": "dead", "os": "Linux", "kind": "agent",
                           "machine_id": "dmi-dead"}, token=attacker.get("api_key"))
    c.check("an offline row is reclaimed with a rotated key",
            revived.get("runner_id") == dead.get("runner_id")
            and revived.get("api_key") != dead.get("api_key"), (dead, revived))
    st, m3 = cl.call("POST", "/api/v1/register",
                     {"name": "box", "os": "Linux", "kind": "gui", "machine_id": "dmi-abc123"})
    c.check("machine_id is scoped per kind", m3.get("runner_id") != m1.get("runner_id"), m3)
    st, rl = cl.call("GET", "/api/v1/runners")
    c.check("runners expose a short uid", any(x.get("uid") == "abc123" for x in rl["runners"]),
            [x.get("uid") for x in rl["runners"]])

    # ---- instance keeps same-host dashboards apart (live USB tty1 / ttyS0) ----
    st, i1 = cl.call("POST", "/api/v1/register",
                     {"name": "live", "os": "Linux", "kind": "tui", "machine_id": "dmi-live1",
                      "instance": "tty1"})
    st, i2 = cl.call("POST", "/api/v1/register",
                     {"name": "live", "os": "Linux", "kind": "tui", "machine_id": "dmi-live1",
                      "instance": "ttyS0"})
    c.check("instance keeps same-host dashboards distinct",
            i1.get("runner_id") != i2.get("runner_id"), (i1, i2))
    st, i1b = cl.call("POST", "/api/v1/register",
                      {"name": "live", "os": "Linux", "kind": "tui", "machine_id": "dmi-live1",
                       "instance": "tty1"})
    c.check("instance still dedupes a re-register",
            i1b.get("runner_id") == i1.get("runner_id"), (i1, i1b))
    st, rl2 = cl.call("GET", "/api/v1/runners")
    c.check("runner label carries the instance",
            any(x.get("label") == "live (tty1)" for x in rl2["runners"]),
            [x.get("label") for x in rl2["runners"]])

    # ---- run lifecycle ----
    st, rr = cl.call("POST", "/api/v1/runs", {"runner_id": rid, "host": "lab-1", "label": "Quick",
                                              "preset": "Quick check", "system": {"cpu": "X"}}, token=key)
    run_id = rr.get("run_id")
    c.check("create run", st == 200 and run_id, rr)
    cl.call("POST", "/api/v1/runs/%s/phase" % run_id, {"seq": 1, "group": "gpu", "label": "matmul",
                                                       "status": "running"}, token=key)
    cl.call("POST", "/api/v1/runs/%s/phase" % run_id, {"seq": 1, "group": "gpu", "label": "matmul",
                                                       "status": "done", "t1": time.time()}, token=key)
    cl.call("POST", "/api/v1/runs/%s/phase" % run_id, {"seq": 2, "group": "gpu", "label": "soak",
                                                       "status": "running"}, token=key)
    # a re-posted phase is an upsert: group/label/t0 were silently ignored on the
    # update path, so a phase that renamed itself kept the stale first values
    cl.call("POST", "/api/v1/runs/%s/phase" % run_id, {"seq": 1, "group": "gpu2",
                                                       "label": "matmul-v2", "status": "done",
                                                       "t1": time.time()}, token=key)
    _, det0 = cl.call("GET", "/api/v1/runs/%s" % run_id)
    p1 = [p for p in det0["phases"] if p["seq"] == 1]
    c.check("a phase update applies group and label",
            len(p1) == 1 and p1[0]["grp"] == "gpu2" and p1[0]["label"] == "matmul-v2", p1)
    c.check("a phase update does not duplicate the row",
            len([p for p in det0["phases"] if p["seq"] == 1]) == 1, det0["phases"])

    # named telemetry with extras
    st, r = cl.call("POST", "/api/v1/runs/%s/telemetry" % run_id, {"samples": [
        {"elapsed": 1.0, "temp_c": 60, "power_w": 250, "gpu_util": 99, "mem_util": 55,
         "cpu_temp_c": 70, "vram_used_mib": 9000, "pstate": 0, "load1": 3.2},
        {"elapsed": 2.0, "temp_c": 61, "power_w": 251, "gpu_util": 98, "mem_util": 56,
         "cpu_temp_c": 71, "vram_used_mib": 9100, "pstate": 0, "load1": 3.4}]}, token=key)
    c.check("named telemetry accepted", st == 200 and r.get("accepted") == 2, r)
    st, t = cl.call("GET", "/api/v1/runs/%s/telemetry" % run_id)
    c.check("telemetry rows are dicts", isinstance(t["rows"][0], dict), t["rows"][:1])
    c.check("extras merged", t["rows"][0].get("mem_util") == 55 and t["rows"][1].get("cpu_temp_c") == 71,
            t["rows"][0])
    c.check("core merged", t["rows"][1].get("temp_c") == 61, t["rows"][1])

    # legacy positional telemetry still works
    st, r = cl.call("POST", "/api/v1/runs/%s/telemetry" % run_id,
                    {"samples": [[3.0, 62, 50, 252, 97, 0, 30, 8, 2700, 4]]}, token=key)
    c.check("legacy positional telemetry", st == 200 and r.get("accepted") == 1, r)

    # an impossible percentage must not reach the charts: a live stick run recorded
    # fan_pct 413 from a transient nvidia-smi glitch, and one spike rescales the
    # whole fan series. Dropped, not clamped - clamping would invent a reading.
    st, r = cl.call("POST", "/api/v1/runs/%s/telemetry" % run_id, {"samples": [
        {"elapsed": 4.0, "fan_pct": 413, "gpu_util": 62, "cpu_util": 30, "mem_util": 180},
        {"elapsed": 5.0, "fan_pct": 55, "gpu_util": 100, "cpu_util": 0}]}, token=key)
    c.check("out-of-range telemetry accepted", st == 200, r)
    st, t = cl.call("GET", "/api/v1/runs/%s/telemetry" % run_id)
    by_el = {x.get("elapsed"): x for x in t["rows"]}
    c.check("an impossible fan_pct is dropped, not stored",
            by_el.get(4.0, {}).get("fan_pct") is None, by_el.get(4.0))
    c.check("a valid fan_pct is kept", by_el.get(5.0, {}).get("fan_pct") == 55, by_el.get(5.0))
    c.check("the boundary values 0 and 100 are kept",
            by_el.get(5.0, {}).get("gpu_util") == 100 and by_el.get(5.0, {}).get("cpu_util") == 0,
            by_el.get(5.0))
    c.check("mem_util is deliberately unbounded (drivers exceed 100)",
            by_el.get(4.0, {}).get("mem_util") == 180, by_el.get(4.0))

    # non-finite floats (NaN/Infinity) are not valid JSON: json.dumps emitted them
    # as bare literals, the dashboard's JSON.parse threw, and the run page died.
    # They are nulled on ingest and can never be serialised out.
    st, r = cl.call("POST", "/api/v1/runs/%s/telemetry" % run_id, {"samples": [
        {"elapsed": 6.0, "temp_c": float("inf"), "power_w": float("nan"),
         "fan_pct": float("-inf"), "gpu_util": 50, "bad_extra": float("inf"),
         "ts": float("inf")},
        {"elapsed": 7.0, "temp_c": 60}]}, token=key)
    c.check("non-finite telemetry accepted (sanitised)", st == 200 and r.get("accepted") == 2, r)
    st, raw = cl.call("GET", "/api/v1/runs/%s/telemetry" % run_id, raw=True)
    c.check("no literal NaN/Infinity can be emitted",
            b"Infinity" not in raw and b"NaN" not in raw and b"nan" not in raw, raw[:120])
    t = json.loads(raw.decode())
    by_el = {x.get("elapsed"): x for x in t["rows"]}
    c.check("a non-finite reading is stored as null, not Infinity",
            by_el.get(6.0, {}).get("temp_c") is None and by_el.get(6.0, {}).get("power_w") is None,
            by_el.get(6.0))
    c.check("a non-finite extra is dropped",
            not isinstance(by_el.get(6.0, {}).get("bad_extra"), float), by_el.get(6.0))
    c.check("a finite sample in the same batch is unaffected",
            by_el.get(7.0, {}).get("temp_c") == 60, by_el.get(7.0))

    # ---- per-request batch caps (the byte cap alone left the arrays unbounded) ----
    st, _ = cl.call("POST", "/api/v1/runs/%s/telemetry" % run_id,
                    {"samples": [{"elapsed": i} for i in range(5001)]}, token=key)
    c.check("an oversized telemetry batch -> 413", st == 413, st)
    st, _ = cl.call("POST", "/api/v1/runs/%s/log" % run_id,
                    {"lines": ["x"] * 2001}, token=key)
    c.check("an oversized log batch -> 413", st == 413, st)
    st, _ = cl.call("POST", "/api/v1/runs/%s/telemetry" % run_id,
                    {"samples": {"elapsed": 1}}, token=key)
    c.check("a non-list samples body -> 400", st == 400, st)

    # the importer must read telemetry rows as dicts: indexing them positionally
    # raised KeyError and aborted every Windows import with telemetry in the window
    _imp = open(os.path.join(REPO, "server", "import_results.py"), encoding="utf-8").read()
    c.check("the importer does not index telemetry rows positionally",
            "vals = [r[i] for r in tele" not in _imp and "r.get(name) for r in tele" in _imp, "")
    # static files must be contained by real path, not a raw string prefix
    _bd = open(os.path.join(REPO, "server", "benchd.py"), encoding="utf-8").read()
    c.check("the server can update its own checkout, operator only (source)",
            're.match(r"^/api/v1/server/update$", path)' in _bd
            and "_server_update" in _bd and "copy deploy" in _bd, "")
    c.check("static file serving uses a real containment check",
            "os.path.commonpath([full, root]) == root" in _bd, "")
    c.check("a body size cap exists", "MAX_BODY" in _bd, "")

    # ---- logs ----
    st, r = cl.call("POST", "/api/v1/runs/%s/log" % run_id,
                    {"lines": ["[gpu] info", "[gpu] soak"], "stream": "out"}, token=key)
    c.check("log post", st == 200 and r.get("n") == 2, r)
    st, l = cl.call("GET", "/api/v1/runs/%s/logs" % run_id)
    c.check("log get", len(l["lines"]) == 2 and l["lines"][0]["line"] == "[gpu] info", l)

    # ---- artifact + downloads ----
    import base64
    cl.call("POST", "/api/v1/runs/%s/artifact" % run_id,
            {"name": "report.json", "kind": "json",
             "content": base64.b64encode(b'{"gpu_soak_tflops":81.1}').decode()}, token=key)
    st, det = cl.call("GET", "/api/v1/runs/" + run_id)
    c.check("detail has artifact", any(a["name"] == "report.json" for a in det["artifacts"]), det.get("artifacts"))
    c.check("detail has logs", len(det["logs"]) == 2, len(det.get("logs", [])))
    c.check("detail has stats", det["telemetry_stats"]["stats"]["temp_c"]["max"] == 62, det.get("telemetry_stats"))
    st, body = cl.call("GET", "/api/v1/runs/%s/artifact/report.json?token=%s" % (run_id, TOKEN), raw=True)
    c.check("artifact download", st == 200 and b"gpu_soak_tflops" in body, body[:60])
    # a retried upload must replace the stored copy: the download used to serve
    # the stale first one
    cl.call("POST", "/api/v1/runs/%s/artifact" % run_id,
            {"name": "report.json", "kind": "json",
             "content": base64.b64encode(b'{"gpu_soak_tflops":99.9}').decode()}, token=key)
    st, body = cl.call("GET", "/api/v1/runs/%s/artifact/report.json?token=%s" % (run_id, TOKEN), raw=True)
    c.check("a re-uploaded artifact serves the new copy",
            st == 200 and b"99.9" in body and b"81.1" not in body, body[:60])
    st, body = cl.call("GET", "/api/v1/runs/%s/telemetry.csv?token=%s" % (run_id, TOKEN), raw=True)
    c.check("telemetry.csv download", st == 200 and b"elapsed_s" in body, body[:60])

    # ---- commands ----
    st, c1 = cl.call("POST", "/api/v1/runners/%s/commands" % rid, {"kind": "exec", "payload": {"cmd": "echo hi"}})
    c.check("exec command accepted", st == 200 and c1.get("id"), c1)
    st, cmds = cl.call("GET", "/api/v1/runners/%s/commands" % rid, token=key)
    c.check("runner polls command", len(cmds["commands"]) == 1, cmds)
    st, cmds2 = cl.call("GET", "/api/v1/runners/%s/commands" % rid, token=key)
    c.check("queue drained", len(cmds2["commands"]) == 0, cmds2)
    cl.call("POST", "/api/v1/runners/%s/commands/%s/ack" % (rid, c1["id"]),
            {"status": "done", "result": {"rc": 0}}, token=key)
    st, _ = cl.call("POST", "/api/v1/runners/%s/commands" % rid, {"kind": "nope"})
    c.check("unknown command rejected", st == 400, st)
    for kind in ("shutdown", "reboot"):
        st, r = cl.call("POST", "/api/v1/runners/%s/commands" % rid, {"kind": kind})
        c.check("%s command accepted" % kind, st == 200 and r.get("id"), (st, r))

    # ---- authorization: a runner key is scoped to its own runner + run ----
    st, other = cl.call("POST", "/api/v1/register", {"name": "lab-2", "os": "Linux", "kind": "agent"})
    orid, okey = other.get("runner_id"), other.get("api_key")
    st, _ = cl.call("POST", "/api/v1/runs/%s/phase" % run_id,
                    {"seq": 9, "group": "gpu", "label": "x", "status": "running"}, token=okey)
    c.check("runner cannot write another runner's run", st in (403, 404), st)
    st, _ = cl.call("POST", "/api/v1/runs/%s/telemetry" % run_id, {"samples": []}, token=okey)
    c.check("runner cannot post telemetry to another run", st in (403, 404), st)
    st, _ = cl.call("POST", "/api/v1/runners/%s/heartbeat" % rid, {"status": "idle"}, token=okey)
    c.check("runner cannot heartbeat as another runner", st == 403, st)
    st, _ = cl.call("POST", "/api/v1/schedules", {"runner_id": rid, "label": "x"}, token=okey)
    c.check("runner cannot create schedules", st == 403, st)
    st, _ = cl.call("POST", "/api/v1/runs/%s/event" % run_id,
                    {"level": "info", "message": "x"}, token=okey)
    c.check("runner cannot write events to another run", st in (403, 404), st)

    # ---- malformed input must be rejected, not 500 ----
    for q in ("?limit=abc", "?offset=x", "?limit=-5", "?limit=99999"):
        st, _ = cl.call("GET", "/api/v1/runs" + q)
        c.check("bad query %s -> not 5xx" % q, st < 500, st)
    st, _ = cl.call("GET", "/api/v1/runs/%s/telemetry?max=zzz" % run_id)
    c.check("bad telemetry max -> not 5xx", st < 500, st)
    st, _ = cl.call("GET", "/api/v1/runs/%s/logs?after=x&limit=y" % run_id)
    c.check("bad log paging -> not 5xx", st < 500, st)
    st, _ = cl.call("POST", "/api/v1/runners/no-such-runner/heartbeat", {"status": "idle"})
    c.check("heartbeat unknown runner -> 404", st == 404, st)
    st, _ = cl.call("POST", "/api/v1/runners/no-such-runner/commands", {"kind": "stop"})
    c.check("commands for an unknown runner -> 404", st == 404, st)

    # ---- malformed / oversized / truncated bodies ----
    port = srv.httpd.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.putrequest("POST", "/api/v1/runs/%s/finish" % run_id)
    conn.putheader("Authorization", "Bearer " + TOKEN)
    conn.putheader("Content-Length", str(25 * 1024 * 1024))
    conn.endheaders()                       # declare a huge body and send nothing
    resp = conn.getresponse()
    c.check("a declared-oversized body -> 413 (not a pinned thread)",
            resp.status == 413, resp.status)
    resp.read()
    conn.close()

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", "/api/v1/runs/%s/finish" % run_id, body=b"{",
                 headers={"Authorization": "Bearer " + TOKEN})
    resp = conn.getresponse()
    c.check("a malformed JSON body -> 400", resp.status == 400, resp.status)
    resp.read()
    conn.close()
    c.check("a malformed finish did not mark the run done",
            cl.call("GET", "/api/v1/runs/%s" % run_id)[1]["status"] == "running", "")

    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    s.sendall(("POST /api/v1/runs/%s/finish HTTP/1.0\r\nAuthorization: Bearer %s\r\n"
               "Content-Length: 10\r\n\r\n{\"a\":1}" % (run_id, TOKEN)).encode())
    s.shutdown(socket.SHUT_WR)              # truncate: 7 bytes sent, 10 declared
    head = s.recv(200)
    s.close()
    c.check("a truncated body -> 400", b" 400 " in head.split(b"\r\n", 1)[0], head[:40])

    # ---- binary artifacts survive the round-trip ----
    import base64 as _b64
    blob = bytes(range(256)) * 4
    cl.call("POST", "/api/v1/runs/%s/artifact" % run_id,
            {"name": "blob.bin", "kind": "bin",
             "content": _b64.b64encode(blob).decode()}, token=key)
    st, body = cl.call("GET", "/api/v1/runs/%s/artifact/blob.bin?token=%s" % (run_id, TOKEN), raw=True)
    c.check("binary artifact round-trips byte-for-byte",
            st == 200 and body == blob, (st, len(body)))

    # ---- per-sample telemetry timestamps are honoured ----
    cl.call("POST", "/api/v1/runs/%s/telemetry" % run_id,
            {"samples": [{"elapsed": 1, "temp_c": 50, "ts": 1600000000.0},
                         {"elapsed": 2, "temp_c": 51, "ts": 1600000001.0}]}, token=key)
    st, t2 = cl.call("GET", "/api/v1/runs/%s/telemetry" % run_id)
    c.check("telemetry honours per-sample ts",
            any(abs((x.get("ts") or 0) - 1600000000.0) < 1.0 for x in t2["rows"]),
            [x.get("ts") for x in t2["rows"]][-3:])

    # ---- finish + trends ----
    st, _ = cl.call("POST", "/api/v1/runs/%s/finish" % run_id,
                    {"status": "done", "exit_code": 0, "ended": time.time(),
                     "summary": {"gpu_soak_tflops": 81.1, "net": {"download_mbps": 248}}}, token=key)
    c.check("finish run", st == 200, st)
    st, runs = cl.call("GET", "/api/v1/runs?host=lab-1")
    c.check("runs list", runs["total"] == 1 and runs["runs"][0]["status"] == "done", runs)
    c.check("preset recorded", runs["runs"][0]["preset"] == "Quick check", runs["runs"][0])
    st, tr = cl.call("GET", "/api/v1/trends?host=lab-1&metric=gpu_soak_tflops")
    c.check("trends", tr["series"]["gpu_soak_tflops"][0]["v"] == 81.1, tr)
    ts_ = tr.get("stats", {}).get("gpu_soak_tflops", {})
    c.check("trends carry percentile stats",
            ts_.get("n") == 1 and ts_.get("p95") == 81.1 and "p50" in ts_, ts_)

    # ---- API consistency: finished/unknown runs, idempotent create, query token ----
    st, _ = cl.call("POST", "/api/v1/runs/%s/telemetry" % run_id,
                    {"samples": [{"elapsed": 99}]}, token=key)
    c.check("telemetry into a finished run -> 409", st == 409, st)
    st, _ = cl.call("POST", "/api/v1/runs/%s/finish" % run_id,
                    {"status": "done", "summary": {"x": 1}}, token=key)
    c.check("a second finish -> 409", st == 409, st)
    st, _ = cl.call("POST", "/api/v1/runs/no-such-run/finish", {"status": "done"}, token=key)
    c.check("finish on an unknown run -> 404", st == 404, st)
    st, r2 = cl.call("POST", "/api/v1/runs", {"id": run_id, "runner_id": rid, "label": "retry"},
                     token=key)
    c.check("re-posting a run id is idempotent", st == 200 and r2.get("existing"), (st, r2))
    st, _ = cl.call("POST", "/api/v1/runs", {"id": "bad-started-1", "started": "yesterday",
                                             "runner_id": rid}, token=key)
    c.check("a non-numeric started -> 400", st == 400, st)
    st, _ = cl.call("GET", "/api/v1/state?token=" + TOKEN, token=None)
    c.check("?token= is not accepted on ordinary GETs", st == 401, st)
    st, _ = cl.call("GET", "/api/v1/runs/%s/telemetry.csv?token=%s" % (run_id, TOKEN),
                    token=None, raw=True)
    c.check("?token= still works for downloads", st == 200, st)
    st, _ = cl.call("GET", "/api/v1/server/update")
    c.check("the side-effecting server update is POST-only", st == 404, st)

    # ---- schedules ----
    st, sc = cl.call("POST", "/api/v1/schedules", {"runner_id": rid, "label": "nightly",
                                                   "profile": {"label": "Full run"}, "kind": "every",
                                                   "every_s": 3600})
    c.check("create schedule", st == 200 and sc.get("id"), sc)
    B.Handler.store.ex("UPDATE schedules SET next_run=? WHERE id=?", (time.time() - 5, sc["id"]))
    n = B.scheduler_tick(B.Handler.store, B.Handler.bus)
    c.check("scheduler fires", n == 1, n)
    st, cmds = cl.call("GET", "/api/v1/runners/%s/commands" % rid, token=key)
    c.check("scheduled start queued", any(x["kind"] == "start" for x in cmds["commands"]), cmds)
    st, _ = cl.call("DELETE", "/api/v1/schedules/" + sc["id"])
    c.check("delete schedule", cl.call("GET", "/api/v1/schedules")[1]["schedules"] == [], "")

    # ---- reaper ----
    st = B.Handler.store
    st.ex("INSERT INTO runners(id,name,last_seen,status,current_run) VALUES(?,?,?,?,?)",
          ("stale-r", "lab-stale", time.time() - 300, "busy", "stale-run"))
    st.ex("INSERT INTO runs(id,runner_id,host,label,started,status) VALUES(?,?,?,?,?,?)",
          ("stale-run", "stale-r", "lab-stale", "x", time.time() - 300, "running"))
    st.ex("INSERT INTO runners(id,name,last_seen,status,current_run) VALUES(?,?,?,?,?)",
          ("live-r", "lab-live", time.time(), "busy", "live-run"))
    st.ex("INSERT INTO runs(id,runner_id,host,label,started,status) VALUES(?,?,?,?,?,?)",
          ("live-run", "live-r", "lab-live", "y", time.time() - 300, "running"))
    B.reap_stale_runs(st, B.Handler.bus)
    c.check("reaper fails stale run", st.one("SELECT status FROM runs WHERE id='stale-run'")["status"] == "failed", "")
    c.check("reaper leaves live run", st.one("SELECT status FROM runs WHERE id='live-run'")["status"] == "running", "")

    st.ex("INSERT INTO runners(id,name,last_seen,status,ephemeral) VALUES(?,?,?,?,?)",
          ("eph-r", "self-test", time.time() - 300, "idle", 1))
    B.reap_ephemeral_runners(st, B.Handler.bus)
    c.check("reaper drops a stale ephemeral runner",
            st.one("SELECT id FROM runners WHERE id='eph-r'") is None, "")

    # a command a runner fetched but never acked must not sit in 'sent' forever
    st.ex("INSERT INTO commands(id,runner_id,kind,payload,status,created,sent) "
          "VALUES(?,?,?,?,?,?,?)",
          ("stuck-c", rid, "start", "{}", "sent", time.time() - 400, time.time() - 400))
    st.ex("INSERT INTO commands(id,runner_id,kind,payload,status,created,sent) "
          "VALUES(?,?,?,?,?,?,?)",
          ("fresh-c", rid, "start", "{}", "sent", time.time() - 5, time.time() - 5))
    B.reap_stale_commands(st, B.Handler.bus)
    c.check("reaper fails a never-acked command",
            st.one("SELECT status FROM commands WHERE id='stuck-c'")["status"] == "failed", "")
    c.check("reaper leaves a just-sent command",
            st.one("SELECT status FROM commands WHERE id='fresh-c'")["status"] == "sent", "")

    # a long download must not be called dropped while it is still working
    st.ex("INSERT INTO commands(id,runner_id,kind,payload,status,created,sent) "
          "VALUES(?,?,?,?,?,?,?)",
          ("iso-c", rid, "iso_fetch", '{"tag": "x"}', "sent", time.time() - 400,
           time.time() - 400))
    st.ex("INSERT INTO commands(id,runner_id,kind,payload,status,created,sent) "
          "VALUES(?,?,?,?,?,?,?)",
          ("exec-c", rid, "exec", '{"cmd": "sleep 1", "timeout": 600}', "sent",
           time.time() - 500, time.time() - 500))
    st.ex("INSERT INTO commands(id,runner_id,kind,payload,status,created,sent) "
          "VALUES(?,?,?,?,?,?,?)",
          ("exec-d", rid, "exec", '{"cmd": "sleep 1", "timeout": 60}', "sent",
           time.time() - 500, time.time() - 500))
    B.reap_stale_commands(st, B.Handler.bus)
    c.check("reaper spares an in-progress ISO fetch",
            st.one("SELECT status FROM commands WHERE id='iso-c'")["status"] == "sent", "")
    c.check("reaper spares an exec inside its payload timeout",
            st.one("SELECT status FROM commands WHERE id='exec-c'")["status"] == "sent", "")
    c.check("reaper still drops an exec past its payload timeout",
            st.one("SELECT status FROM commands WHERE id='exec-d'")["status"] == "failed", "")
    c.check("ack_timeout scales with the kind and its payload timeout",
            B.ack_timeout("start") == 120
            and B.ack_timeout("iso_fetch") > 3600
            and B.ack_timeout("exec", '{"timeout": 600}') == 660, "")

    # ---- delete ----
    # deleting a run must clear a runner still pointing at it (it would otherwise
    # look busy forever and block schedules)
    B.Handler.store.ex("UPDATE runners SET status='busy', current_run=? WHERE id=?",
                       (run_id, rid))
    st, _ = cl.call("DELETE", "/api/v1/runs/" + run_id)
    c.check("delete run", cl.call("GET", "/api/v1/runs/%s" % run_id)[0] == 404, "")
    c.check("deleting a run clears the runner's current_run",
            B.Handler.store.one("SELECT current_run FROM runners WHERE id=?",
                                (rid,))["current_run"] is None, "")
    st, _ = cl.call("DELETE", "/api/v1/runners/" + rid)
    c.check("delete runner", not any(x["id"] == rid for x in cl.call("GET", "/api/v1/runners")[1]["runners"]), "")

    # ---- SSE ----
    try:
        req = urllib.request.Request(srv.base + "/api/v1/stream?token=" + TOKEN)
        with urllib.request.urlopen(req, timeout=3) as resp:
            first = resp.readline().decode()
        c.check("sse opens", first.startswith("event: snapshot"), first)
    except Exception as e:
        c.check("sse opens", False, e)

    # ---- toolkit version + staleness ----
    st, h = cl.call("GET", "/health", token=None)
    c.check("health exposes the control-plane commit", "git" in h, h)
    st, stt = cl.call("GET", "/api/v1/state")
    c.check("state exposes server_git", "server_git" in stt, list(stt)[:6])
    st, reg3 = cl.call("POST", "/api/v1/register",
                       {"name": "v1", "os": "Linux", "kind": "agent",
                        "capabilities": {"toolkit": {"mode": "copy", "commit": "abc1234"}}})
    row = [x for x in cl.call("GET", "/api/v1/runners")[1]["runners"]
           if x["id"] == reg3["runner_id"]][0]
    c.check("runner exposes its toolkit commit", row.get("toolkit_commit") == "abc1234", row)
    c.check("staleness is a bool", isinstance(row.get("update_available"), bool), row.get("update_available"))
    c.check("a different commit is flagged stale", row.get("update_available") is True, row)
    hh = cl.call("GET", "/health", token=None)[1]
    if hh.get("git"):
        st, reg4 = cl.call("POST", "/api/v1/register",
                           {"name": "v2", "os": "Linux", "kind": "agent",
                            "capabilities": {"toolkit": {"mode": "git", "commit": hh["git"]}}})
        row4 = [x for x in cl.call("GET", "/api/v1/runners")[1]["runners"]
                if x["id"] == reg4["runner_id"]][0]
        c.check("a runner at the latest commit is NOT stale",
                row4.get("update_available") is False, row4)

    # ---- a docs-only difference is not an update ----
    c.check("update_matters ignores docs", B.update_matters(["docs/DATA.md"]) is False, "")
    c.check("update_matters ignores CI and notes",
            B.update_matters([".github/workflows/build-iso.yml", "CHANGELOG.md"]) is False, "")
    c.check("update_matters ignores example/result data",
            B.update_matters(["examples/run.json", "results/x/telemetry.csv"]) is False, "")
    c.check("update_matters ignores the control plane and tests",
            B.update_matters(["server/benchd.py", "tests/test_control_plane.py"]) is False, "")
    c.check("update_matters flags toolkit code", B.update_matters(["bench/gui.py"]) is True, "")
    c.check("update_matters flags the tools a runner uses",
            B.update_matters(["tools/lhm/LibreHardwareMonitorLib.dll"]) is True, "")
    c.check("update_matters flags the live-USB scripts",
            B.update_matters(["live/Build-LiveUSB.sh"]) is True, "")
    c.check("update_matters flags a mixed range",
            B.update_matters(["docs/DATA.md", "live/Build-LiveUSB.sh"]) is True, "")
    c.check("update_matters is conservative when the file list is unknown",
            B.update_matters([]) is True, "")
    c.check("compare_detail short-circuits identical commits",
            B.compare_detail("abc1234", "abc1234") == ("identical", []), "")
    c.check("compare_detail reports unknown without commits",
            B.compare_detail("", "abc1234") == ("", []), "")
    c.check("compare_commits still returns just the status",
            B.compare_commits("abc1234", "abc1234") == "identical", "")

    # ---- a run orphaned by a runner restart must not stay 'running' ----
    # The runner comes back, registers and goes idle; the heartbeat rule cannot see it.
    _spec_or = importlib.util.spec_from_file_location(
        "benchd_orphan", os.path.join(REPO, "server", "benchd.py"))
    _bdm = importlib.util.module_from_spec(_spec_or)
    _spec_or.loader.exec_module(_bdm)
    import time as _t
    _orph = {"id": "run-orphan", "runner_id": "r1", "started": _t.time() - 600}
    _idle = {"id": "r1", "last_seen": _t.time(), "status": "idle", "current_run": None}
    _busy = {"id": "r1", "last_seen": _t.time(), "status": "busy", "current_run": "run-orphan"}

    class _Store:
        def __init__(self, runner):
            self.runner = runner
            self.sql = []

        def q(self, sql, args=()):
            return [_orph] if "FROM runs" in sql else []

        def one(self, sql, args=()):
            self.sql.append(sql)
            if "FROM runners" in sql:
                return self.runner
            if "MAX(ts)" in sql:
                return {"m": _t.time() - 300}      # quiet for five minutes
            return {"n": 0}

        def ex(self, sql, args=()):
            self.sql.append(sql)
            return None

    class _Bus:
        def __init__(self):
            self.events = []

        def publish(self, e):
            self.events.append(e)

    st1, bus1 = _Store(_busy), _Bus()
    _bdm.reap_stale_runs(st1, bus1)
    c.check("a busy runner keeps its run",
            not any("UPDATE runs" in x for x in st1.sql) and not bus1.events, st1.sql)
    st2, bus2 = _Store(_idle), _Bus()
    _bdm.reap_stale_runs(st2, bus2)
    c.check("an idle runner orphans its run, and the reaper fails it",
            any("UPDATE runs SET status='failed'" in x for x in st2.sql) and bus2.events,
            st2.sql)
    # the residual gap: the runner restarted, picked up a NEWER run, and the old
    # one was never examined (the old condition only looked at an empty current_run)
    _newer = {"id": "r1", "last_seen": _t.time(), "status": "busy", "current_run": "run-newer"}
    st3, bus3 = _Store(_newer), _Bus()
    _bdm.reap_stale_runs(st3, bus3)
    c.check("a runner on a newer run orphans the old one, and the reaper fails it",
            any("UPDATE runs SET status='failed'" in x for x in st3.sql) and bus3.events,
            st3.sql)

    # ---- the control plane auto-updates itself when it can ----
    _spec_au = importlib.util.spec_from_file_location(
        "benchd_autoupdate", os.path.join(REPO, "server", "benchd.py"))
    _bdm = importlib.util.module_from_spec(_spec_au)
    _spec_au.loader.exec_module(_bdm)

    class _BusyStore:
        def __init__(self, busy):
            self.busy = busy

        def one(self, sql, args=()):
            return {"n": 1 if self.busy else 0}

    _pulled = []
    _real_pull, _real_sg, _real_lg = _bdm.server_pull, _bdm.server_git, _bdm.latest_git
    _bdm.server_pull = lambda: (_pulled.append(1) or {"ok": True, "before": "a", "after": "b"})
    _bdm.AUTO_UPDATE_S = 0.001
    _bdm._AUTO_UPDATE_LAST[0] = 0.0
    _bdm.AUTO_UPDATE_OFF = False
    _bdm.server_git = lambda: "aaa"
    _bdm.latest_git = lambda: "aaa"
    c.check("auto-update does nothing when it is already current",
            _bdm.auto_update(_BusyStore(False)) is None and not _pulled, _pulled)
    _bdm.latest_git = lambda: "bbb"
    c.check("auto-update defers while a run is active",
            _bdm.auto_update(_BusyStore(True)) is None and not _pulled, _pulled)
    _bdm._AUTO_UPDATE_LAST[0] = 0.0        # the calls above consumed the window
    c.check("auto-update pulls when behind and idle",
            _bdm.auto_update(_BusyStore(False)) and _pulled, _pulled)
    _bdm.AUTO_UPDATE_OFF = True
    _pulled.clear()
    c.check("auto-update can be switched off",
            _bdm.auto_update(_BusyStore(False)) is None and not _pulled, _pulled)
    _bdm.server_pull, _bdm.server_git, _bdm.latest_git = _real_pull, _real_sg, _real_lg

    # ---- retention: bound the per-sample tables, keep the runs and their stats ----
    _spec3 = importlib.util.spec_from_file_location(
        "benchd_retention", os.path.join(REPO, "server", "benchd.py"))
    _bdm = importlib.util.module_from_spec(_spec3)
    _spec3.loader.exec_module(_bdm)
    _seen = []

    class _Cur:
        rowcount = 5

    class _FakeStore:
        def q(self, sql, args=()):
            _seen.append((sql, args))
            return [{"id": "run-old-1"}, {"id": "run-old-2"}]

        def ex(self, sql, args=()):
            _seen.append((sql, args))
            return _Cur()

    _bdm.RETENTION_DAYS = 30.0
    _bdm._RETENTION_LAST[0] = 0.0
    n = _bdm.retain_old_data(_FakeStore())
    c.check("retention selects finished runs older than the cutoff",
            "status != 'running'" in _seen[0][0] and _seen[0][1][0] < time.time(),
            _seen[0])
    c.check("retention sweeps telemetry, logs, events and artifacts per run",
            n == 45 and len([s for s, _a in _seen if s.startswith("UPDATE")]) == 2 and
            len([s for s, _a in _seen if s.startswith("DELETE")]) == 9,
            (n, len(_seen)))
    c.check("retention stores a run's stats before deleting its samples",
            [s.split()[0] for s, _a in _seen if s.startswith(("UPDATE", "DELETE"))] ==
            ["UPDATE", "DELETE", "DELETE", "DELETE", "DELETE",
             "UPDATE", "DELETE", "DELETE", "DELETE", "DELETE", "DELETE"],
            [s[:24] for s, _a in _seen if s.startswith(("UPDATE", "DELETE"))])
    c.check("retention computes the stats from the run's own samples",
            len([s for s, _a in _seen if s.startswith("SELECT * FROM telemetry")]) == 2, "")
    c.check("retention deletes per run, not one sweep of everything",
            all("WHERE run_id=?" in s and "IN (SELECT" not in s
                for s, _a in _seen if s.startswith("DELETE") and "commands" not in s), "")
    _cmd = [(s, a) for s, a in _seen if s.startswith("DELETE FROM commands")]
    c.check("retention prunes terminal commands older than the cutoff",
            len(_cmd) == 1 and "status IN ('done','failed')" in _cmd[0][0]
            and _cmd[0][1][0] < time.time(), _cmd)
    c.check("retention covers every old run it selected",
            {a[0] for s, a in _seen if s.startswith("DELETE") and "commands" not in s} ==
            {"run-old-1", "run-old-2"} and
            {a[1] for s, a in _seen if s.startswith("UPDATE")} ==
            {"run-old-1", "run-old-2"}, "")
    before = len(_seen)
    c.check("retention is throttled between sweeps",
            _bdm.retain_old_data(_FakeStore()) == 0 and len(_seen) == before, "")
    c.check("a forced sweep runs regardless of the throttle",
            _bdm.retain_old_data(_FakeStore(), force=True) == 45, "")
    _bdm.RETENTION_DAYS = 0.0
    _bdm._RETENTION_LAST[0] = 0.0
    settled = len(_seen)
    c.check("retention disabled keeps everything",
            _bdm.retain_old_data(_FakeStore()) == 0 and len(_seen) == settled, "")

    # ...and a real sweep removes artifact content and terminal commands while a
    # fresh terminal command survives (they hold base64/rows in the same DB)
    import base64 as _b64r
    _rs = B.Handler.store
    _old_t = time.time() - 10 * 86400
    _rs.ex("INSERT INTO runs(id,runner_id,host,label,started,ended,status) VALUES(?,?,?,?,?,?,?)",
           ("ret-old", "gone-r", "lab-1", "old", _old_t, _old_t + 60, "done"))
    _rs.ex("INSERT INTO artifacts(run_id,name,kind,size,sha256,content) VALUES(?,?,?,?,?,?)",
           ("ret-old", "big.bin", "bin", 4, None, _b64r.b64encode(b"data").decode()))
    _rs.ex("INSERT INTO commands(id,runner_id,kind,payload,status,created,acked) "
           "VALUES(?,?,?,?,?,?,?)",
           ("ret-cmd", "gone-r", "exec", "{}", "done", _old_t, _old_t))
    _rs.ex("INSERT INTO commands(id,runner_id,kind,payload,status,created) VALUES(?,?,?,?,?,?)",
           ("keep-cmd", "gone-r", "exec", "{}", "queued", time.time()))
    _bdm.RETENTION_DAYS = 1.0
    _bdm._RETENTION_LAST[0] = 0.0
    _bdm.retain_old_data(_rs, force=True)
    c.check("retention removes old artifact content from the database",
            _rs.one("SELECT COUNT(*) c FROM artifacts WHERE run_id='ret-old'")["c"] == 0, "")
    c.check("retention prunes old terminal commands, keeps live ones",
            _rs.one("SELECT COUNT(*) c FROM commands WHERE id='ret-cmd'")["c"] == 0
            and _rs.one("SELECT COUNT(*) c FROM commands WHERE id='keep-cmd'")["c"] == 1, "")

    # the run detail must still have stats once the samples are gone
    _stored = {"samples": 400, "stats": {"temp_c": {"max": 62}}, "derived": {"energy_j": 1}}

    class _StatsStore:
        def __init__(self, rows, stored=True):
            self.rows = rows
            self.stored = stored
            self.one_calls = 0

        def q(self, sql, args=()):
            return self.rows

        def one(self, sql, args=()):
            self.one_calls += 1
            return {"stats": json.dumps(_stored)} if self.stored else None

    class _Self:
        def __init__(self, rows, stored=True):
            self.store = _StatsStore(rows, stored)

    live = _Self([{"ts": 1.0, "elapsed": 1.0, "temp_c": 50.0},
                  {"ts": 2.0, "elapsed": 2.0, "temp_c": 60.0}])
    st = _bdm.Handler._compute_stats(live, "run-live")
    c.check("stats come from the samples while they exist",
            st["samples"] == 2 and st["stats"]["temp_c"]["max"] == 60 and
            live.store.one_calls == 0, st.get("stats", {}).get("temp_c"))
    swept = _Self([])
    st = _bdm.Handler._compute_stats(swept, "run-swept")
    c.check("a swept run still reports the stats stored at retention time",
            st == _stored and swept.store.one_calls == 1, st)
    c.check("a run with no samples and no stored stats is still empty, not an error",
            _bdm.Handler._compute_stats(_Self([], stored=False), "run-nothing")["samples"] == 0,
            "")

    # ---- config parsing and self-update failure reporting ----
    os.environ["BENCHD_TEST_BAD_FLOAT"] = "30d"
    c.check("a bad numeric env var falls back instead of crashing the unit",
            _bdm.env_float("BENCHD_TEST_BAD_FLOAT", 30.0) == 30.0, "")
    os.environ["BENCHD_TEST_BAD_FLOAT"] = "7.5"
    c.check("a valid numeric env var is used",
            _bdm.env_float("BENCHD_TEST_BAD_FLOAT", 30.0) == 7.5, "")
    os.environ["BENCHD_TEST_BAD_FLOAT"] = "nan"
    c.check("a non-finite env var falls back",
            _bdm.env_float("BENCHD_TEST_BAD_FLOAT", 30.0) == 30.0, "")
    del os.environ["BENCHD_TEST_BAD_FLOAT"]

    _cdir = tmpdir()
    with open(os.path.join(_cdir, "benchd.conf"), "w", encoding="utf-8") as fh:
        fh.write("# comment line\nBENCHD_TOKEN=abc123  # rotated last week\n")
    _real_here = _bdm.HERE
    _bdm.HERE = _cdir
    c.check("find_conf_value strips inline comments",
            _bdm.find_conf_value("BENCHD_TOKEN") == "abc123", _bdm.find_conf_value("BENCHD_TOKEN"))
    with open(os.path.join(_cdir, "benchd.conf"), "w", encoding="utf-8") as fh:
        fh.write('BENCHD_TOKEN="quoted value"\n')
    c.check("find_conf_value unwraps a quoted value",
            _bdm.find_conf_value("BENCHD_TOKEN") == "quoted value", "")
    _bdm.HERE = _real_here

    # a failed git pull must be reported as not ok (server_pull ignored return codes)
    _gd = tmpdir()
    os.makedirs(os.path.join(_gd, "server"))
    shutil.copy(os.path.join(REPO, "server", "benchd.py"),
                os.path.join(_gd, "server", "benchd.py"))

    def _git(*a):
        return subprocess.run(["git", "-C", _gd] + list(a), capture_output=True, text=True)

    _git("init", "-q")
    _git("remote", "add", "origin", os.path.join(_gd, "no-such-remote.git"))
    _spec_pull = importlib.util.spec_from_file_location(
        "benchd_pull", os.path.join(_gd, "server", "benchd.py"))
    _pdm = importlib.util.module_from_spec(_spec_pull)
    _spec_pull.loader.exec_module(_pdm)
    _res = _pdm.server_pull()
    c.check("a failed git pull is reported as not ok", _res.get("ok") is False, _res)

    # server_pull must never fetch with --depth: a shallow fetch grafts the
    # checkout down to one history-less commit, breaking git describe (the
    # version scheme) and the fleet's commit-compare/staleness ancestry walks.
    c.check("server_pull no longer fetches with --depth", '"--depth"' not in _bd, "")
    c.check("server_pull fetches tags and can heal a shallow checkout",
            '"--tags"' in _bd and '"--unshallow"' in _bd, "")

    def _g(*a):
        return subprocess.run(["git"] + list(a), capture_output=True, text=True)

    # a throwaway origin (two commits + a tag) and a depth-1 clone of it
    _src = tmpdir()
    _g("init", "-q", _src)
    _g("-C", _src, "config", "user.email", "t@t")
    _g("-C", _src, "config", "user.name", "t")
    open(os.path.join(_src, "f.txt"), "w").write("one\n")
    _g("-C", _src, "add", "f.txt")
    _g("-C", _src, "commit", "-qm", "one")
    _g("-C", _src, "tag", "v9.9.9")
    open(os.path.join(_src, "f.txt"), "w").write("two\n")
    _g("-C", _src, "add", "f.txt")
    _g("-C", _src, "commit", "-qm", "two")
    _sh = tmpdir()
    _url = "file:///" + _src.replace("\\", "/").lstrip("/")
    _g("clone", "-q", "--depth", "1", _url, _sh)
    c.check("the throwaway clone is shallow",
            os.path.exists(os.path.join(_sh, ".git", "shallow")), "")
    _g("-C", _sh, "config", "core.abbrev", "12")   # keep before==after (no restart spawn)
    os.makedirs(os.path.join(_sh, "server"))
    shutil.copy(os.path.join(REPO, "server", "benchd.py"),
                os.path.join(_sh, "server", "benchd.py"))
    _spec_sh = importlib.util.spec_from_file_location(
        "benchd_shallow", os.path.join(_sh, "server", "benchd.py"))
    _sdm = importlib.util.module_from_spec(_spec_sh)
    _spec_sh.loader.exec_module(_sdm)
    _res_sh = _sdm.server_pull()
    c.check("server_pull heals a shallow checkout",
            _res_sh.get("ok") is True
            and not os.path.exists(os.path.join(_sh, ".git", "shallow")), _res_sh)
    _cnt = _g("-C", _sh, "rev-list", "--count", "HEAD").stdout.strip()
    c.check("the healed checkout has its ancestry back",
            _cnt.isdigit() and int(_cnt) >= 2, _cnt)
    _desc = _g("-C", _sh, "describe", "--tags", "--long", "--match", "v[0-9]*")
    c.check("git describe works again after the heal",
            _desc.returncode == 0 and "9.9.9" in _desc.stdout,
            (_desc.stdout, _desc.stderr))
    # the race: the shallow marker is seen, then the repo turns out to be complete
    _real_exists = os.path.exists
    os.path.exists = lambda p: True if str(p).endswith("shallow") else _real_exists(p)
    try:
        _res_race = _sdm.server_pull()
    finally:
        os.path.exists = _real_exists
    c.check("an already-complete repo is success, not a failed update",
            _res_race.get("ok") is True, _res_race)

    # ---- fail-closed auth: no token must refuse to start ----
    c.check("no token without --allow-anonymous is refused",
            _bdm.auth_refusal(None, False) is not None, "")
    _ref = _bdm.auth_refusal(None, False) or ""
    c.check("the refusal names the fix",
            "BENCHD_TOKEN" in _ref and "allow-anonymous" in _ref, _ref)
    c.check("a token or --allow-anonymous allows serving",
            _bdm.auth_refusal("tok", False) is None
            and _bdm.auth_refusal(None, True) is None, "")
    _home = tmpdir()
    _env = {k: v for k, v in os.environ.items() if k != "BENCHD_TOKEN"}
    _env["USERPROFILE"] = _home
    _env["HOME"] = _home
    _p = subprocess.run([sys.executable, os.path.join(REPO, "server", "benchd.py"),
                         "--host", "127.0.0.1", "--port", "0",
                         "--db", os.path.join(_home, "auth.sqlite")],
                        capture_output=True, text=True, timeout=60, env=_env, cwd=REPO)
    c.check("benchd refuses to start with no token (exit 2)",
            _p.returncode == 2 and "BENCHD_TOKEN" in (_p.stderr + _p.stdout),
            (_p.returncode, (_p.stderr or "")[-200:]))

    # ---- an operator key rotation must store what it returns ----
    st, rot = cl.call("POST", "/api/v1/register",
                      {"name": "rotate", "os": "Linux", "kind": "agent"})
    rrid, rkey = rot.get("runner_id"), rot.get("api_key")
    st, rot2 = cl.call("POST", "/api/v1/register",
                       {"runner_id": rrid, "api_key": "rotated-key-123", "name": "rotate"})
    newkey = rot2.get("api_key")
    c.check("an operator re-register returns a key that was actually stored",
            st == 200 and rot2.get("runner_id") == rrid and newkey and newkey != rkey, rot2)
    st, _ = cl.call("POST", "/api/v1/runners/%s/heartbeat" % rrid, {"status": "idle"},
                    token=newkey)
    c.check("the returned key authenticates", st == 200, st)
    st, _ = cl.call("POST", "/api/v1/runners/%s/heartbeat" % rrid, {"status": "idle"},
                    token=rkey)
    c.check("the superseded key no longer authenticates", st == 401, st)
    _rs.ex("UPDATE runners SET current_run='stale-run', status='busy' WHERE id=?", (rrid,))
    st, _ = cl.call("POST", "/api/v1/register", {"runner_id": rrid, "name": "rotate"})
    c.check("a re-register clears a stale current_run",
            st == 200 and _rs.one("SELECT current_run FROM runners WHERE id=?",
                                  (rrid,))["current_run"] is None, st)

    # ---- server scale: the SSE fan-out is bounded ----
    _b = B.Bus(flush_s=3600)
    _qs = [_b.subscribe() for _ in range(B.Bus.MAX_SUBS)]
    c.check("concurrent SSE subscribers are capped", _b.subscribe() is None, "")
    _b.unsubscribe(_qs.pop())
    c.check("unsubscribing frees a stream slot", _b.subscribe() is not None, "")
    for _q in _qs:
        _b.unsubscribe(_q)

    _b2 = B.Bus(flush_s=3600)
    _q1, _q2 = _b2.subscribe(), _b2.subscribe()
    _b2.publish({"event": "runner", "data": {"id": "x"}})
    _f1, _f2 = _q1.get_nowait(), _q2.get_nowait()
    c.check("an event is serialised once and shared by every subscriber",
            _f1 is _f2 and _f1.startswith(b"event: runner"), (_f1, _f2))
    for _ in range(B.Bus.QUEUE_MAX + 1):
        _b2.publish({"event": "noise", "data": {"i": 1}})
    c.check("an overflowed subscriber is detached and woken to close",
            _q1.get_nowait() is B._SSE_OVERFLOW and _q1 not in _b2.subs, "")
    _b2.unsubscribe(_q2)

    # telemetry is coalesced to one event per run per tick, not one per batch
    _b3 = B.Bus(flush_s=3600)
    _q3 = _b3.subscribe()
    _b3.publish_telemetry("run-a", [{"elapsed": 1.0}], 1)
    _b3.publish_telemetry("run-a", [{"elapsed": 2.0}], 1)
    _b3.publish_telemetry("run-b", [{"elapsed": 1.0}], 1)
    _b3.flush_telemetry()
    _evs = []
    try:
        while True:
            _evs.append(_q3.get_nowait())
    except Exception:
        pass
    c.check("telemetry batches inside one tick are coalesced per run",
            len(_evs) == 2 and {e.split(b"\n")[0] for e in _evs} == {b"event: telemetry"},
            _evs)
    _tel_a = json.loads([e for e in _evs if b"run-a" in e][0]
                        .split(b"data: ", 1)[1].decode().strip())
    c.check("the coalesced event carries every sample since the last tick",
            len(_tel_a["samples"]) == 2 and _tel_a["n"] == 2
            and _tel_a["sample"]["elapsed"] == 2.0, _tel_a)
    _b3.unsubscribe(_q3)

    # a runner heartbeat sends the full record only when something changed
    _b4 = B.Bus(flush_s=3600)
    _q4 = _b4.subscribe()
    _rec = {"id": "r1", "name": "box", "capabilities": {"groups": ["gpu"]},
            "last_seen": 100.0, "online": True}
    _b4.publish_runner(dict(_rec))
    _b4.publish_runner(dict(_rec, last_seen=102.0))
    _full = json.loads(_q4.get_nowait().split(b"data: ", 1)[1].decode().strip())
    _delta = json.loads(_q4.get_nowait().split(b"data: ", 1)[1].decode().strip())
    c.check("the first heartbeat publishes the full runner record",
            _full.get("capabilities") == {"groups": ["gpu"]} and not _full.get("delta"), _full)
    c.check("an unchanged heartbeat publishes only the liveness delta",
            _delta.get("delta") and "capabilities" not in _delta
            and _delta.get("last_seen") == 102.0, _delta)
    _b4.unsubscribe(_q4)

    # ...and end to end: a stalled stream is closed so the client reconnects.
    # A raw socket with a tiny receive buffer and an overall deadline: the
    # handler cannot buffer its way out of the overflow, and the test can never
    # hang (a keepalive would otherwise keep resetting a socket timeout).
    _sk = socket.create_connection(("127.0.0.1", port), timeout=10)
    _sk.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    _sk.sendall(("GET /api/v1/stream?token=%s HTTP/1.0\r\nHost: localhost\r\n\r\n"
                 % TOKEN).encode())
    _buf = b""
    _deadline = time.time() + 10
    while b"event: snapshot" not in _buf and time.time() < _deadline:
        try:
            _buf += _sk.recv(4096)
        except socket.timeout:
            break
    c.check("a live stream opens", b"event: snapshot" in _buf, _buf[:60])
    # one frame larger than any socket buffer parks the handler in write();
    # the queue then fills and overflows
    B.Handler.bus.publish({"event": "noise", "data": {"pad": "x" * (4 * 1024 * 1024)}})
    for _ in range(B.Handler.bus.QUEUE_MAX + 2):
        B.Handler.bus.publish({"event": "noise", "data": {"pad": "x" * 8192}})
    _closed, _deadline = False, time.time() + 30
    while time.time() < _deadline:
        try:
            _chunk = _sk.recv(262144)
        except socket.timeout:
            continue
        except OSError:
            break
        if not _chunk:
            _closed = True
            break
    _sk.close()
    c.check("a stream that cannot keep up is closed, not silently starved", _closed, "")

    # ---- server scale: the caller is resolved once per request ----
    st, _regx = cl.call("POST", "/api/v1/register",
                        {"name": "scale", "os": "Linux", "kind": "agent"})
    _ridx, _keyx = _regx["runner_id"], _regx["api_key"]
    st, _rrx = cl.call("POST", "/api/v1/runs", {"runner_id": _ridx, "host": "lab-scale"},
                       token=_keyx)
    _runx = _rrx["run_id"]
    _rfb_calls = []
    _real_rfb = B.Handler._runner_from_bearer

    def _counting_rfb(self):
        _rfb_calls.append(1)
        return _real_rfb(self)

    B.Handler._runner_from_bearer = _counting_rfb
    try:
        st, _ = cl.call("POST", "/api/v1/runs/%s/telemetry" % _runx,
                        {"samples": [{"elapsed": 1}]}, token=_keyx)
    finally:
        B.Handler._runner_from_bearer = _real_rfb
    c.check("a runner write resolves the caller once, not per gate",
            st == 200 and len(_rfb_calls) == 1, (st, len(_rfb_calls)))

    # ---- server scale: the lookups have real indexes ----
    _plan = _rs.q("EXPLAIN QUERY PLAN SELECT * FROM runners WHERE api_key=?", ("x",))
    c.check("the api_key lookup uses its unique index",
            any("idx_runners_api_key" in (r.get("detail") or "") for r in _plan), _plan)
    _plan = _rs.q("EXPLAIN QUERY PLAN SELECT * FROM runners WHERE machine_id=? AND kind=? "
                  "AND COALESCE(instance,'')=?", ("m", "agent", ""))
    c.check("machine_id adoption uses its index",
            any("idx_runners_machine" in (r.get("detail") or "") for r in _plan), _plan)
    _plan = _rs.q("EXPLAIN QUERY PLAN SELECT * FROM runs WHERE host=?", ("h",))
    c.check("the runs host filter uses its index",
            any("idx_runs_host" in (r.get("detail") or "") for r in _plan), _plan)

    # a legacy database (no api_key/machine_id columns, duplicate keys) migrates
    import sqlite3 as _sqlite3
    _legacy = os.path.join(tmpdir(), "legacy.sqlite")
    _con = _sqlite3.connect(_legacy)
    _con.execute("CREATE TABLE runners (id TEXT PRIMARY KEY, name TEXT, api_key TEXT)")
    _con.execute("INSERT INTO runners VALUES('a','one','dupkey')")
    _con.execute("INSERT INTO runners VALUES('b','two','dupkey')")
    _con.execute("INSERT INTO runners VALUES('c','three','')")
    _con.commit()
    _con.close()
    _legacy_store = B.Store(_legacy)
    _keys = {r["id"]: r["api_key"] for r in _legacy_store.q("SELECT id, api_key FROM runners")}
    c.check("a legacy database with duplicate api_keys migrates to the unique index",
            sum(1 for k in _keys.values() if k == "dupkey") == 1, _keys)
    c.check("a blank api_key is cleared, not left to collide", _keys.get("c") is None, _keys)
    _legacy_store.conn.close()

    # ---- server scale: the commit compare never blocks a request ----
    _t0 = time.time()
    _cmp = B.compare_detail("cmpaaa1", "cmpbbb2")
    _dt = time.time() - _t0
    c.check("a compare cache miss returns immediately",
            _cmp == ("", []) and _dt < 0.5, (_cmp, _dt))
    _deadline = time.time() + 5
    while time.time() < _deadline and ("cmpaaa1", "cmpbbb2") not in B._CMP:
        time.sleep(0.05)
    c.check("the compare is refreshed in the background",
            ("cmpaaa1", "cmpbbb2") in B._CMP, list(B._CMP))

    srv.stop()
    return c.fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
