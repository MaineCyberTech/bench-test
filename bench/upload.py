#!/usr/bin/env python3
"""Upload a bench-test results directory into a GitHub repository.

Runs on the live USB (system python) and on the desktop. Uses only stdlib.

Config (first existing file wins):
  $BENCH_UPLOAD_CONF, /mnt/benchdata/bench-upload.conf, /etc/bench-upload.conf,
  ~/.config/bench-upload.conf

Config format (key=value, one per line):
  GITHUB_TOKEN=github_pat_xxx      fine-grained PAT with Contents: Read/Write
  GITHUB_REPO=MaineCyberTech/bench-test
  GITHUB_BRANCH=main               (default: main)
  UPLOAD_PATH=results              (default: results)
  AUTO_UPLOAD=1                    (optional: dashboards upload after collect)

Consent: publishing results to a remote repository requires an explicit opt-in.
Set BENCH_UPLOAD_CONSENT=1 (the TUI's upload key sets it for the session), or
the upload is skipped with a clear log line. Nothing is published by default.

Usage: upload.py [RESULTS_DIR]     (default: /opt/bench-test/results)

Exit codes: 0 all good, 3 missing consent/config/dir/nothing to send, 5 total failure.
"""
import base64
import glob
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
# Files small enough to inline in the tree request (saves one API call each) and
# the extensions that are safely text - binary (screenshots) still goes via blobs.
INLINE_MAX = 512 * 1024
INLINE_EXTS = {".log", ".txt", ".json", ".jsonl", ".md", ".csv", ".out", ".err",
               ".yaml", ".yml", ".conf", ".html", ".js", ".css", ".py", ".sh"}


def find_conf():
    cands = [
        os.environ.get("BENCH_UPLOAD_CONF"),
        "/mnt/benchdata/bench-upload.conf",
        "/etc/bench-upload.conf",
        os.path.expanduser("~/.config/bench-upload.conf"),
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def read_conf(path):
    conf = {}
    with open(path) as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            conf[k.strip()] = v.strip().strip('"').strip("'")
    return conf


def consent_granted(env=None):
    """True when the operator has explicitly opted in to GitHub uploads.

    Publishing a run to a remote repository used to happen with no consent.
    Uploads now require ``BENCH_UPLOAD_CONSENT=1`` (the TUI's upload key sets it
    for the session); without it ``main()`` skips the upload and says why.
    """
    e = env if env is not None else os.environ
    return str(e.get("BENCH_UPLOAD_CONSENT", "")).strip().lower() in ("1", "true", "yes", "on")


def api(method, url, token, data=None):
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "bench-test-upload")
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, body, timeout=60) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get("message", str(e))
        except Exception:
            msg = str(e)
        return e.code, {"message": msg}
    except Exception as e:
        return 0, {"message": str(e)}


def upload_as_one_commit(repo, branch, upath, folder, files, token, log=print, attempts=5):
    """Put every file in ONE commit via the Git Data API (blobs -> tree -> commit).

    The Contents API makes a commit per file, which turned a single run into
    hundreds of commits on main (165 arrived in three minutes) and made every
    other push lose a race with the uploader.

    The whole flow is retried: the final ref PATCH loses to a concurrent push
    (another runner uploading, or an operator pushing) and a fresh base fixes it.
    Falling straight back to per-file commits is what floods main, so give the
    batched path a real chance first.
    """
    for i in range(max(1, attempts)):
        if _one_commit_once(repo, branch, upath, folder, files, token, log):
            return True
        if i + 1 < attempts:
            time.sleep(0.5 * (i + 1))
    log("[upload] one-commit upload failed after %d attempts" % attempts)
    return False


