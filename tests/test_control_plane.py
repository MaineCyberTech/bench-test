"""Control-plane (benchd) API tests: auth, runners, runs, telemetry, logs,
artifacts, stats, commands, schedules, delete, reaper, SSE."""
import json
import sys
import time
import urllib.request

from harness import Checker, Server, TOKEN


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

    # ---- delete ----
    st, _ = cl.call("DELETE", "/api/v1/runs/" + run_id)
    c.check("delete run", cl.call("GET", "/api/v1/runs/%s" % run_id)[0] == 404, "")
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

    srv.stop()
    return c.fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
