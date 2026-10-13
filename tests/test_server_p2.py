"""Wave W24a control-plane P2 tests.

Covers the P2 backlog that had no server-side enforcement:

  * ADMIN-P2-002 - a destructive command (shutdown / reboot / iso_write /
    iso_write_slot) is refused with 400 unless its payload carries
    ``confirm: true``; a confirmed one is accepted.
  * ADMIN-P2-004 / ADMIN-P2-006 - ``exec`` / ``shell`` can be constrained with an
    operator allow/deny list; a denial is a 403 and is audited, and the default
    (no lists) still allows everything.
  * ACM-P2-001 / ACM-P2-002 - ``POST /runners/<id>/rotate`` mints a new api_key
    (the old one stops working at once); an optional ``--key-ttl-days`` makes an
    expired key a 401 on heartbeat / command poll.
  * FILE-P2-002 - artifact upload enforces a per-artifact size cap, a per-run
    quota, and rejects an empty / path-traversal / absolute name.

Stdlib only, reuse the shared harness.
"""
import base64
import re
import sys
import time

from harness import Checker, Server, TOKEN


def _upload(cl, run_id, key, name, blob=b"payload"):
    b64 = base64.b64encode(blob).decode()
    body = {"name": name, "kind": "bin", "content": b64}
    return cl.call("POST", "/api/v1/runs/%s/artifact" % run_id, body, token=key)


