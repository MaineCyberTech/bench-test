"""Remote self-update tests (the live_update / benchreport update helpers).

Everything runs against a local stub server: no network, no real block devices,
and nothing destructive (iso_write is only exercised through its guards).
"""
import hashlib
import http.server
import io
import json
import os
import socketserver
import subprocess
import sys
import tarfile
import threading

from harness import Checker, tmpdir, BENCH
import benchreport as br


def _git(where, *args, check=True):
    r = subprocess.run(["git", "-C", where] + list(args), capture_output=True, text=True,
                       timeout=120)
    if check and r.returncode != 0:
        raise AssertionError("git %s failed: %s" % (" ".join(args), r.stderr.strip()))
    return r


def _tarball(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            b = data.encode()
            ti = tarfile.TarInfo("bench-test-abc123/" + name)
            ti.size = len(b)
            ti.mode = 0o755
            tf.addfile(ti, io.BytesIO(b))
    return buf.getvalue()


class _Stub(http.server.BaseHTTPRequestHandler):
    toolkit = b""
    parts = []
    sums = ""

    def log_message(self, *_a):
        pass

    def do_GET(self):
        if self.path.startswith("/toolkit"):
            body, ctype = self.toolkit, "application/gzip"
        elif self.path.startswith("/repos/"):
            assets = [{"name": "bench-live-%s.iso.part.%s" % ("nvidia-legacy", chr(97 + i)),
                       "browser_download_url": "http://%s/blob/part.%s" % (self.headers["Host"], chr(97 + i)),
                       "size": len(p)} for i, p in enumerate(self.parts)]
            assets.append({"name": "SHA256SUMS",
                           "browser_download_url": "http://%s/blob/SHA256SUMS" % self.headers["Host"],
                           "size": len(self.sums)})
            body = json.dumps({"tag_name": "iso-stub-1", "assets": assets}).encode()
            ctype = "application/json"
        elif self.path == "/blob/SHA256SUMS":
            body, ctype = self.sums.encode(), "text/plain"
        elif self.path.startswith("/blob/part."):
            body, ctype = self.parts[ord(self.path[-1]) - 97], "application/octet-stream"
        else:
            body, ctype = b"", "text/plain"
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve():
    httpd = socketserver.TCPServer(("127.0.0.1", 0), _Stub)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, "http://127.0.0.1:%d" % httpd.server_address[1]


def run():
    c = Checker("update")

    # os.symlink needs privilege on Windows; the switch is a Linux/live feature
    try:
        _p = tmpdir()
        os.symlink(_p, os.path.join(_p, "l"))
        CAN_SYMLINK = True
    except OSError:
        CAN_SYMLINK = False

    toolkit = _tarball({"bench/benchreport.py": "x = 1\n", "bench/tui.py": "y = 2\n",
                        "live/airootfs/usr/local/bin/bench-tui": "#!/bin/sh\n"})
    _Stub.toolkit = toolkit
    httpd, base = _serve()

    # sandbox: toolkit dir + persist dir under a temp dir, switch allowed
    sand = tmpdir()
    toolkit_dir = os.path.join(sand, "opt", "bench-test")
    os.makedirs(toolkit_dir)
    open(os.path.join(toolkit_dir, "marker"), "w").close()
    os.environ.update({"BENCH_TOOLKIT_DIR": toolkit_dir,
                       "BENCH_PERSIST_DIR": os.path.join(sand, "stick"),
                       "BENCH_ALLOW_TOOLKIT_SWITCH": "1",
                       "BENCH_TOOLKIT_URL": base + "/toolkit.tar.gz",
                       "BENCH_GH_API": base,
                       "BENCH_REPO_SLUG": "stub/repo"})
    os.makedirs(os.path.join(sand, "stick"))
    br.TOOLKIT_DIR = toolkit_dir
    br.PERSIST_DIR = os.path.join(sand, "stick")
    br.GH_API = base

    # ---- toolkit update: download, extract, switch, persist ----
    sha = hashlib.sha256(toolkit).hexdigest()
    r = br.update_toolkit("main", sha256=sha)
    c.check("toolkit update ok", r.get("ok") is True, r)
    c.check("toolkit sha256 recorded", r.get("sha256") == sha, r.get("sha256"))
    c.check("toolkit extracted to the stick",
            os.path.isfile(os.path.join(r["dest"], "bench", "benchreport.py")), r.get("dest"))
    c.check("toolkit switched", r.get("switched") is True if CAN_SYMLINK else True,
            r if CAN_SYMLINK else "skipped: no symlink privilege on Windows")
    vp = os.path.join(r["dest"], br.VERSION_FILE)
    c.check("version stamp written", os.path.isfile(vp), vp)
    try:
        with open(vp, encoding="utf-8") as fh:
            ver = json.load(fh)
    except Exception:
        ver = {}
    c.check("version stamp records ref + sha",
            ver.get("ref") == "main" and ver.get("sha256") == sha, ver)
    c.check("toolkit_version reads a copy's stamp",
            br.toolkit_version(r["dest"]).get("sha256") == sha, br.toolkit_version(r["dest"]))
    c.check("toolkit_version reports git for this checkout",
            br.toolkit_version(os.path.dirname(BENCH)).get("mode") == "git", "")
    if CAN_SYMLINK:
        c.check("toolkit dir is a symlink to the fetched copy",
                os.path.islink(toolkit_dir)
                and os.path.isfile(os.path.join(toolkit_dir, "bench", "benchreport.py")), toolkit_dir)
        c.check("exec bit preserved",
                os.access(os.path.join(toolkit_dir, "live", "airootfs", "usr", "local",
                                       "bin", "bench-tui"), os.X_OK), "")
        c.check("current symlink written",
                os.path.isfile(os.path.join(r["current"], "bench", "benchreport.py")), r.get("current"))

    # ---- sha256 mismatch is refused ----
    bad = br.update_toolkit("main", sha256="0" * 64)
    c.check("bad sha256 refused", "sha256" in str(bad.get("error", "")), bad)

    if CAN_SYMLINK:
        # ---- boot link re-points a reset toolkit dir ----
        os.remove(toolkit_dir)
        os.makedirs(toolkit_dir)
        c.check("boot-link restores the toolkit", br.toolkit_boot_link() is True, "")
        c.check("boot-link target is the fetched copy",
                os.path.isfile(os.path.join(toolkit_dir, "bench", "benchreport.py")), "")

    # ---- boot-time re-point when BENCHDATA cannot hold symlinks (it is vfat) ----
    root2 = os.path.join(sand, "stick2", "bench-toolkit")
    for name, when in (("main", 111), ("older", 11)):
        os.makedirs(os.path.join(root2, name, "bench"))
        open(os.path.join(root2, name, "bench", "benchreport.py"), "w").close()
        with open(os.path.join(root2, name, br.VERSION_FILE), "w") as fh:
            json.dump({"fetched_at": when}, fh)
    c.check("_newest_toolkit_copy picks the freshest fetched copy",
            os.path.basename(br._newest_toolkit_copy(root2)) == "main",
            br._newest_toolkit_copy(root2))
    c.check("_newest_toolkit_copy tolerates a missing root",
            br._newest_toolkit_copy(os.path.join(sand, "absent")) == "", "")

    saved_persist = br.PERSIST_DIR
    try:
        br.PERSIST_DIR = os.path.join(sand, "stick-empty")
        c.check("boot-link reports nothing to point at",
                br.toolkit_boot_link() is False, "")
        br.PERSIST_DIR = os.path.join(sand, "stick2")
        with open(os.path.join(root2, "current.txt"), "w") as fh:
            fh.write(os.path.join(root2, "main"))
        if CAN_SYMLINK:
            os.remove(toolkit_dir)
            os.makedirs(toolkit_dir)
            c.check("boot-link follows current.txt when 'current' is absent",
                    br.toolkit_boot_link() is True, "")
            os.remove(os.path.join(root2, "current.txt"))
            c.check("boot-link falls back to the newest copy without a pointer",
                    br.toolkit_boot_link() is True, "")
    finally:
        br.PERSIST_DIR = saved_persist

    # ---- iso fetch: concatenate parts + verify SHA256SUMS ----
    _Stub.parts = [b"A" * 1024, b"B" * 2048, b"C" * 512]
    iso_bytes = b"".join(_Stub.parts)
    _Stub.sums = "%s  bench-live-nvidia-legacy.iso\n" % hashlib.sha256(iso_bytes).hexdigest()
    r2 = br.iso_fetch("iso-stub-1", "nvidia-legacy", dest=os.path.join(sand, "iso"))
    c.check("iso fetch ok", r2.get("ok") and r2.get("bytes") == len(iso_bytes), r2)
    c.check("iso verified against SHA256SUMS", r2.get("verified") is True, r2)
    c.check("iso reassembled in part order",
            open(r2["iso"], "rb").read() == iso_bytes, r2.get("iso"))

    _Stub.sums = "%s  bench-live-nvidia-legacy.iso\n" % ("0" * 64)
    r3 = br.iso_fetch("iso-stub-1", "nvidia-legacy", dest=os.path.join(sand, "iso2"))
    c.check("iso mismatched sum flagged", r3.get("verified") is False, r3)

    # ---- restart semantics ----
    c.check("git update always restarts (so the commit re-registers)",
            br.update_needs_restart({"ok": True, "kind": "git", "out": "Already up to date."}) is True, "")
    c.check("toolkit switch restarts",
            br.update_needs_restart({"ok": True, "switched": True}) is True, "")
    c.check("no-op toolkit update does not restart",
            br.update_needs_restart({"ok": True, "switched": False}) is False, "")
    c.check("failed update does not restart",
            br.update_needs_restart({"error": "x"}) is False, "")

    # ---- auto_update: the startup check (no network needed) ----
    saved_offline = os.environ.pop("BENCH_OFFLINE", None)
    try:
        c.check("auto_update can be turned off",
                br.auto_update(BENCH, enabled=False)["updated"] is False, "")
        c.check("auto_update ignores a non-git dir",
                br.auto_update(sand)["kind"] == "none", br.auto_update(sand))

        repo2 = tmpdir()
        _git(repo2, "init", "-q")
        _git(repo2, "config", "user.email", "t@t")
        _git(repo2, "config", "user.name", "t")
        open(os.path.join(repo2, "f.txt"), "w").write("x")
        _git(repo2, "add", "-A")
        _git(repo2, "commit", "-q", "-m", "init")
        open(os.path.join(repo2, "f.txt"), "w").write("dirty")
        c.check("auto_update refuses to touch a dirty checkout",
                br.auto_update(repo2).get("skipped") == "dirty", br.auto_update(repo2))

        os.environ["BENCH_NO_UPDATE"] = "1"
        c.check("BENCH_NO_UPDATE=1 disables it", br.auto_update(repo2)["updated"] is False, "")
        os.environ.pop("BENCH_NO_UPDATE")

        # a clean checkout behind its upstream gets fast-forwarded
        bare = os.path.join(sand, "origin.git")
        _git(sand, "init", "-q", "--bare", "-b", "main", bare)
        work, behind = os.path.join(sand, "work"), os.path.join(sand, "behind")
        _git(sand, "clone", "-q", bare, work)
        _git(work, "config", "user.email", "t@t")
        _git(work, "config", "user.name", "t")
        open(os.path.join(work, "a.txt"), "w").write("a")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", "a")
        _git(work, "push", "-q", "-u", "origin", "main")
        _git(sand, "clone", "-q", bare, behind)          # now at commit a
        open(os.path.join(work, "b.txt"), "w").write("b")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", "b")
        _git(work, "push", "-q", "origin", "main")

        res = br.auto_update(behind, log=lambda _m: None)
        c.check("auto_update pulls when behind", res.get("updated") is True, res)
        c.check("auto_update reports how far behind", res.get("behind") == 1, res)
        c.check("auto_update actually fast-forwarded the checkout",
                os.path.isfile(os.path.join(behind, "b.txt")), res.get("out"))
        c.check("auto_update is a no-op when current",
                br.auto_update(behind)["updated"] is False, br.auto_update(behind))

        os.environ["BENCH_OFFLINE"] = "1"
        c.check("auto_update skips when offline",
                br.auto_update(behind).get("skipped") == "offline", "")
        os.environ.pop("BENCH_OFFLINE")
    finally:
        if saved_offline is not None:
            os.environ["BENCH_OFFLINE"] = saved_offline

    # ---- the dashboards/agent check for updates at startup ----
    for f in ("gui.py", "tui.py", "agent.py"):
        src = open(os.path.join(BENCH, f), encoding="utf-8").read()
        c.check("auto-update wired into %s" % f, "auto_update(" in src, f)

    # ---- A/B slot write guards (never touches a device here) ----
    c.check("iso_write_slot needs confirm",
            "confirm" in str(br.iso_write_slot(r2["iso"], confirm=False).get("error")), "")
    c.check("iso_write_slot rejects a missing iso",
            "no such iso" in str(br.iso_write_slot("/nope.iso", confirm=True).get("error")), "")
    if os.path.exists("/dev/disk/by-label/BENCH_ESP"):
        c.check("iso_write_slot not exercised on a real A/B stick (destructive)", True, "")
    else:
        c.check("iso_write_slot refuses a non-A/B machine",
                "BENCH_ESP" in str(br.iso_write_slot(r2["iso"], confirm=True).get("error")), "")
    c.check("_mounts_of tolerates an unknown device",
            br._mounts_of("/dev/definitely-not-a-disk") == [], "")

    # ---- iso write guards (never actually writes) ----
    c.check("iso_write needs confirm", "confirm" in str(br.iso_write(r2["iso"], "/dev/null").get("error")), "")
    c.check("iso_write rejects a missing iso",
            "no such iso" in str(br.iso_write("/nope.iso", "/dev/null", confirm=True).get("error")), "")
    c.check("iso_write rejects a non-/dev path",
            "/dev/" in str(br.iso_write(r2["iso"], "/tmp/x", confirm=True).get("error")), "")
    if os.name != "nt":
        c.check("iso_write rejects a non-block device",
                "not a block device" in str(br.iso_write(r2["iso"], "/dev/null", confirm=True).get("error")), "")
    else:
        c.check("iso_write rejects a nonexistent device",
                "cannot stat" in str(br.iso_write(r2["iso"], "/dev/nope", confirm=True).get("error")), "")

    # ---- hostile archive entries are ignored ----
    hostile = io.BytesIO()
    with tarfile.open(fileobj=hostile, mode="w:gz") as tf:
        b = b"pwned"
        ti = tarfile.TarInfo("pkg-abc/../../escape")
        ti.size = len(b)
        tf.addfile(ti, io.BytesIO(b))
        link = tarfile.TarInfo("pkg-abc/bench/evil")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tf.addfile(link)
        good = b"ok"
        gi = tarfile.TarInfo("pkg-abc/bench/benchreport.py")
        gi.size = len(good)
        tf.addfile(gi, io.BytesIO(good))
    _Stub.toolkit = hostile.getvalue()
    r4 = br.update_toolkit("hostile")
    c.check("hostile archive: update still succeeds on the safe files", r4.get("ok") is True, r4)
    c.check("hostile archive: traversal escaped nothing",
            not os.path.exists(os.path.join(sand, "escape"))
            and not os.path.exists(os.path.join(sand, "..", "escape")), "")
    c.check("hostile archive: symlink not created",
            not os.path.islink(os.path.join(toolkit_dir, "bench", "evil")), "")

    httpd.shutdown()
    for k in ("BENCH_TOOLKIT_DIR", "BENCH_PERSIST_DIR", "BENCH_ALLOW_TOOLKIT_SWITCH",
              "BENCH_TOOLKIT_URL", "BENCH_GH_API", "BENCH_REPO_SLUG"):
        os.environ.pop(k, None)
    return c.fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
