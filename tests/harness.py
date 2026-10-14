"""Shared harness for the bench-test test suite.

Starts an in-process benchd on an ephemeral port with a throwaway SQLite db, and
gives a tiny HTTP client. Stdlib only, so it runs on Windows, Linux and the live
USB alike.

    python tests/run_all.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(REPO, "bench")
SERVER = os.path.join(REPO, "server")
WEB = os.path.join(SERVER, "web")
sys.path.insert(0, SERVER)
sys.path.insert(0, BENCH)

TOKEN = "test-token"

# keep the suite offline: the server resolves "latest main" from GitHub otherwise
os.environ.setdefault("BENCH_OFFLINE", "1")

_TMPDIRS = []


def tmpdir(prefix="bench-test-"):
    """mkdtemp that `run_all` cleans up at the end."""
    d = tempfile.mkdtemp(prefix=prefix)
    _TMPDIRS.append(d)
    return d


def cleanup_tmpdirs():
    # the last Server's SQLite connection is held by the global Handler; on
    # Windows an open file cannot be removed, so close it first
    try:
        import benchd
        st = getattr(benchd.Handler, "store", None)
        if st is not None and getattr(st, "conn", None) is not None:
            st.conn.close()
    except Exception:
        pass
    while _TMPDIRS:
        shutil.rmtree(_TMPDIRS.pop(), ignore_errors=True)


class Server:
    def __init__(self, token=TOKEN, rate_limit=False):
        import benchd
        self.benchd = benchd
        self.db = os.path.join(tmpdir(), "benchd.sqlite")
        benchd.Handler.store = benchd.Store(self.db)
        benchd.Handler.bus = benchd.Bus()
        benchd.Handler.token = token
        benchd.Handler.web_dir = WEB
        self.prev_token = token  # Handler.token is process-global; restored on stop()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), benchd.Handler)
        # The suite makes many register/auth calls from one IP; every server gets a
        # permissive bucket unless a test asks for the shipped defaults.
        self.httpd._bench_rate = (
            benchd.RateLimiter() if rate_limit
            else benchd.RateLimiter({"auth": (10 ** 6, 60.0), "register": (10 ** 6, 60.0)}))
        self.httpd.quiet = True
        self.httpd.daemon_threads = True
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.token = token

    def stop(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        finally:
            self._thread.join(timeout=5)

    def client(self):
        return Client(self.base, self.token)


class Client:
    def __init__(self, base, token):
        self.base = base
        self.token = token

    def call(self, method, path, body=None, token="__default__", raw=False):
        req = urllib.request.Request(self.base + path, method=method)
        t = self.token if token == "__default__" else token
        if t:
            req.add_header("Authorization", "Bearer " + t)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, data, timeout=10) as r:
                b = r.read()
                return r.status, (b if raw else json.loads(b.decode() or "{}"))
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode() or "{}")
            except Exception:
                return e.code, {}


class Checker:
    def __init__(self, name):
        self.name = name
        self.fails = []

    def check(self, label, cond, extra=""):
        print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else "  -> " + str(extra)[:200]))
        if not cond:
            self.fails.append("%s: %s" % (self.name, label))
        return cond
