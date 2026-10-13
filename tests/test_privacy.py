"""Privacy tests (PRIV-P2-001 / AN-P2-001..003).

Covers the three fixes in the privacy P2 wave:
  * opt-in redaction pseudonymises stable identifiers/PII deterministically and
    leaves non-PII intact (`bench/redact.py`, wired through benchreport);
  * GitHub uploads require explicit consent (`bench/upload.py`);
  * the control plane can stop storing the client IP and anonymises stale
    runner rows (`server/benchd.py`).

Stdlib only, like the rest of the suite.
"""
import importlib.util
import json
import os
import sys

from harness import Checker, Server, tmpdir, REPO
import redact
import upload as up


def _redaction(c):
    saved = os.environ.get("BENCH_REDACT")
    try:
        os.environ.pop("BENCH_REDACT", None)
        c.check("redaction is off by default", redact.is_enabled() is False, "")
        obj = {"hostname": "DESKTOP-ABC", "cpu": "AMD Ryzen 9 5950X", "threads": 32,
               "ram_gib": 64.0, "serial": "SN-12345",
               "nics": [{"name": "eth0", "mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.1.5"}],
               "home": "C:\\Users\\alice",
               "log": "C:\\Users\\alice\\bench.log"}
        c.check("redaction off is a pass-through",
                redact.redact_obj(obj) is obj and redact.redact_text("x") == "x", "")

        os.environ["BENCH_REDACT"] = "1"
        c.check("redaction is enabled by BENCH_REDACT", redact.is_enabled() is True, "")
        host = redact.pseudonym("host", "DESKTOP-ABC")
        c.check("a pseudonym is deterministic and well-formed",
                host == redact.pseudonym("host", "DESKTOP-ABC")
                and host.startswith("host-") and len(host) == len("host-") + 8, host)
        c.check("a pseudonym is stable under a second pass",
                redact.pseudonym("host", host) == host, host)

        r = redact.redact_obj(obj)
        c.check("the hostname is pseudonymised", r["hostname"] == host, r["hostname"])
        c.check("non-PII is left intact",
                r["cpu"] == "AMD Ryzen 9 5950X" and r["threads"] == 32
                and r["ram_gib"] == 64.0, r)
        c.check("a serial is pseudonymised",
                str(r["serial"]).startswith("serial-") and "SN-12345" not in json.dumps(r),
                r["serial"])
        c.check("MAC and IP are redacted in a nested inventory",
                "aa:bb:cc:dd:ee:ff" not in json.dumps(r)
                and "192.168.1.5" not in json.dumps(r), r)
        c.check("a home path keeps its prefix and redacts the user",
                str(r["log"]).startswith("C:\\Users\\") and "alice" not in r["log"], r["log"])
        c.check("free text scrubs a MAC and an IP",
                "aa:bb" not in redact.redact_text("mac aa:bb:cc:dd:ee:ff ip 10.0.0.7")
                and "10.0.0.7" not in redact.redact_text("ip 10.0.0.7"), "")

        raw = json.dumps({"host": "DESKTOP-ABC", "gpu_soak_tflops": 81.1}).encode()
        out = redact.redact_bytes("report.json", raw).decode()
        c.check("a JSON artifact is redacted on upload",
                "DESKTOP-ABC" not in out and "81.1" in out, out)
        c.check("a binary artifact passes through unchanged",
                redact.redact_bytes("shot.png", b"\x89PNG\x00") == b"\x89PNG\x00", "")
    finally:
        if saved is None:
            os.environ.pop("BENCH_REDACT", None)
        else:
            os.environ["BENCH_REDACT"] = saved


