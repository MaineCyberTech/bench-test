"""Data-lifecycle + disaster-recovery tests for benchd.

Covers: deleting a run removes every dependent row (no orphans readable by run
id); the bounded retention/prune policy; and the consistent SQLite backup helper
(plus its rotation). Stdlib only; reuses the shared harness.
"""
import os
import sqlite3

from harness import Checker, Server, tmpdir

import backup_benchd


def _count(S, table, run_id):
    """Rows in `table` for this run (the runs table is keyed by id)."""
    if table == "runs":
        return S.one("SELECT COUNT(*) c FROM runs WHERE id=?", (run_id,))["c"]
    return S.one("SELECT COUNT(*) c FROM %s WHERE run_id=?" % table, (run_id,))["c"]


def run():
    c = Checker("lifecycle")
    srv = Server()
    cl = srv.client()
    B = srv.benchd
    S = B.Handler.store

    # ---- DATA-P1-001 / SEARCH-P1-001: delete clears every dependent row ----
    st, reg = cl.call("POST", "/api/v1/register", {"name": "life", "kind": "agent"})
    rid, key = reg["runner_id"], reg["api_key"]
    st, rr = cl.call("POST", "/api/v1/runs", {"runner_id": rid, "host": "life"}, token=key)
    run_id = rr["run_id"]
    cl.call("POST", "/api/v1/runs/%s/phase" % run_id, {"seq": 1, "label": "p1"}, token=key)
    cl.call("POST", "/api/v1/runs/%s/telemetry" % run_id,
            {"samples": [{"elapsed": 1, "temp_c": 40}, {"elapsed": 2, "temp_c": 41}]}, token=key)
    cl.call("POST", "/api/v1/runs/%s/log" % run_id, {"lines": ["one", "two"]}, token=key)
    cl.call("POST", "/api/v1/runs/%s/event" % run_id, {"level": "warn", "message": "e"}, token=key)
    cl.call("POST", "/api/v1/runs/%s/artifact" % run_id,
            {"name": "report.json", "content": "e30="}, token=key)
    # a queued command can also carry the run id
    S.ex("INSERT INTO commands(id,runner_id,run_id,kind,payload,status,created) "
         "VALUES(?,?,?,?,?,?,?)", ("cmd-life", rid, run_id, "stop", "{}", "queued", B.now()))

    tables = ("phases", "telemetry", "logs", "events", "artifacts", "commands")
    c.check("run exists before delete", _count(S, "runs", run_id) == 1, "")
    for t in tables:
        c.check("row present in %s before delete" % t, _count(S, t, run_id) >= 1,
                _count(S, t, run_id))

    st, _ = cl.call("DELETE", "/api/v1/runs/" + run_id)
    c.check("delete run returns ok", st == 200, st)
    c.check("run row gone", _count(S, "runs", run_id) == 0, "")
    for t in tables:
        c.check("no orphaned %s rows" % t, _count(S, t, run_id) == 0, _count(S, t, run_id))
    st, tel = cl.call("GET", "/api/v1/runs/%s/telemetry" % run_id)
    c.check("deleted run's telemetry is not retrievable", st == 200 and tel.get("rows") == [], tel)
    c.check("deleted run is 404", cl.call("GET", "/api/v1/runs/%s" % run_id)[0] == 404, "")

    # (Retention is upstream's own policy and is covered by test_control_plane.)

    # ---- PERF-P1-001: capacity signal in /health ----
    st, h = cl.call("GET", "/health", token=None)
    cap_sig = h.get("capacity") or {}
    c.check("health exposes per-table row counts",
            {"runs", "telemetry", "logs", "events", "artifacts"} <= set(cap_sig.get("rows", {})),
            cap_sig)
    c.check("health exposes db_bytes",
            isinstance(cap_sig.get("db_bytes"), int) and cap_sig["db_bytes"] > 0,
            cap_sig.get("db_bytes"))

    # ---- DR-P1-001 / DR-P1-002: consistent backup + rotation ----
    out = tmpdir("benchd-backups-")
    dest = backup_benchd.backup(srv.db, out, keep=2)
    c.check("backup file exists", os.path.isfile(dest), dest)
    copy = sqlite3.connect(dest)
    try:
        names = {r[0] for r in copy.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        c.check("backup carries the schema",
                {"runs", "telemetry", "logs", "events", "artifacts", "runners"} <= names, names)
        c.check("backup carries the data",
                copy.execute("SELECT COUNT(*) FROM runners").fetchone()[0] >= 1, "")
        c.check("backup passes integrity_check",
                copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "")
    finally:
        copy.close()
    for i in range(3):
        backup_benchd.backup(srv.db, out, keep=2, when=1000000000 + i * 100000)
    snaps = [n for n in os.listdir(out) if backup_benchd.NAME_RE.match(n)]
    c.check("rotation keeps only the newest snapshots", len(snaps) == 2, snaps)
    try:
        backup_benchd.backup(os.path.join(out, "does-not-exist.sqlite"), out, keep=2)
        missing_raised = False
    except FileNotFoundError:
        missing_raised = True
    c.check("backup fails closed on a missing database", missing_raised, "")

    srv.stop()
    return c.fails
