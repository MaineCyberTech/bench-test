"""Wave W22a control-plane P3 leftovers.

Covers: FILE-P3-002 - one attached artifact can be deleted by its run's owner
(the runner api_key) or the operator, 404 when absent, and the deletion is
audited; artifacts of finished runs are pruned by the same retention sweep and
window as telemetry/logs/events; OBS-P3-003 / CI-P3-001 - the control plane's
version resolves from a shipped VERSION / .bench-version stamp when git is
unavailable, so a copy-deploy does not report 0.0.0+unknown and a release tag can
be tied to a build.

Stdlib only, reuse the shared harness.
"""
import base64
import json
import os
import sys
import tempfile
import time

from harness import Checker, Server, TOKEN


def _upload(cl, run_id, key, name, blob=b"payload"):
    """Attach an artifact to a run with the runner's api_key."""
    b64 = base64.b64encode(blob).decode()
    return cl.call("POST", "/api/v1/runs/%s/artifact" % run_id,
                   {"name": name, "kind": "bin", "content": b64}, token=key)


def run():
    c = Checker("server_left")
    srv = Server()
    cl = srv.client()
    B = srv.benchd
    S = B.Handler.store

    # ---- FILE-P3-002: per-artifact delete (owner / operator / stranger / 404) ----
    st, reg = cl.call("POST", "/api/v1/register", {"name": "owner", "kind": "agent"})
    owner_id, owner_key = reg.get("runner_id"), reg.get("api_key")
    c.check("registered the owning runner", st == 200 and owner_id and owner_key, reg)
    st, rr = cl.call("POST", "/api/v1/runs", {"runner_id": owner_id, "host": "left"},
                     token=owner_key)
    run_id = rr.get("run_id")
    c.check("opened a run", st == 200 and run_id, rr)

    st, reg2 = cl.call("POST", "/api/v1/register", {"name": "stranger", "kind": "agent"})
    stranger_key = reg2.get("api_key")
    c.check("registered a second runner", st == 200 and stranger_key, reg2)

    st, body = _upload(cl, run_id, owner_key, "keep.bin")
    c.check("the owner attached an artifact", st == 200, body)

    # a stranger runner may not delete it
    st, body = cl.call("DELETE", "/api/v1/runs/%s/artifact/keep.bin" % run_id,
                       token=stranger_key)
    c.check("a stranger cannot delete the artifact", st in (401, 403), (st, body))
    c.check("the stranger's attempt left the artifact in place",
            bool(S.one("SELECT id FROM artifacts WHERE run_id=? AND name='keep.bin'",
                       (run_id,))), "")

    # an absent artifact (and an absent run) is a 404
    st, body = cl.call("DELETE", "/api/v1/runs/%s/artifact/nope.bin" % run_id,
                       token=owner_key)
    c.check("deleting an absent artifact -> 404", st == 404, (st, body))
    st, body = cl.call("DELETE", "/api/v1/runs/no-such-run/artifact/keep.bin",
                       token=owner_key)
    c.check("deleting on an absent run -> 404", st == 404, (st, body))

    # the owner deletes its own run's artifact
    st, body = cl.call("DELETE", "/api/v1/runs/%s/artifact/keep.bin" % run_id,
                       token=owner_key)
    c.check("the owner can delete the artifact", st == 200, (st, body))
    c.check("the artifact row is gone",
            not S.one("SELECT id FROM artifacts WHERE run_id=? AND name='keep.bin'",
                      (run_id,)), "")
    st, body = cl.call("GET", "/api/v1/runs/%s/artifact/keep.bin?token=%s" % (run_id, TOKEN),
                       raw=True)
    c.check("the deleted artifact no longer downloads", st == 404, st)

    # the operator deletes any artifact
    _upload(cl, run_id, owner_key, "op.bin")
    st, body = cl.call("DELETE", "/api/v1/runs/%s/artifact/op.bin" % run_id, token=TOKEN)
    c.check("the operator can delete any artifact", st == 200, (st, body))

    aud = S.q("SELECT action,target,detail FROM audit WHERE action='artifact_delete'")
    c.check("artifact deletion is audited",
            any(a["target"] == "keep.bin" and a["detail"] == run_id for a in aud), aud)

    # ---- FILE-P3-002: retention prunes artifacts of finished runs ----
    old_run = "old-run-1"
    S.ex("INSERT INTO runs(id,runner_id,host,started,ended,status) VALUES(?,?,?,?,?,?)",
         (old_run, owner_id, "left", time.time() - 40 * 86400,
          time.time() - 40 * 86400, "done"))
    S.ex("INSERT INTO artifacts(run_id,name,kind,size,sha256,content) VALUES(?,?,?,?,?,?)",
         (old_run, "old.bin", "bin", 3, "abc", base64.b64encode(b"old").decode()))
    saved_days = B.RETENTION_DAYS
    B.RETENTION_DAYS = 30.0
    try:
        n = B.prune_old_artifacts(S, force=True)
    finally:
        B.RETENTION_DAYS = saved_days
    c.check("retention prunes the artifact of a finished old run", n >= 1, n)
    c.check("the pruned artifact is gone",
            not S.one("SELECT id FROM artifacts WHERE run_id=?", (old_run,)), "")
    c.check("the run row itself is retained",
            bool(S.one("SELECT id FROM runs WHERE id=?", (old_run,))), "")
    # a running run's artifacts are never pruned
    _upload(cl, run_id, owner_key, "fresh.bin")
    saved_days = B.RETENTION_DAYS
    B.RETENTION_DAYS = 30.0
    try:
        B.prune_old_artifacts(S, force=True)
    finally:
        B.RETENTION_DAYS = saved_days
    c.check("a running run's artifact is not pruned",
            bool(S.one("SELECT id FROM artifacts WHERE run_id=? AND name='fresh.bin'",
                       (run_id,))), "")
    # retention disabled keeps everything
    saved_days = B.RETENTION_DAYS
    B.RETENTION_DAYS = 0.0
    try:
        kept = B.prune_old_artifacts(S, force=True)
    finally:
        B.RETENTION_DAYS = saved_days
    c.check("retention disabled keeps artifacts", kept == 0, kept)

    # ---- OBS-P3-003 / CI-P3-001: version resolves from the shipped stamp ----
    with tempfile.TemporaryDirectory() as td:
        with open(os.path.join(td, ".bench-version"), "w", encoding="utf-8") as fh:
            json.dump({"mode": "copy", "commit": "abc1234", "ref": "main",
                       "version": "2.3.4"}, fh)
        c.check("the version resolves from the .bench-version stamp when VERSION is absent",
                B.server_version(td) == "2.3.4", B.server_version(td))
        with open(os.path.join(td, "VERSION"), "w", encoding="utf-8") as fh:
            fh.write("9.9.9\n")
        c.check("a shipped VERSION is preferred over the stamp",
                B.server_version(td) == "9.9.9", B.server_version(td))
    with tempfile.TemporaryDirectory() as td:
        with open(os.path.join(td, ".bench-version"), "w", encoding="utf-8") as fh:
            json.dump({"mode": "copy", "version": "0.0.0+unknown"}, fh)
        c.check("a sentinel in the stamp is not echoed as a release",
                B.server_version(td) == "0.0.0+unknown", B.server_version(td))
    with tempfile.TemporaryDirectory() as td:
        c.check("no VERSION and no stamp -> 0.0.0+unknown, never invented",
                B.server_version(td) == "0.0.0+unknown", B.server_version(td))

    # the deployed copy's commit comes from the same stamp, so /health and /state
    # tie a release tag to a build and say where the version came from
    _, health = cl.call("GET", "/health")
    c.check("health exposes version + git for the deployed copy",
            "version" in health and "git" in health, sorted(health))
    c.check("health says where the version came from",
            health.get("version_source") in ("VERSION", ".bench-version", "none"),
            health.get("version_source"))
    _, state = cl.call("GET", "/api/v1/state")
    c.check("state exposes version + server_git",
            "version" in state and "server_git" in state, sorted(state))
    c.check("state says where the version came from",
            state.get("version_source") in ("VERSION", ".bench-version", "none"),
            state.get("version_source"))

    srv.stop()
    return c.fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
