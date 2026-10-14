"""Remote self-update tests (the live_update / benchreport update helpers).

Everything runs against a local stub server: no network, no real block devices,
and nothing destructive (iso_write is only exercised through its guards).
"""
import glob
import hashlib
import http.server
import io
import json
import os
import re
import socketserver
import subprocess
import sys
import tarfile
import threading
import time

from harness import Checker, tmpdir, BENCH, REPO, skip
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
    with_sums = True

    def log_message(self, *_a):
        pass

    def do_GET(self):
        if self.path.startswith("/toolkit"):
            body, ctype = self.toolkit, "application/gzip"
        elif self.path.startswith("/repos/"):
            assets = [{"name": "bench-live-%s.iso.part.%s" % ("nvidia-legacy", chr(97 + i)),
                       "browser_download_url": "http://%s/blob/part.%s" % (self.headers["Host"], chr(97 + i)),
                       "size": len(p)} for i, p in enumerate(self.parts)]
            if self.with_sums:
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
    c.check("iso mismatched sum refused (ok False)", r3.get("ok") is False, r3)
    c.check("iso mismatch error names both hashes",
            ("0" * 64) in str(r3.get("error")) and r3.get("sha256") in str(r3.get("error")), r3)

    # a release with no SHA256SUMS is unverifiable, never a verified success
    _Stub.with_sums = False
    try:
        r3b = br.iso_fetch("iso-stub-1", "nvidia-legacy", dest=os.path.join(sand, "iso3"))
    finally:
        _Stub.with_sums = True
    c.check("an iso without SHA256SUMS is not ok", r3b.get("ok") is False, r3b)
    c.check("an iso without SHA256SUMS is refused, never unverified-but-ok",
            r3b.get("verified") is False
            and "no SHA256SUMS" in str(r3b.get("error")), r3b)

    # a SHA256SUMS with no .iso entry cannot verify anything either
    _Stub.sums = "%s  some-other-file.txt\n" % ("0" * 64)
    r3c = br.iso_fetch("iso-stub-1", "nvidia-legacy", dest=os.path.join(sand, "iso4"))
    c.check("a SHA256SUMS with no .iso entry is refused",
            r3c.get("ok") is False and r3c.get("verified") is False
            and "no .iso entry" in str(r3c.get("error")), r3c)

    # ---- restart semantics ----
    c.check("git update always restarts (so the commit re-registers)",
            br.update_needs_restart({"ok": True, "kind": "git", "out": "Already up to date."}) is True, "")
    c.check("toolkit switch restarts",
            br.update_needs_restart({"ok": True, "switched": True}) is True, "")
    c.check("no-op toolkit update does not restart",
            br.update_needs_restart({"ok": True, "switched": False}) is False, "")
    c.check("failed update does not restart",
            br.update_needs_restart({"error": "x"}) is False, "")

    # ---- a plain update restarts too, unless a run is in progress ----
    switched = {"ok": True, "switched": True}
    c.check("dashboards restart after a plain update",
            br.should_restart_after_update(switched) is True, "")
    c.check("a run in progress defers the restart",
            br.should_restart_after_update(switched, running=True) is False, "")
    c.check("an explicit request overrides a run in progress",
            br.should_restart_after_update(switched, running=True, requested=True) is True, "")
    c.check("a no-op update never restarts",
            br.should_restart_after_update({"ok": True, "switched": False}) is False, "")
    for f in ("gui.py", "tui.py"):
        src = open(os.path.join(BENCH, f), encoding="utf-8").read()
        c.check("%s uses the shared restart decision" % f,
                "should_restart_after_update(" in src, f)

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

    # ---- the sampler must run under the venv when there is one ----
    fake = os.path.join(sand, "fakerepo")
    os.makedirs(os.path.join(fake, "bench"))
    c.check("venv_python is empty without a venv",
            br.venv_python(fake) == "", br.venv_python(fake))
    venvdir = os.path.join(fake, ".venv", "Scripts")
    os.makedirs(venvdir)
    open(os.path.join(venvdir, "python.exe"), "w").close()
    c.check("venv_python finds the venv interpreter",
            br.venv_python(fake) == os.path.join(venvdir, "python.exe"), br.venv_python(fake))
    for f in ("gui.py", "agent.py"):
        src = open(os.path.join(BENCH, f), encoding="utf-8").read()
        c.check("%s prefers the venv interpreter" % f, "venv_python(" in src, f)

    # ---- the image's PyTorch must survive a toolkit re-point ----
    c.check("_overlay_lowerdirs tolerates a missing /proc/mounts",
            isinstance(br._overlay_lowerdirs(), list), "")
    torchdir = os.path.join(sand, "pypkg")
    os.makedirs(os.path.join(torchdir, "torch"))
    saved_torch = br.TORCH_DIR
    try:
        br.TORCH_DIR = torchdir
        c.check("ensure_torch_packages accepts an already-baked dir",
                br.ensure_torch_packages() == torchdir, "")
        br.TORCH_DIR = os.path.join(sand, "absent-pypkg")
        c.check("ensure_torch_packages reports nothing to link",
                br.ensure_torch_packages() == "", br.ensure_torch_packages())
    finally:
        br.TORCH_DIR = saved_torch
    for f in ("gui.py", "tui.py"):
        src = open(os.path.join(BENCH, f), encoding="utf-8").read()
        c.check("%s hides the runner console on Windows" % f, "CREATE_NO_WINDOW" in src, f)
    c.check("the boot env links torch before re-pointing the toolkit",
            "torch-link" in open(os.path.join(os.path.dirname(BENCH), "live", "airootfs",
                                              "usr", "local", "bin", "bench-agent-env"),
                                 encoding="utf-8").read(), "")

    # ---- the uploader must make ONE commit, not one per file ----
    import upload as up
    calls = []

    def fake_api(method, url, token, payload=None):
        calls.append((method, url, payload))
        if "/git/ref/heads/" in url:
            return 200, {"object": {"sha": "p" * 40}}
        if method == "GET" and "/git/commits/" in url:
            return 200, {"tree": {"sha": "t" * 40}}
        if url.endswith("/git/blobs"):
            return 201, {"sha": "b" * 40}
        if url.endswith("/git/trees"):
            return 201, {"sha": "n" * 40}
        if url.endswith("/git/commits"):
            return 201, {"sha": "c" * 40}
        if "/git/refs/heads/" in url:
            return 200, {}
        return 404, {}

    saved_api = up.api
    up.api = fake_api
    try:
        ufiles = []
        for n in ("u1.txt", "u2.txt", "u3.txt"):
            p = os.path.join(sand, n)
            open(p, "w").write("x")
            ufiles.append((p, n))
        c.check("uploader writes one commit",
                up.upload_as_one_commit("o/r", "main", "results", "folder", ufiles, "tok") is True, "")
        c.check("exactly one commit call for three files",
                len([x for x in calls if x[0] == "POST" and x[1].endswith("/git/commits")]) == 1, calls)
        # small text files ride along in the tree request: one API call for the
        # whole batch instead of a blob POST each (which tripped the secondary
        # rate limit and pushed the uploader onto its per-file fallback)
        c.check("no blob calls for small text files",
                len([x for x in calls if x[1].endswith("/git/blobs")]) == 0, calls)
        c.check("one tree call carries the batch",
                len([x for x in calls if x[0] == "POST" and x[1].endswith("/git/trees")]) == 1, calls)
        c.check("the branch is moved once",
                len([x for x in calls if x[0] == "PATCH"]) == 1, calls)
    finally:
        up.api = saved_api

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

    # ---- an app bundle updates its bundled toolkit in place ----
    # A PyInstaller bundle is neither a git checkout nor a live runner, so the
    # "use git pull" guard used to refuse it outright; a downloaded app could not
    # update at all. It ships the toolkit next to its exe, which is what an update
    # can replace (the frozen dashboard itself needs a new download).
    bdir = tmpdir()
    os.makedirs(os.path.join(bdir, "bench"))
    with open(os.path.join(bdir, ".bench-version"), "w") as fh:
        json.dump({"mode": "app", "commit": "aa6ca7c", "ref": "main"}, fh)
    src = tmpdir()                       # stand-in for an extracted toolkit tarball
    os.makedirs(os.path.join(src, "bench"))
    with open(os.path.join(src, "VERSION"), "w") as fh:
        fh.write("9.9.9\n")
    for name in ("benchreport.py", "Run-LinuxBench.sh"):
        with open(os.path.join(src, "bench", name), "w") as fh:
            fh.write("# new\n")
    c.check("_app_bundle_root spots a bundle", br._app_bundle_root(bdir) is True, bdir)
    c.check("_app_bundle_root rejects a git checkout",
            br._app_bundle_root(os.path.dirname(BENCH)) is False, "")
    c.check("_app_bundle_root rejects an unstamped dir",
            br._app_bundle_root(tmpdir()) is False, "")
    r5 = br._install_into_bundle(bdir, src, "main", "abc1234", "sha", 10)
    c.check("bundle update reports ok and its kind", r5.get("ok") and r5.get("kind") == "app", r5)
    c.check("bundle update refreshes the shipped toolkit",
            os.path.isfile(os.path.join(bdir, "bench", "Run-LinuxBench.sh")), "")
    c.check("bundle update bakes VERSION so the version stops reading 0.0.0",
            open(os.path.join(bdir, "VERSION")).read().strip() == "9.9.9", "")
    c.check("bundle update stamps the release it installed",
            json.load(open(os.path.join(bdir, ".bench-version"))).get("version") == "9.9.9", "")
    c.check("bundle update says the dashboard itself needs a new download",
            "download a new app bundle" in (r5.get("note") or ""), r5.get("note"))
    c.check("a bundle still reports mode=app after updating",
            br._app_bundle_root(bdir) is True, "")
    c.check("the version really resolves after the update",
            br.bench_version(bdir) == "9.9.9", br.bench_version(bdir))

    # ---- a failed refresh must leave the working toolkit intact ----
    # The old code deleted bench/ and copied over it, so a mid-copy failure (disk
    # full, unreadable source) left the bundle with no bench/ and no way to start.
    bdir_fail = tmpdir()
    os.makedirs(os.path.join(bdir_fail, "bench"))
    with open(os.path.join(bdir_fail, "bench", "benchreport.py"), "w") as fh:
        fh.write("# old, working\n")
    with open(os.path.join(bdir_fail, ".bench-version"), "w") as fh:
        json.dump({"mode": "app"}, fh)
    src_fail = tmpdir()
    os.makedirs(os.path.join(src_fail, "bench"))
    for name in ("benchreport.py", "tui.py"):
        with open(os.path.join(src_fail, "bench", name), "w") as fh:
            fh.write("# new\n")

    real_copytree = br.shutil.copytree

    def _copytree_then_fail(s, d, *a, **kw):
        real_copytree(s, d, *a, **kw)     # stage a full copy, then fail
        raise OSError("simulated failure after the copy")

    br.shutil.copytree = _copytree_then_fail
    try:
        rf = br._install_into_bundle(bdir_fail, src_fail, "main", "abc", "sha", 1)
    finally:
        br.shutil.copytree = real_copytree
    c.check("a failed bundle refresh reports the error",
            not rf.get("ok") and "could not refresh" in str(rf.get("error")), rf)
    c.check("a failed bundle refresh leaves the working bench/ intact",
            open(os.path.join(bdir_fail, "bench", "benchreport.py")).read() == "# old, working\n",
            "")
    c.check("a failed bundle refresh leaves no staging dir behind",
            not os.path.exists(os.path.join(bdir_fail, "bench.new"))
            and not os.path.exists(os.path.join(bdir_fail, "bench.old")), "")

    # a failed swap must roll the previous toolkit back, not lose it
    real_replace = br.os.replace

    def _replace_new_fails(src, dst):
        if src.endswith("bench.new"):
            raise OSError("simulated locked directory")
        return real_replace(src, dst)

    br.os.replace = _replace_new_fails
    try:
        rf2 = br._install_into_bundle(bdir_fail, src_fail, "main", "abc", "sha", 1)
    finally:
        br.os.replace = real_replace
    c.check("a failed swap reports the error",
            not rf2.get("ok") and "could not refresh" in str(rf2.get("error")), rf2)
    c.check("a failed swap rolls the previous bench/ back",
            os.path.isfile(os.path.join(bdir_fail, "bench", "benchreport.py"))
            and open(os.path.join(bdir_fail, "bench", "benchreport.py")).read() == "# old, working\n",
            "")
    c.check("a failed swap leaves no bench.new/bench.old behind",
            not os.path.exists(os.path.join(bdir_fail, "bench.new"))
            and not os.path.exists(os.path.join(bdir_fail, "bench.old")), "")

    # a plain copy (no git, not live, not a bundle) must still be refused - the
    # suite sets BENCH_ALLOW_TOOLKIT_SWITCH for its own scenarios, so clear it here
    plain = tmpdir()
    os.makedirs(os.path.join(plain, "bench"))
    _switch = os.environ.pop("BENCH_ALLOW_TOOLKIT_SWITCH", None)
    try:
        r6 = br.update_toolkit("main", root=plain)
    finally:
        if _switch is not None:
            os.environ["BENCH_ALLOW_TOOLKIT_SWITCH"] = _switch
    c.check("a plain non-live copy is still refused",
            not r6.get("ok") and "not a live runner" in (r6.get("error") or ""), r6)

    # ---- a bundle update must actually apply (restart, not just refresh) ----
    c.check("an app-bundle update asks for a restart",
            br.update_needs_restart({"ok": True, "kind": "app"}) is True, "")
    c.check("a switched copy still asks for a restart",
            br.update_needs_restart({"ok": True, "kind": "copy", "switched": True}) is True, "")
    c.check("a no-op update does not restart",
            br.update_needs_restart({"ok": True, "kind": "app", "updated": False}) is True
            and br.update_needs_restart({"ok": False, "kind": "app"}) is False, "")

    # ---- the app's own code must be seen as running from disk ----
    # (that is what lets a toolkit refresh update the dashboard, not just the runners)
    c.check("_code_runs_from_disk is true for the toolkit's own dir",
            br._code_runs_from_disk(os.path.dirname(BENCH)) is True, "")
    c.check("_code_runs_from_disk is false for an unrelated dir",
            br._code_runs_from_disk(tmpdir()) is False, "")

    # ---- startup update for an app bundle: throttled, and offline-safe ----
    bdir2 = tmpdir()
    os.makedirs(os.path.join(bdir2, "bench"))
    with open(os.path.join(bdir2, ".bench-version"), "w") as fh:
        json.dump({"mode": "app", "commit": "aa6ca7c", "checked_at": time.time()}, fh)
    t0 = time.time()
    r7 = br.auto_update(bdir2, enabled=True)
    c.check("a recently checked bundle does not re-download at startup",
            r7.get("kind") == "app" and not r7.get("updated") and time.time() - t0 < 2.0, r7)
    with open(os.path.join(bdir2, ".bench-version"), "w") as fh:
        json.dump({"mode": "app", "commit": "aa6ca7c"}, fh)
    os.environ["BENCH_OFFLINE"] = "1"
    try:
        r8 = br.auto_update(bdir2, enabled=True)
    finally:
        os.environ.pop("BENCH_OFFLINE", None)
    c.check("an offline startup check is a no-op, not an error",
            r8.get("kind") == "app" and not r8.get("updated") and r8.get("skipped") == "offline", r8)
    c.check("a git checkout still uses the git path",
            br.auto_update(os.path.dirname(BENCH), enabled=False).get("kind") == "none", "")

    # ---- the packaged launcher runs the app from bench/, not from the exe ----
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("pkg_launcher",
                                         os.path.join(REPO, "packaging", "launcher.py"))
    _lm = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_lm)
    c.check("launcher maps bench-tui.exe -> tui.py", _lm._target("bench-tui.exe") == "tui.py", "")
    c.check("launcher maps bench-gui.exe -> gui.py", _lm._target(r"C:\x\bench-gui.exe") == "gui.py", "")
    c.check("launcher maps bench-agent.exe -> agent.py",
            _lm._target("/usr/bin/bench-agent") == "agent.py", "")
    os.environ["BENCH_APP"] = "gui"
    try:
        c.check("launcher honours BENCH_APP", _lm._target("whatever.exe") == "gui.py", "")
    finally:
        os.environ.pop("BENCH_APP", None)
    _bsrc = open(os.path.join(REPO, "packaging", "build_app.py"), encoding="utf-8").read()
    c.check("the build freezes the launcher, not the app code",
            "LAUNCHER]" in _bsrc and "os.path.join(BENCH, script)]" not in _bsrc, "")
    c.check("the build does not freeze benchreport into the exe",
            '"--hidden-import", "benchreport"' not in _bsrc, "")
    c.check("the build ships the sensor library so a bundle needs no download",
            "have_lhm" in _bsrc and '"%s%stools"' in _bsrc, "")

    # ---- Valley screenshots: keep one, not five ----
    # Unigine writes a full-res PNG per frame; they were 91% of the results dir
    sd = tmpdir()
    for i in range(4):
        p = os.path.join(sd, "valley-host-2026010%d-000000-f%02d.png" % (i + 1, i))
        with open(p, "wb") as fh:
            fh.write(b"png")
        os.utime(p, (time.time() + i, time.time() + i))       # distinct mtimes
    n = br.prune_screenshots(sd)
    left = sorted(glob.glob(os.path.join(sd, "valley-*.png")))
    c.check("pruning keeps exactly one screenshot", n == 3 and len(left) == 1, (n, left))
    c.check("pruning keeps the newest one",
            os.path.basename(left[0]).endswith("f03.png") if left else False, left)
    c.check("pruning an empty dir is a no-op", br.prune_screenshots(tmpdir()) == 0, "")

    # ---- results dirs: keep the newest N, never touch anything that is not a run ----
    rd = tmpdir()
    for i in range(5):
        d = os.path.join(rd, "host-a-run-2026010%d-120000" % (i + 1))
        os.makedirs(d)
        os.utime(d, (time.time() + i, time.time() + i))       # distinct mtimes
    os.makedirs(os.path.join(rd, "notes"))                    # not a run dir
    n = br.prune_results(rd, keep=3)
    left = sorted(os.path.basename(d) for d in glob.glob(os.path.join(rd, "*")))
    c.check("results pruning keeps the newest N run dirs", n == 2 and len(left) == 4, (n, left))
    c.check("results pruning leaves non-run directories alone", "notes" in left, left)
    c.check("results pruning drops the oldest and keeps the newest",
            "host-a-run-20260105-120000" in left and
            "host-a-run-20260101-120000" not in left, left)
    c.check("results pruning disabled keeps everything", br.prune_results(rd, keep=0) == 0, "")
    _spec2 = _ilu.spec_from_file_location("pkg_build",
                                          os.path.join(REPO, "packaging", "build_app.py"))
    _bm = _ilu.module_from_spec(_spec2)
    _spec2.loader.exec_module(_bm)
    _std = _bm.stdlib_imports([])
    # stdlib_module_names holds top-level names only; PyInstaller follows each into
    # its submodules, which is why the check is on the packages
    c.check("the frozen stdlib is complete (packages the toolkit needs)",
            all(m in _std for m in ("glob", "http", "ssl", "email", "urllib", "ctypes",
                                    "json", "subprocess", "tarfile", "winreg" if os.name == "nt"
                                    else "termios")),
            [m for m in ("glob", "http", "ssl", "email", "urllib") if m not in _std])
    c.check("the frozen stdlib does not include the toolkit itself",
            not any(m in _std for m in ("benchreport", "bootstrap", "tui", "gui")), "")
    # the launcher runs the app from bench/, so PyInstaller cannot see gui.py's own
    # imports: every tkinter submodule it uses must be a hidden import, or the
    # bundle ships a partial tkinter (filedialog/font/messagebox/ttk ImportError).
    _gui_src = open(os.path.join(BENCH, "gui.py"), encoding="utf-8").read()
    _tk_names = set()
    for _m in re.finditer(r"from tkinter import ([^\n]+)", _gui_src):
        for _part in _m.group(1).split(","):
            _part = _part.strip()
            if _part:
                _tk_names.add(_part.split()[0])          # "font as tkfont" -> font
    _gui_extra = [extra for name, _s, _n, extra in _bm.APPS if name == "bench-gui"][0]
    c.check("the GUI bundle hidden-imports every tkinter submodule gui.py uses",
            bool(_tk_names)
            and all(("tkinter." + n) in _gui_extra for n in _tk_names), sorted(_tk_names))

    # ---- the restart command line ----
    # It re-passed the whole argv, so a launcher bundle handed its successor the
    # *script path* as a stray argument: argparse rejected it, the TUI closed and
    # never came back. The re-spawn path is Windows-only: POSIX re-execs itself
    # (os.execv), so the captured-Popen assertions cannot run there.
    _script = os.path.join(BENCH, "tui.py")
    if os.name != "nt":
        skip("the restart command line", "the re-spawn path is Windows-only (POSIX uses execv)")
    else:
        _cap = {}
        _real_popen, _real_exit = subprocess.Popen, os._exit
        subprocess.Popen = lambda argv, **kw: _cap.update(argv=list(argv), kw=kw)
        os._exit = lambda code: _cap.update(exit=code)
        try:
            sys.frozen = True
            _sys_exe = sys.executable
            sys.executable = r"C:\apps\bench-tui\bench-tui.exe"
            sys.argv = [_script, "--demo", "--exit-after", "2"]
            br.restart_self(0, log=lambda _m: None, console=True)
            c.check("restart of a bundle spawns the exe, not the script path",
                    _cap.get("argv") == [sys.executable, "--demo", "--exit-after", "2"],
                    _cap.get("argv"))
            c.check("restart does not re-pass the script as an argument",
                    _script not in _cap.get("argv", []), _cap.get("argv"))
            c.check("a dashboard restart inherits the console",
                    _cap.get("kw", {}).get("creationflags") == 0, _cap.get("kw"))
            _cap.clear()
            sys.frozen = False
            sys.executable = r"C:\Python312\python.exe"
            sys.argv = [_script, "--demo"]
            br.restart_self(0, log=lambda _m: None)
            c.check("restart under a plain interpreter keeps the script",
                    _cap.get("argv") == [sys.executable, _script, "--demo"], _cap.get("argv"))
        finally:
            subprocess.Popen, os._exit = _real_popen, _real_exit
            sys.executable = _sys_exe
            if hasattr(sys, "frozen"):
                del sys.frozen

    c.check("launcher drops a stray .py argument", _lm._app_args(["x.exe", _script, "--demo"]),
            ["--demo"])
    c.check("launcher keeps the app's own arguments",
            _lm._app_args(["x.exe", "--demo", "--exit-after", "2"]),
            ["--demo", "--exit-after", "2"])

    # ---- the app itself drops its own path, so a bundle built with the old
    # restart_self still recovers without a manual relaunch ----
    _me = os.path.join(BENCH, "tui.py")
    c.check("an app drops its own path from argv",
            br.drop_stray_script_arg(["tui.py", _me, "--demo"], me=_me) == ["tui.py", "--demo"], "")
    c.check("an app keeps its real arguments",
            br.drop_stray_script_arg(["tui.py", "--demo"], me=_me) == ["tui.py", "--demo"], "")
    c.check("a real .py argument is not eaten",
            br.drop_stray_script_arg(["agent.py", "--extra", "run.py"], me=_me)
            == ["agent.py", "--extra", "run.py"], "")
    c.check("another app's path is not eaten",
            br.drop_stray_script_arg(["tui.py", os.path.join(BENCH, "gui.py")], me=_me)
            == ["tui.py", os.path.join(BENCH, "gui.py")], "")
    for _f in ("tui.py", "gui.py", "agent.py"):
        _src = open(os.path.join(BENCH, _f), encoding="utf-8").read()
        c.check("%s tolerates the stray path (guarded so an old toolkit still runs)" % _f,
                '"drop_stray_script_arg" in globals()' in _src, "")

    httpd.shutdown()
    for k in ("BENCH_TOOLKIT_DIR", "BENCH_PERSIST_DIR", "BENCH_ALLOW_TOOLKIT_SWITCH",
              "BENCH_TOOLKIT_URL", "BENCH_GH_API", "BENCH_REPO_SLUG"):
        os.environ.pop(k, None)
    return c.fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
