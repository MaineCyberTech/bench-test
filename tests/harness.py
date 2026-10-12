"""Shared harness for the bench-test test suite.

Starts an in-process benchd on an ephemeral port with a throwaway SQLite db, and
gives a tiny HTTP client. Stdlib only, so it runs on Windows, Linux and the live
USB alike.

    python tests/run_all.py
"""
import json
import os
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


class Server:
    def __init__(self, token=TOKEN):
        import benchd
        self.benchd = benchd
        self.db = os.path.join(tempfile.mkdtemp(), "benchd.sqlite")
        benchd.Handler.store = benchd.Store(self.db)
        benchd.Handler.bus = benchd.Bus()
        benchd.Handler.token = token
        benchd.Handler.web_dir = WEB
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), benchd.Handler)
        self.httpd.quiet = True
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.token = token

    def stop(self):
        self.httpd.shutdown()

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