def _consent(c):
    saved = os.environ.get("BENCH_UPLOAD_CONSENT")
    try:
        os.environ.pop("BENCH_UPLOAD_CONSENT", None)
        c.check("upload consent is not granted by default",
                up.consent_granted() is False, "")
        c.check("consent_granted reads BENCH_UPLOAD_CONSENT",
                up.consent_granted({"BENCH_UPLOAD_CONSENT": "1"}) is True
                and up.consent_granted({"BENCH_UPLOAD_CONSENT": "0"}) is False, "")

        src = tmpdir()
        with open(os.path.join(src, "r.json"), "w") as fh:
            fh.write("{}")
        conf = os.path.join(tmpdir(), "bench-upload.conf")
        with open(conf, "w") as fh:
            fh.write("GITHUB_TOKEN=t\nGITHUB_REPO=o/r\n")
        calls = []
        saved_find, saved_commit = up.find_conf, up.upload_as_one_commit
        saved_argv = sys.argv
        up.find_conf = lambda: conf
        up.upload_as_one_commit = lambda *a, **k: (calls.append(a), True)[1]
        try:
            sys.argv = ["upload.py", src]
            rc = up.main()
            c.check("an upload without consent is skipped",
                    rc == 3 and not calls, (rc, calls))
            os.environ["BENCH_UPLOAD_CONSENT"] = "1"
            rc2 = up.main()
            c.check("an upload with consent proceeds",
                    rc2 == 0 and len(calls) == 1, (rc2, calls))
        finally:
            sys.argv = saved_argv
            up.find_conf, up.upload_as_one_commit = saved_find, saved_commit
    finally:
        if saved is None:
            os.environ.pop("BENCH_UPLOAD_CONSENT", None)
        else:
            os.environ["BENCH_UPLOAD_CONSENT"] = saved


def _control_plane(c):
    srv = Server()
    cl = srv.client()
    B = srv.benchd
    saved = B.Handler.store_client_ip
    try:
        B.Handler.store_client_ip = True
        st, reg = cl.call("POST", "/api/v1/register",
                          {"name": "p1", "os": "Linux", "kind": "agent"})
        row = B.Handler.store.one("SELECT addr FROM runners WHERE id=?", (reg["runner_id"],))
        c.check("the client IP is stored by default",
                bool(row) and row.get("addr") == "127.0.0.1", row)
        B.Handler.store_client_ip = False
        st, reg2 = cl.call("POST", "/api/v1/register",
                           {"name": "p2", "os": "Linux", "kind": "agent"})
        row2 = B.Handler.store.one("SELECT addr FROM runners WHERE id=?", (reg2["runner_id"],))
        c.check("client IP storage can be disabled",
                bool(row2) and row2.get("addr") is None, row2)
    finally:
        B.Handler.store_client_ip = saved
        srv.stop()

    # the retention sweep anonymises stale runner rows (module-level helper)
    spec = importlib.util.spec_from_file_location(
        "benchd_privacy", os.path.join(REPO, "server", "benchd.py"))
    bdm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bdm)
    rows = [{"id": "r1", "name": "DESKTOP-ABC", "machine_id": "win-abc", "addr": "10.0.0.9"}]

    class _Store:
        def __init__(self):
            self.updates = []

        def q(self, sql, args=()):
            return rows

        def ex(self, sql, args=()):
            self.updates.append((sql, args))
            return type("C", (), {"rowcount": 1})()

    bdm.RETENTION_DAYS = 30.0
    bdm._ANON_LAST[0] = 0.0
    store = _Store()
    n = bdm.anonymise_old_runners(store, force=True)
    c.check("the retention sweep anonymises stale runners",
            n == 1 and len(store.updates) == 1, (n, store.updates))
    sql, args = store.updates[0]
    c.check("the client IP is blanked on anonymise", "addr=NULL" in sql, sql)
    c.check("hostname and machine id are pseudonymised",
            str(args[0]).startswith("host-") and str(args[1]).startswith("machine-")
            and "DESKTOP-ABC" not in json.dumps(store.updates), args)
    bdm.RETENTION_DAYS = 0.0
    c.check("retention disabled disables anonymisation",
            bdm.anonymise_old_runners(_Store(), force=True) == 0, "")


def run():
    c = Checker("privacy")
    _redaction(c)
    _consent(c)
    _control_plane(c)
    return c.fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
