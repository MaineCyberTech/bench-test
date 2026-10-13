"""Wave W13 data/schema integrity tests.

Covers: the PRAGMA user_version schema marker and its startup check
(DATA-P2-003 / DR-P2-002 / EVOL-P2-003), foreign-key enforcement and cascade
(DATA-P2-002), idempotent phase writes (DATA-P2-004) and hashed per-runner
API keys (PRIV-P2-005). Stdlib only, reuse the shared harness.
"""
import io
import os
import sqlite3
import sys
from contextlib import redirect_stderr

from harness import Checker, Server, tmpdir  # noqa: E402 (sets sys.path)

import benchd  # noqa: E402


def run():
    c = Checker("schema")

    # ---- DATA-P2-003 / DR-P2-002 / EVOL-P2-003: schema version ----
    db = os.path.join(tmpdir(), "v.sqlite")
    st = benchd.Store(db)
    ver = st.conn.execute("PRAGMA user_version").fetchone()[0]
    c.check("a fresh database records the schema version",
            ver == benchd.SCHEMA_VERSION, (ver, benchd.SCHEMA_VERSION))

    # a newer database is refused, with a clear message...
    st.conn.execute("PRAGMA user_version=%d" % (benchd.SCHEMA_VERSION + 1))
    st.conn.commit()
    st.conn.close()
    raised = None
    try:
        benchd.Store(db)
    except benchd.SchemaVersionError as exc:
        raised = exc
    c.check("a database written by a newer build is refused", raised is not None, raised)
    c.check("the refusal names both versions",
            raised is not None and str(benchd.SCHEMA_VERSION + 1) in str(raised)
            and str(benchd.SCHEMA_VERSION) in str(raised), raised)

    # ...and main() exits non-zero instead of starting the server
    argv = sys.argv
    sys.argv = ["benchd", "--db", db, "--allow-anonymous", "--port", "0"]
    try:
        rc = benchd.main()
    finally:
        sys.argv = argv
    c.check("main() refuses to start on a newer database (non-zero exit)", rc == 3, rc)

    # an older database is upgraded in place, and plaintext keys are hashed
    old = os.path.join(tmpdir(), "old.sqlite")
    conn = sqlite3.connect(old)
    conn.executescript("CREATE TABLE runners (id TEXT PRIMARY KEY, api_key TEXT);")
    conn.execute("INSERT INTO runners(id,api_key) VALUES('r1','legacy-plaintext')")
    conn.execute("PRAGMA user_version=0")
    conn.commit()
    conn.close()
    st2 = benchd.Store(old)
    c.check("an older database is upgraded in place",
            st2.conn.execute("PRAGMA user_version").fetchone()[0] == benchd.SCHEMA_VERSION, "")
    c.check("a stored plaintext api_key is migrated to its digest",
            st2.one("SELECT api_key FROM runners WHERE id='r1'")["api_key"]
            == benchd.key_digest("legacy-plaintext"), "")
    st2.conn.close()

    # ---- DATA-P2-002: foreign keys are enforced, and cascade ----
    fdb = os.path.join(tmpdir(), "fk.sqlite")
    fs = benchd.Store(fdb)
    c.check("foreign keys are enforced on the connection",
            fs.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1, "")
    fs.ex("INSERT INTO runs(id,runner_id,host,status,started) "
          "VALUES('run1','r1','h','running',1)")
    fs.ex("INSERT INTO phases(run_id,seq,label) VALUES('run1',1,'p')")
    fs.ex("INSERT INTO telemetry(run_id,ts) VALUES('run1',1)")
    fs.ex("INSERT INTO logs(run_id,ts,line) VALUES('run1',1,'x')")
    fs.ex("INSERT INTO events(run_id,ts,message) VALUES('run1',1,'e')")
    fs.ex("INSERT INTO artifacts(run_id,name) VALUES('run1','a')")
    fs.ex("INSERT INTO commands(id,run_id,kind) VALUES('c1','run1','stop')")
    rejected = False
    try:
        fs.ex("INSERT INTO phases(run_id,seq,label) VALUES('no-such-run',1,'p')")
    except sqlite3.IntegrityError:
        rejected = True
    c.check("a dependent row cannot reference a missing run", rejected, "")
    # delete the run row directly: the ON DELETE CASCADE must clear dependents
    fs.ex("DELETE FROM runs WHERE id='run1'")
    for table in ("phases", "telemetry", "logs", "events", "artifacts", "commands"):
        c.check("FK cascade cleared %s" % table,
                fs.one("SELECT COUNT(*) c FROM %s WHERE run_id='run1'" % table)["c"] == 0, "")
    fs.conn.close()

    # a legacy database that already holds duplicate (run_id, seq) rows must still
    # start: the unique index cannot be built, which is a warning, not a crash
    dup = os.path.join(tmpdir(), "dup.sqlite")
    d0 = benchd.Store(dup)
    d0.ex("DROP INDEX IF EXISTS idx_phases_run_seq")
    d0.ex("INSERT INTO runs(id,host,status,started) VALUES('r','h','running',1)")
    d0.ex("INSERT INTO phases(run_id,seq,label) VALUES('r',1,'a')")
    d0.ex("INSERT INTO phases(run_id,seq,label) VALUES('r',1,'b')")
    d0.conn.close()
    with redirect_stderr(io.StringIO()):
        ds = benchd.Store(dup)
    c.check("a legacy database with duplicate phases still starts", ds is not None, "")
    c.check("no unique phase index is built while duplicates exist",
            ds.one("SELECT name FROM sqlite_master WHERE type='index' "
                   "AND name='idx_phases_run_seq'") is None, "")
    ds.conn.close()

    # ---- PRIV-P2-005 + DATA-P2-004 over the real API ----
    srv = Server()
    cl = srv.client()
    S = srv.benchd.Handler.store

    st, reg = cl.call("POST", "/api/v1/register", {"name": "w13", "kind": "agent"})
    rid, key = reg.get("runner_id"), reg.get("api_key")
    c.check("register still issues a key", st == 200 and rid and key, reg)
    stored = S.one("SELECT api_key FROM runners WHERE id=?", (rid,))["api_key"]
    c.check("the stored api_key is a sha256 digest, never the plaintext",
            stored == benchd.key_digest(key) and stored != key
            and stored == stored.lower() and len(stored) == 64, stored)
    st, _ = cl.call("POST", "/api/v1/runners/%s/heartbeat" % rid,
                    {"status": "idle"}, token=key)
    c.check("the runner key still authenticates", st == 200, st)

    st, reg2 = cl.call("POST", "/api/v1/register",
                       {"runner_id": rid, "api_key": key, "name": "w13"})
    c.check("re-register with the old key succeeds and keeps it",
            st == 200 and reg2.get("runner_id") == rid and reg2.get("api_key") == key, reg2)
    st, reg3 = cl.call("POST", "/api/v1/register",
                       {"runner_id": rid, "name": "w13"}, token=key)
    c.check("ownership can also be proved by the bearer",
            st == 200 and reg3.get("api_key") == key, reg3)
    st, _ = cl.call("POST", "/api/v1/runners/%s/heartbeat" % rid,
                    {"status": "idle"}, token=key)
    c.check("the key still works after re-register", st == 200, st)

    # phase replay: the unique index + INSERT OR REPLACE keep one row
    st, run = cl.call("POST", "/api/v1/runs", {"runner_id": rid, "host": "w13"}, token=key)
    rn = run.get("run_id")
    c.check("run created for the phase test", st == 200 and rn, run)
    cl.call("POST", "/api/v1/runs/%s/phase" % rn,
            {"seq": 1, "group": "g", "label": "x", "status": "running"}, token=key)
    cl.call("POST", "/api/v1/runs/%s/phase" % rn,
            {"seq": 1, "group": "g", "label": "x", "status": "done", "t1": 5.0}, token=key)
    c.check("a replayed phase insert does not duplicate its row",
            S.one("SELECT COUNT(*) c FROM phases WHERE run_id=? AND seq=1", (rn,))["c"] == 1, "")
    cl.call("POST", "/api/v1/runs/%s/phase" % rn,
            {"seq": 2, "label": "y", "status": "running", "t0": 123.0}, token=key)
    cl.call("POST", "/api/v1/runs/%s/phase" % rn,
            {"seq": 2, "label": "y", "status": "done"}, token=key)
    row = S.one("SELECT t0,status FROM phases WHERE run_id=? AND seq=2", (rn,))
    c.check("a replayed phase keeps its original t0 and takes the new status",
            row["t0"] == 123.0 and row["status"] == "done", row)
    srv.stop()

    return c.fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
