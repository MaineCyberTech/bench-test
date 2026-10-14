"""Wave W25a API contract tests (API-P3-003 / EVOL-P3-001).

benchd publishes a machine-readable contract (``docs/openapi.json``) and declares
its HTTP surface (``benchd.API_ROUTES``) and command catalogue
(``benchd.COMMAND_KINDS``). This module keeps the three honest:

  * every documented path+method is in ``API_ROUTES``;
  * every ``API_ROUTES`` entry is documented (an explicit, commented exceptions
    list covers routes deliberately kept out of the published contract);
  * every route the dispatcher actually matches appears in ``API_ROUTES``, so a
    new route added to the code cannot be left unregistered/undocumented;
  * a live request to each documented route is answered by the dispatcher (not the
    generic unmatched-route 404);
  * the command catalogue is exposed read-only, matches ``COMMAND_KINDS``, and the
    destructive confirm gate follows it.

Stdlib only, reuse the shared harness.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

from harness import Checker, REPO, Server, TOKEN

DOC = os.path.join(REPO, "docs", "openapi.json")

# Routes deliberately absent from the published OpenAPI document (none today).
# Anything listed here must also be flagged ``internal`` in benchd.API_ROUTES.
INTERNAL_EXCEPTIONS = ()

# The SSE stream never ends, so the behavioural probe cannot read it; the route
# is still checked against the registry and the dispatcher source.
PROBE_SKIP = {("GET", "/api/v1/stream")}

_METHODS = ("get", "post", "delete")
_PARAM_RE = re.compile(r"\{[^}]+\}")
_PARAM_GROUP_RE = re.compile(r"\(\[\^/\]\+\)")


def _canon(path):
    """Normalise a path template to its placeholder-free shape."""
    return _PARAM_RE.sub("{}", path)


def _regex_to_template(rx):
    """Turn an anchored dispatch regex into a templated path."""
    rx = rx.strip("^$")
    rx = _PARAM_GROUP_RE.sub("{}", rx)
    return rx.replace("\\.", ".")


def _doc_routes(doc):
    """{(METHOD, canonical path): operation} plus the original templates."""
    routes, templates = {}, {}
    for path, ops in doc.get("paths", {}).items():
        for method in _METHODS:
            if method in ops:
                key = (method.upper(), _canon(path))
                routes[key] = ops[method]
                templates[key] = path
    return routes, templates


def _reg_routes(benchd):
    return {(r["method"], _canon(r["path"])): r for r in benchd.API_ROUTES}


def _source_routes(src):
    """(method, canonical path) for every route the dispatcher actually matches.

    do_GET/_api_get -> GET, do_POST/_api_post -> POST, do_DELETE -> DELETE. Only
    real route tests are read: ``path == "LITERAL"`` and anchored inline
    ``re.match(r"^...$", path)``. A non-capturing group (``(?:...))`` is an
    ownership pre-check, not a dispatch, so it is skipped.
    """
    method_of = {"do_GET": "GET", "_api_get": "GET",
                 "do_POST": "POST", "_api_post": "POST",
                 "do_DELETE": "DELETE"}
    cur = None
    out = set()
    for line in src.splitlines():
        m = re.match(r"\s*def (\w+)\(", line)
        if m:
            cur = m.group(1)
            continue
        if cur not in method_of:
            continue
        for lit in re.findall(r'path == "([^"]+)"', line):
            out.add((method_of[cur], _canon(lit)))
        for rx in re.findall(r're\.match\(r"(\^[^"]+)"', line):
            if "(?:" in rx:
                continue
            out.add((method_of[cur], _canon(_regex_to_template(rx))))
    return out


def run():
    c = Checker("contract")
    import benchd
    B = benchd

    with open(DOC, encoding="utf-8") as fh:
        doc = json.load(fh)
    with open(B.__file__, encoding="utf-8") as fh:
        src = fh.read()

    # ---- the document itself ----
    c.check("the OpenAPI document is 3.0",
            str(doc.get("openapi", "")).startswith("3.0"), doc.get("openapi"))
    c.check("the OpenAPI document declares servers",
            isinstance(doc.get("servers"), list) and bool(doc["servers"]),
            doc.get("servers"))

    doc_routes, doc_templates = _doc_routes(doc)
    reg_routes = _reg_routes(B)
    src_routes = _source_routes(src)

    # ---- documented <=> API_ROUTES ----
    missing_reg = sorted(k for k in doc_routes if k not in reg_routes)
    c.check("every documented route is in API_ROUTES", not missing_reg, missing_reg)
    undoc = sorted(k for k, r in reg_routes.items()
                   if not r.get("internal") and k not in INTERNAL_EXCEPTIONS
                   and k not in doc_routes)
    c.check("every API_ROUTES entry is documented", not undoc, undoc)

    # ---- the internal flag and the exceptions list agree ----
    flagged = {k for k, r in reg_routes.items() if r.get("internal")}
    c.check("every internal route is in the exceptions list",
            flagged <= set(INTERNAL_EXCEPTIONS), sorted(flagged))
    c.check("every exception is flagged internal in API_ROUTES",
            set(INTERNAL_EXCEPTIONS) <= flagged, sorted(INTERNAL_EXCEPTIONS))

    # ---- the dispatcher <=> API_ROUTES (a new route cannot go unregistered) ----
    dispatch_only = sorted(src_routes - set(reg_routes))
    c.check("every dispatched route is in API_ROUTES", not dispatch_only, dispatch_only)
    registry_only = sorted(set(reg_routes) - src_routes)
    c.check("every API_ROUTES entry is dispatched", not registry_only, registry_only)

    # ---- auth expectations are declared and agree with the registry ----
    for key in sorted(set(doc_routes) & set(reg_routes)):
        want = reg_routes[key]["auth"]
        got = doc_routes[key].get("x-required-role")
        c.check("documented %s %s declares auth %s" % (key[0], key[1], want),
                got == want, (got, want))

    # ---- behavioural: the live dispatcher really serves each documented route ----
    srv = Server()
    cl = srv.client()
    st, reg = cl.call("POST", "/api/v1/register", {"name": "contract", "kind": "agent"})
    rid, key = reg.get("runner_id"), reg.get("api_key")
    c.check("registered a runner for the probes", st == 200 and rid and key, reg)
    st, rr = cl.call("POST", "/api/v1/runs", {"runner_id": rid, "host": "contract"},
                     token=key)
    run_id = rr.get("run_id")
    c.check("opened a run for the probes", st == 200 and run_id, rr)
    subs = {"runner_id": rid or "nope", "run_id": run_id or "nope",
            "command_id": "nope", "schedule_id": "nope", "name": "nope.bin"}

    def probe(method, template):
        table = dict(subs)
        if method == "DELETE":  # never remove the real probe rows
            for k in ("runner_id", "run_id", "schedule_id"):
                table[k] = "gone"
        path = template
        for name, val in table.items():
            path = path.replace("{%s}" % name, val)
        data = b"{}" if method in ("POST", "DELETE") else None
        req = urllib.request.Request(srv.base + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + TOKEN)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    for key in sorted(doc_routes):
        if key in PROBE_SKIP:
            continue
        method, canon = key
        status, raw = probe(method, doc_templates[key])
        # the generic dispatch miss is the only 404 that says exactly "not found"
        miss = status == 404 and b'"not found"' in raw and b"no such" not in raw
        c.check("the dispatcher serves %s %s" % (method, canon),
                not miss, (status, raw[:100]))

    # ---- EVOL-P3-001: the command catalogue ----
    st, body = cl.call("GET", "/api/v1/commands")
    kinds = body.get("commands") if isinstance(body, dict) else None
    c.check("GET /api/v1/commands returns the catalogue",
            st == 200 and isinstance(kinds, list), (st, body))
    by_kind = {k.get("kind"): k for k in (kinds or [])}
    c.check("the catalogue matches COMMAND_KINDS",
            set(by_kind) == set(B.COMMAND_KINDS),
            (sorted(by_kind), sorted(B.COMMAND_KINDS)))
    for kind, spec in B.COMMAND_KINDS.items():
        row = by_kind.get(kind, {})
        c.check("command %s exposes its flags" % kind,
                row.get("destructive") == spec["destructive"]
                and row.get("needs_confirm") == spec["needs_confirm"]
                and bool(row.get("description")), row)

    st, state = cl.call("GET", "/api/v1/state")
    c.check("/state exposes command_kinds read-only",
            isinstance(state.get("command_kinds"), list)
            and {k.get("kind") for k in state["command_kinds"]} == set(B.COMMAND_KINDS),
            state.get("command_kinds"))

    # the published catalogue is a copy: mutating it cannot change the registry
    pub = B.command_kinds_public()
    pub[0]["kind"] = "hacked"
    pub[0]["destructive"] = True
    c.check("the published catalogue is read-only",
            "hacked" not in B.COMMAND_KINDS
            and B.COMMAND_KINDS[tuple(B.COMMAND_KINDS)[0]]["destructive"] is False, "")

    c.check("DESTRUCTIVE_COMMANDS is derived from needs_confirm",
            set(B.DESTRUCTIVE_COMMANDS)
            == {k for k, v in B.COMMAND_KINDS.items() if v["needs_confirm"]},
            B.DESTRUCTIVE_COMMANDS)
    for kind, spec in B.COMMAND_KINDS.items():
        if spec["needs_confirm"]:
            st, body = cl.call("POST", "/api/v1/runners/%s/commands" % rid, {"kind": kind})
            c.check("%s without confirm -> 400" % kind, st == 400, (st, body))
            st, body = cl.call("POST", "/api/v1/runners/%s/commands" % rid,
                               {"kind": kind, "payload": {"confirm": True}})
            c.check("%s with confirm -> 200" % kind, st == 200, (st, body))
        else:
            st, body = cl.call("POST", "/api/v1/runners/%s/commands" % rid, {"kind": kind})
            c.check("%s needs no confirm" % kind, st == 200, (st, body))

    srv.stop()
    return c.fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
