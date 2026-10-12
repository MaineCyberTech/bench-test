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
import sys
import tarfile
import threading

from harness import Checker, tmpdir, BENCH
import benchreport as br


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