def _one_commit_once(repo, branch, upath, folder, files, token, log=print):
    st, ref = api("GET", "%s/repos/%s/git/ref/heads/%s" % (API, repo, branch), token)
    sha = (ref or {}).get("object", {}).get("sha")
    if st != 200 or not sha:
        return False
    st, commit = api("GET", "%s/repos/%s/git/commits/%s" % (API, repo, sha), token)
    base_tree = (commit or {}).get("tree", {}).get("sha")
    if not base_tree:
        return False
    entries = []
    for p, rel in sorted(files):
        try:
            with open(p, "rb") as fh:
                raw = fh.read()
        except Exception as e:
            log("  skip %s: read: %s" % (rel, e))
            continue
        path = "%s/%s/%s" % (upath, folder, rel.replace(os.sep, "/"))
        ext = os.path.splitext(rel)[1].lower()
        if ext in INLINE_EXTS and len(raw) <= INLINE_MAX:
            # Inline the content in the tree instead of POSTing a blob per file.
            # A run uploads 60+ files; that many rapid blob calls trips GitHub's
            # secondary rate limit, the batched path gives up, and the per-file
            # fallback then commits them one at a time - which floods main.
            entries.append({"path": path, "mode": "100644", "type": "blob",
                            "content": raw.decode("utf-8", "replace")})
        else:
            st, blob = api("POST", "%s/repos/%s/git/blobs" % (API, repo), token,
                           {"content": base64.b64encode(raw).decode(), "encoding": "base64"})
            if st not in (200, 201) or not blob.get("sha"):
                return False
            entries.append({"path": path, "mode": "100644", "type": "blob",
                            "sha": blob["sha"]})
    if not entries:
        return False
    st, tree = api("POST", "%s/repos/%s/git/trees" % (API, repo), token,
                   {"base_tree": base_tree, "tree": entries})
    if st not in (200, 201) or not tree.get("sha"):
        return False
    st, newc = api("POST", "%s/repos/%s/git/commits" % (API, repo), token,
                   {"message": "bench: %s (%d files)" % (folder, len(entries)),
                    "tree": tree["sha"], "parents": [sha]})
    if st not in (200, 201) or not newc.get("sha"):
        return False
    st, _res = api("PATCH", "%s/repos/%s/git/refs/heads/%s" % (API, repo, branch), token,
                   {"sha": newc["sha"]})
    if st not in (200, 201):
        return False
    log("[upload] 1 commit, %d files -> %s@%s" % (len(entries), repo, branch))
    return True


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    src = args[0] if args else os.environ.get("BENCH_RESULTS_DIR", "/opt/bench-test/results")

    # explicit consent before anything is published (AN-P2-002)
    if not consent_granted():
        print("[upload] skipped: no consent. Results were NOT published to GitHub.")
        print("[upload] set BENCH_UPLOAD_CONSENT=1 (the dashboard's upload key does "
              "this for the session) to allow it")
        return 3

    if not os.path.isdir(src):
        print("[upload] results dir not found: %s" % src)
        return 3

    conf_path = find_conf()
    if not conf_path:
        print("[upload] no config found - create /mnt/benchdata/bench-upload.conf")
        print("[upload] (GITHUB_TOKEN=... / GITHUB_REPO=owner/name, see bench/upload.py header)")
        return 3
    conf = read_conf(conf_path)
    token = conf.get("GITHUB_TOKEN") or os.environ.get("BENCH_GITHUB_TOKEN", "")
    repo = conf.get("GITHUB_REPO", "")
    branch = conf.get("GITHUB_BRANCH", "main")
    upath = conf.get("UPLOAD_PATH", "results").strip("/")
    if not token or "/" not in repo:
        print("[upload] %s must set GITHUB_TOKEN and GITHUB_REPO=owner/name" % conf_path)
        return 3

    folder = os.path.basename(src.rstrip("/"))
    if folder in ("results", ""):
        # uploads of a live-session results dir: name the target after the run
        cands = sorted(glob.glob(os.path.join(src, "*-linux-*.json")), key=os.path.getmtime)
        if cands:
            folder = os.path.basename(cands[-1])[:-5]
    files = []
    for root, _dirs, names in os.walk(src):
        for n in names:
            p = os.path.join(root, n)
            files.append((p, os.path.relpath(p, src)))
    if not files:
        print("[upload] nothing to upload in %s" % src)
        return 3

    print("[upload] %d files -> %s@%s:%s/%s (config: %s)"
          % (len(files), repo, branch, upath, folder, conf_path))

    ok = 0
    fails = []
    if upload_as_one_commit(repo, branch, upath, folder, files, token):
        print("[upload] done -> https://github.com/%s/tree/%s/%s/%s"
              % (repo, branch, upath, folder))
        return 0
    print("[upload] one-commit upload unavailable - falling back to per-file commits")
    for p, rel in sorted(files):
        target = "%s/%s/%s" % (upath, folder, rel.replace(os.sep, "/"))
        url = "%s/repos/%s/contents/%s" % (API, repo, urllib.parse.quote(target))
        try:
            with open(p, "rb") as fh:
                content = base64.b64encode(fh.read()).decode()
        except Exception as e:
            fails.append((rel, "read: %s" % e))
            continue
        payload = {"message": "bench: %s (%s)" % (rel, folder),
                   "content": content, "branch": branch}
        status, res = api("PUT", url, token, payload)
        if status == 422 and "sha" in str(res.get("message", "")):
            s2, r2 = api("GET", url + "?ref=" + urllib.parse.quote(branch), token)
            if r2.get("sha"):
                payload["sha"] = r2["sha"]
                status, res = api("PUT", url, token, payload)
        if status in (200, 201):
            ok += 1
            print("  ok  %s" % target)
        else:
            fails.append((rel, res.get("message", "HTTP %s" % status)))

    print("[upload] uploaded %d/%d files" % (ok, len(files)))
    for rel, why in fails[:6]:
        print("  FAIL %s: %s" % (rel, why))
    if fails:
        print("[upload] %d file(s) failed" % len(fails))
        return 0 if ok else 5
    print("[upload] done -> https://github.com/%s/tree/%s/%s/%s" % (repo, branch, upath, folder))
    return 0


if __name__ == "__main__":
    sys.exit(main())