def run():
    c = Checker("server_p2")
    srv = Server()
    cl = srv.client()
    B = srv.benchd
    S = B.Handler.store

    # remember the process-global controls so a later module is unaffected
    saved = (B.Handler.exec_allowlist, B.Handler.exec_denylist,
             B.Handler.key_ttl_days, B.Handler.artifact_max_bytes,
             B.Handler.artifact_quota)
    try:
        # ---- a runner + run to drive the command/artifact paths ----
        st, reg = cl.call("POST", "/api/v1/register", {"name": "p2", "kind": "agent"})
        rid, key = reg.get("runner_id"), reg.get("api_key")
        c.check("registered a runner", st == 200 and rid and key, reg)
        st, rr = cl.call("POST", "/api/v1/runs", {"runner_id": rid, "host": "p2"}, token=key)
        run_id = rr.get("run_id")
        c.check("opened a run", st == 200 and run_id, rr)

        # ---- ADMIN-P2-002: destructive commands need payload.confirm === true ----
        for kind in ("shutdown", "reboot", "iso_write", "iso_write_slot"):
            st, body = cl.call("POST", "/api/v1/runners/%s/commands" % rid, {"kind": kind})
            c.check("%s without confirm -> 400" % kind, st == 400, (st, body))
            st, body = cl.call("POST", "/api/v1/runners/%s/commands" % rid,
                               {"kind": kind, "payload": {}})
            c.check("%s with an empty payload -> 400" % kind, st == 400, (st, body))
            st, body = cl.call("POST", "/api/v1/runners/%s/commands" % rid,
                               {"kind": kind, "payload": {"confirm": False}})
            c.check("%s with confirm=false -> 400" % kind, st == 400, (st, body))
            st, body = cl.call("POST", "/api/v1/runners/%s/commands" % rid,
                               {"kind": kind, "payload": {"confirm": True}})
            c.check("%s with confirm=true -> 200" % kind,
                    st == 200 and body.get("id"), (st, body))
        # the gate is scoped to destructive kinds
        st, body = cl.call("POST", "/api/v1/runners/%s/commands" % rid, {"kind": "stop"})
        c.check("a non-destructive command needs no confirm",
                st == 200 and body.get("id"), (st, body))
        denied = S.q("SELECT detail FROM audit WHERE action='command_denied'")
        c.check("an unconfirmed destructive command is audited",
                any("payload.confirm" in (d["detail"] or "") for d in denied), denied)

        # ---- ADMIN-P2-004 / ADMIN-P2-006: exec allow / deny lists ----
        st, body = cl.call("POST", "/api/v1/runners/%s/commands" % rid,
                           {"kind": "exec", "payload": {"cmd": "echo hi"}})
        c.check("exec is allowed by default", st == 200 and body.get("id"), (st, body))

        B.Handler.exec_denylist = re.compile(r"rm\s+-rf")
        st, body = cl.call("POST", "/api/v1/runners/%s/commands" % rid,
                           {"kind": "exec", "payload": {"cmd": "rm -rf /"}})
        c.check("a denylisted exec is refused with 403", st == 403, (st, body))
        st, body = cl.call("POST", "/api/v1/runners/%s/commands" % rid,
                           {"kind": "exec", "payload": {"cmd": "echo ok"}})
        c.check("a non-denylisted exec is still accepted", st == 200, (st, body))

        B.Handler.exec_allowlist = re.compile(r"^nvidia-smi\b")
        st, body = cl.call("POST", "/api/v1/runners/%s/commands" % rid,
                           {"kind": "exec", "payload": {"cmd": "nvidia-smi -q"}})
        c.check("an allowlisted exec is accepted", st == 200, (st, body))
        st, body = cl.call("POST", "/api/v1/runners/%s/commands" % rid,
                           {"kind": "exec", "payload": {"cmd": "cat /etc/shadow"}})
        c.check("an exec outside the allowlist is refused with 403", st == 403, (st, body))
        st, body = cl.call("POST", "/api/v1/runners/%s/commands" % rid,
                           {"kind": "shell", "payload": {"cmd": "cat /etc/shadow"}})
        c.check("shell is gated by the same allowlist", st == 403, (st, body))
        denied = S.q("SELECT detail FROM audit WHERE action='command_denied'")
        c.check("an exec/shell denial is audited",
                any("denylist" in (d["detail"] or "") or "allowlist" in (d["detail"] or "")
                    for d in denied), denied)
        B.Handler.exec_allowlist = None
        B.Handler.exec_denylist = None

        # ---- ACM-P2-001 / ACM-P2-002: key rotation ----
        st, rot = cl.call("POST", "/api/v1/runners/%s/rotate" % rid)
        new_key = rot.get("api_key")
        c.check("the operator can rotate a runner's key",
                st == 200 and new_key and new_key != key, (st, rot))
        c.check("the stored key is the new digest",
                S.one("SELECT api_key FROM runners WHERE id=?", (rid,))["api_key"]
                == B.key_digest(new_key), "")
        st, _ = cl.call("POST", "/api/v1/runners/%s/heartbeat" % rid,
                        {"status": "idle"}, token=key)
        c.check("the old key is invalid immediately after rotation", st == 401, st)
        st, _ = cl.call("POST", "/api/v1/runners/%s/heartbeat" % rid,
                        {"status": "idle"}, token=new_key)
        c.check("the new key authenticates", st == 200, st)
        st, rot2 = cl.call("POST", "/api/v1/runners/%s/rotate" % rid, token=new_key)
        c.check("a runner may rotate its own key",
                st == 200 and rot2.get("api_key") and rot2.get("api_key") != new_key,
                (st, rot2))
        key = rot2["api_key"]

        st, other = cl.call("POST", "/api/v1/register", {"name": "p2-stranger", "kind": "agent"})
        stranger_key = other.get("api_key")
        st, body = cl.call("POST", "/api/v1/runners/%s/rotate" % rid, token=stranger_key)
        c.check("a stranger cannot rotate another runner's key", st == 403, (st, body))
        c.check("rotation is audited",
                bool(S.one("SELECT id FROM audit WHERE action='key_rotate' AND target=?",
                           (rid,))), "")

        # ---- ACM-P2-001 / ACM-P2-002: key TTL expiry ----
        st, reg3 = cl.call("POST", "/api/v1/register", {"name": "p2-ttl", "kind": "agent"})
        trid, tkey = reg3.get("runner_id"), reg3.get("api_key")
        B.Handler.key_ttl_days = 1
        S.ex("UPDATE runners SET key_issued=? WHERE id=?", (time.time() - 2 * 86400, trid))
        st, body = cl.call("POST", "/api/v1/runners/%s/heartbeat" % trid,
                           {"status": "idle"}, token=tkey)
        c.check("an expired key is refused on heartbeat with 401", st == 401, (st, body))
        st, body = cl.call("GET", "/api/v1/runners/%s/commands" % trid, token=tkey)
        c.check("an expired key is refused on the command poll with 401", st == 401,
                (st, body))
        c.check("the expiry error is explicit",
                "expired" in str(body.get("error", "")).lower(), body)
        st, rtr = cl.call("POST", "/api/v1/runners/%s/rotate" % trid, token=tkey)
        c.check("an expired key can still be rotated", st == 200 and rtr.get("api_key"),
                (st, rtr))
        st, _ = cl.call("POST", "/api/v1/runners/%s/heartbeat" % trid,
                        {"status": "idle"}, token=rtr["api_key"])
        c.check("the rotated key works again after expiry", st == 200, st)
        B.Handler.key_ttl_days = 0

        # ---- FILE-P2-002: artifact size cap, quota and name validation ----
        st, rr2 = cl.call("POST", "/api/v1/runs", {"runner_id": rid, "host": "p2"}, token=key)
        arun = rr2["run_id"]
        B.Handler.artifact_max_bytes = 64
        B.Handler.artifact_quota = 2
        st, body = _upload(cl, arun, key, "big.bin", blob=b"x" * 100)
        c.check("an oversized artifact -> 413", st == 413, (st, body))
        st, body = _upload(cl, arun, key, "ok1.bin", blob=b"x" * 8)
        c.check("a small artifact is accepted", st == 200, (st, body))
        st, body = _upload(cl, arun, key, "ok2.bin", blob=b"x" * 8)
        c.check("the second artifact is accepted", st == 200, (st, body))
        st, body = _upload(cl, arun, key, "over.bin", blob=b"x" * 8)
        c.check("an artifact over the per-run quota -> 413", st == 413, (st, body))

        for bad in ("", "..", "../evil", "a/b", "sub\\x", "/etc/passwd"):
            body = {"name": bad, "kind": "bin",
                    "content": base64.b64encode(b"z").decode()}
            st, resp = cl.call("POST", "/api/v1/runs/%s/artifact" % arun, body, token=key)
            c.check("a bad artifact name (%r) -> 400" % bad, st == 400, (st, resp))
        body = {"kind": "bin", "content": base64.b64encode(b"z").decode()}
        st, resp = cl.call("POST", "/api/v1/runs/%s/artifact" % arun, body, token=key)
        c.check("a missing artifact name -> 400", st == 400, (st, resp))
        B.Handler.artifact_max_bytes = B.DEFAULT_ARTIFACT_MAX_BYTES
        B.Handler.artifact_quota = B.DEFAULT_ARTIFACT_QUOTA

        srv.stop()
    finally:
        (B.Handler.exec_allowlist, B.Handler.exec_denylist,
         B.Handler.key_ttl_days, B.Handler.artifact_max_bytes,
         B.Handler.artifact_quota) = saved

    return c.fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
