"""Control-plane (benchd) API tests: auth, runners, runs, telemetry, logs,
artifacts, stats, commands, schedules, delete, reaper, SSE."""
import importlib.util
import json
import os
import sys
import time
import urllib.request

from harness import Checker, Server, TOKEN, REPO, BENCH


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
    c.check("retention sweeps all three per-sample tables per run",
            n == 30 and len([s for s, _a in _seen if s.startswith("UPDATE")]) == 2 and
            len([s for s, _a in _seen if s.startswith("DELETE")]) == 6,
            (n, len(_seen)))
    c.check("retention stores a run's stats before deleting its samples",
            [s.split()[0] for s, _a in _seen if s.startswith(("UPDATE", "DELETE"))] ==
            ["UPDATE", "DELETE", "DELETE", "DELETE",
             "UPDATE", "DELETE", "DELETE", "DELETE"],
            [s[:24] for s, _a in _seen if s.startswith(("UPDATE", "DELETE"))])
    c.check("retention computes the stats from the run's own samples",
            len([s for s, _a in _seen if s.startswith("SELECT * FROM telemetry")]) == 2, "")
    c.check("retention deletes per run, not one sweep of everything",
            all("WHERE run_id=?" in s and "IN (SELECT" not in s
                for s, _a in _seen if s.startswith("DELETE")), "")
    c.check("retention covers every old run it selected",
            {a[0] for s, a in _seen if s.startswith("DELETE")} ==
            {"run-old-1", "run-old-2"} and
            {a[1] for s, a in _seen if s.startswith("UPDATE")} ==
            {"run-old-1", "run-old-2"}, "")
    before = len(_seen)
    c.check("retention is throttled between sweeps",
            _bdm.retain_old_data(_FakeStore()) == 0 and len(_seen) == before, "")
    c.check("a forced sweep runs regardless of the throttle",
            _bdm.retain_old_data(_FakeStore(), force=True) == 30, "")
    _bdm.RETENTION_DAYS = 0.0
    _bdm._RETENTION_LAST[0] = 0.0
    settled = len(_seen)
    c.check("retention disabled keeps everything",
            _bdm.retain_old_data(_FakeStore()) == 0 and len(_seen) == settled, "")

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

    srv.stop()
    return c.fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
