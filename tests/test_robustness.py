"""Robustness fixes (wave W14): finish retries, atomic battery start, safe
tarball extraction, and an idempotent result import.

Stdlib only; the reporting/import checks run against the in-process benchd
harness like the other modules.
"""
import hashlib
import importlib.util
import io
import json
import os
import sys
import tarfile
import threading
import time

from harness import Checker, Server, BENCH, tmpdir, wait_for
import benchreport as br
import import_results as imp


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _tar(path, entries):
    """Build a gzip tarball from (name, kind, data) tuples.

    kind 'file' -> regular file (data is bytes); 'sym' -> symlink (data is the
    link target).
    """
    with tarfile.open(path, "w:gz") as tf:
        for name, kind, data in entries:
            if kind == "sym":
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.SYMTYPE
                ti.linkname = data
                tf.addfile(ti)
            else:
                b = data if isinstance(data, bytes) else data.encode()
                ti = tarfile.TarInfo(name)
                ti.size = len(b)
                ti.mode = 0o755
                tf.addfile(ti, io.BytesIO(b))
    return path


def run():
    c = Checker("robustness")
    srv = Server()
    cl = srv.client()

    # ---- RES-P2-001: finish retries with backoff, never loses a done run ----
    rep = br.Reporter(srv.base, srv.token, name="retry", kind="test")
    c.check("register", rep.register({}))
    c.check("start_run", rep.start_run("retry", {}) is not None)
    rid = rep.run_id
    real_req = br.Reporter._req.__get__(rep)
    state = {"n": 0}

    def flaky(method, path, body=None):
        state["n"] += 1
        if state["n"] == 1:
            return 0, {}                       # first post "blips"
        return real_req(method, path, body)

    rep._req = flaky
    ok = rep.finish("done", 0, {"gpu_soak_tflops": 7.0}, backoff=0.01)
    c.check("finish retries then succeeds", ok is True and state["n"] == 2, (ok, state["n"]))
    c.check("the run is closed only after the server acknowledges it",
            rep.run_id is None, rep.run_id)
    _, run = cl.call("GET", "/api/v1/runs/" + rid)
    c.check("the retried finish is recorded as done, with its summary",
            run.get("status") == "done"
            and (run.get("summary") or {}).get("gpu_soak_tflops") == 7.0, run)
    # the server's finish is an idempotent UPDATE, so a re-send is safe
    st, _ = cl.call("POST", "/api/v1/runs/%s/finish" % rid,
                    {"status": "done", "exit_code": 0, "summary": {"gpu_soak_tflops": 7.0}})
    _, run2 = cl.call("GET", "/api/v1/runs/" + rid)
    c.check("a re-sent finish is refused (409) and leaves the run intact",
            st == 409 and run2.get("status") == "done"
            and (run2.get("summary") or {}).get("gpu_soak_tflops") == 7.0, (st, run2))
    cl.call("DELETE", "/api/v1/runs/" + rid)

    # a permanent transport failure must not flip a finished run to failed
    rep2 = br.Reporter("http://127.0.0.1:1", "t", name="down", kind="test")
    rep2.enabled = True
    rep2.run_id = "run-down"
    rep2._req = lambda *a, **k: (0, {})
    ok2 = rep2.finish("done", 0, {"x": 1}, retries=2, backoff=0.01)
    c.check("a network blip never marks a finished run failed",
            ok2 is False and rep2.run_id == "run-down", (ok2, rep2.run_id))

    # ---- RES-P3-002: bounded retries + circuit breaker + visible drops ----
    rep3 = br.Reporter("http://127.0.0.1:1", "t", name="resilient", kind="test",
                       retries=3, backoff=0.001, circuit_threshold=3, circuit_cooldown=0.05)
    rep3.enabled = True
    rep3.run_id = "run-x"
    attempts = {"n": 0}

    def flaky_attempt(method, path, body=None):
        attempts["n"] += 1
        if attempts["n"] < 3:
            rep3.last_error = "boom"
            return 0, {}
        return 200, {"ok": True}

    rep3._attempt = flaky_attempt
    s, _r = rep3._req("POST", "/api/v1/runs/run-x/telemetry", {"samples": [1]})
    c.check("a request retries with backoff and then succeeds",
            s == 200 and attempts["n"] == 3 and rep3.retry_count == 2,
            (s, attempts["n"], rep3.retry_count))
    c.check("the breaker stays closed after a recovered blip",
            rep3.circuit_state == "closed" and rep3._fail_streak == 0, rep3.circuit_state)

    # consecutive failures open the breaker
    rep3._attempt = lambda *a, **k: (0, {})
    for _ in range(3):
        rep3._req("POST", "/api/v1/runs/run-x/telemetry", {"samples": [1]})
    c.check("the breaker opens after N consecutive failures",
            rep3.circuit_state == "open", (rep3.circuit_state, rep3._fail_streak))
    calls = {"n": 0}

    def counting_attempt(method, path, body=None):
        calls["n"] += 1
        return 200, {}

    rep3._attempt = counting_attempt
    s2, _r2 = rep3._req("GET", "/api/v1/runners/run-x/commands")
    c.check("an open breaker fails fast without a transport attempt",
            s2 == 0 and calls["n"] == 0, (s2, calls["n"]))
    c.check("an open breaker is visible as 'unreachable'",
            rep3.status() == "unreachable", rep3.status())

    # after the cooldown one half-open probe is allowed and success closes it
    wait_for(lambda: time.time() >= rep3._circuit_open_until, timeout=1)
    s3, _r3 = rep3._req("GET", "/api/v1/runners/run-x/commands")
    c.check("after the cooldown a half-open probe is allowed and closes the breaker",
            s3 == 200 and calls["n"] == 1 and rep3.circuit_state == "closed",
            (s3, calls["n"], rep3.circuit_state))

    # a dropped telemetry batch is counted and logged, not silent
    logs = []
    rep4 = br.Reporter("http://127.0.0.1:1", "t", name="drops", kind="test",
                       retries=1, log=logs.append)
    rep4.enabled = True
    rep4.run_id = "run-y"
    rep4._attempt = lambda *a, **k: (0, {})
    ok = rep4.telemetry([{"elapsed": 1.0}])
    c.check("a telemetry drop returns a 0 status (never a silent success)",
            ok == 0, ok)
    c.check("the reporter records the failure instead of hiding it",
            rep4._fail_streak >= 1 and rep4.status() != "connected",
            (rep4._fail_streak, rep4.status()))

    # ---- RES-P2-003: a concurrent second start is rejected atomically ----
    import agent as agmod
    ag = agmod.Agent.__new__(agmod.Agent)
    ag.running = False
    ag._claimed = False
    ag._start_lock = threading.Lock()
    ag.log = lambda *_a: None
    started = threading.Event()
    release = threading.Event()

    def fake_run(label, profile, extra_args=None):
        started.set()
        release.wait(5)

    ag.run_battery = fake_run
    acks = []

    class _Rep:
        def ack(self, cid, status="done", result=None):
            acks.append((cid, status, result))

    ag.rep = _Rep()
    ag.handle({"kind": "start", "id": "c1", "payload": {"label": "first"}})
    c.check("the first start claims the slot", started.wait(2), "")
    ag.handle({"kind": "start", "id": "c2", "payload": {"label": "second"}})
    c.check("a concurrent second start is rejected",
            any(r == {"skipped": "already running"} for _c, _s, r in acks), acks)
    c.check("the second start never reports started",
            not any(r == {"started": True} for _c, _s, r in acks if _c == "c2"), acks)
    release.set()
    time.sleep(0.2)

    # two racing claims: exactly one wins
    wins = []
    barrier = threading.Barrier(2)

    def racer():
        barrier.wait()
        wins.append(ag._claim_start())

    ths = [threading.Thread(target=racer) for _ in range(2)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(5)
    c.check("only one of two racing claims wins", wins.count(True) == 1, wins)
    ag._claimed = False

    # ---- SC-P2-003: safe extraction + fail-closed integrity ----
    bs = _load("bootstrap_w14", os.path.join(BENCH, "bootstrap.py"))
    sand = tmpdir()

    traversal = _tar(os.path.join(sand, "traversal.tar.gz"),
                     [("python/../../escape", "file", b"pwned"),
                      ("python/keep.txt", "file", b"ok")])
    dest = os.path.join(sand, "out")
    os.makedirs(dest)
    raised = False
    try:
        bs._safe_extract(traversal, dest)
    except Exception:
        raised = True
    c.check("a `..` traversal archive is refused (fails closed)", raised, "")
    c.check("the traversal escaped nothing",
            not os.path.exists(os.path.join(sand, "escape"))
            and not os.path.exists(os.path.join(dest, "..", "escape")), "")
    c.check("nothing from the traversal archive was extracted",
            not os.path.exists(os.path.join(dest, "python")), os.listdir(dest))

    link = _tar(os.path.join(sand, "link.tar.gz"),
                [("python/evil", "sym", "/etc/passwd"),
                 ("python/keep.txt", "file", b"ok")])
    dest_link = os.path.join(sand, "out-link")
    os.makedirs(dest_link)
    raised2 = False
    try:
        bs._safe_extract(link, dest_link)
    except Exception:
        raised2 = True
    c.check("an escaping symlink is refused (fails closed)", raised2, "")
    c.check("the escaping symlink was not created",
            not os.path.lexists(os.path.join(dest_link, "python", "evil")), "")

    good = _tar(os.path.join(sand, "good.tar.gz"),
                [("python/bin/python3", "file", b"hello")])
    dest_good = os.path.join(sand, "out-good")
    os.makedirs(dest_good)
    bs._safe_extract(good, dest_good)
    c.check("a clean archive still extracts",
            os.path.isfile(os.path.join(dest_good, "python", "bin", "python3")), "")

    blob = os.path.join(sand, "blob.bin")
    with open(blob, "wb") as fh:
        fh.write(b"payload")
    good_hash = hashlib.sha256(b"payload").hexdigest()
    c.check("a matching hash verifies", bs._verify_sha256(blob, good_hash, "blob") is True, "")
    c.check("no published hash falls back to HTTPS (returns False)",
            bs._verify_sha256(blob, "", "blob") is False, "")
    mismatch = False
    try:
        bs._verify_sha256(blob, "0" * 64, "blob")
    except RuntimeError:
        mismatch = True
    c.check("a bad hash is refused (fails closed)", mismatch, "")

    # ---- RES-P2-002: importing the same results twice yields one copy ----
    rdir = tmpdir()
    run_dir = os.path.join(rdir, "host-a-run")
    os.makedirs(run_dir)
    with open(os.path.join(run_dir, "host-linux-20260101-000000.json"), "w") as fh:
        json.dump({"host": "host-a", "timestamp": "2026-01-01T00:00:00Z",
                   "phases": [{"group": "gpu", "phase": "matmul",
                               "result": {"tflops": 1.0}}],
                   "telemetry": {"duration_s": 60}}, fh)
    with open(os.path.join(run_dir, "runs-index.jsonl"), "w") as fh:
        fh.write(json.dumps({"host": "host-a",
                             "timestamp": "2026-01-01T00:00:00Z"}) + "\n")

    argv = ["import_results.py", "--server", srv.base, "--token", srv.token, "--dir", rdir]
    saved = sys.argv
    try:
        sys.argv = argv
        imp.main()
        sys.argv = argv
        imp.main()
    finally:
        sys.argv = saved
    _, runs = cl.call("GET", "/api/v1/runs?limit=1000")
    c.check("importing the same results twice yields one copy",
            runs.get("total") == 1, runs.get("total"))
    c.check("the imported run carries a deterministic id",
            bool(runs.get("runs")) and str(runs["runs"][0].get("id", "")).startswith("imp-"),
            runs.get("runs"))

    srv.stop()
    return c.fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
