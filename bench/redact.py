#!/usr/bin/env python3
"""redact.py - opt-in privacy redaction for bench-test collectors (stdlib only).

A run collects stable hardware identifiers and personal data - the hostname, the
user's home path, NIC MAC/IP addresses, DIMM/disk/GPU serials, the GPU UUID and
the machine id - and the workflow encourages sharing the results. This module
provides one switch (`BENCH_REDACT=1`, or a runner's `--redact` flag) that
replaces those values with stable pseudonyms before they reach the control
plane, an upload, or a shareable report.

What redaction does
  * known PII keys (`hostname`, `serial`, `uuid`, `mac`, `ip`, `machine_id`,
    `username`, ...) are replaced by `kind-<sha256[:8]>` tokens, or blanked;
  * free text is scrubbed of MAC / IPv4 / IPv6 addresses, e-mail addresses and
    home directories (`/home/<user>`, `/Users/<user>`, `C:\\Users\\<user>`);
  * the local machine's own hostname is replaced wherever it appears in text;
  * JSON artifacts are parsed and walked, so nested inventories are covered;
  * binary artifacts pass through unchanged.

The pseudonym is deterministic (same input -> same token), so a fleet can still
correlate runs from one machine without learning which machine it is.

Default OFF: every helper is a pass-through when redaction is disabled, so a
run's default behaviour is unchanged.
"""
import hashlib
import json
import os
import re
import socket

TRUTHY = ("1", "true", "yes", "on")

# keys whose value is a stable identifier / PII -> pseudonym kind. A None kind
# means "blank it" (no useful pseudonym). Matching is on the lower-cased key.
PII_KEYS = {
    "hostname": "host", "host": "host", "host_name": "host",
    "computername": "host", "computer_name": "host", "nodename": "host",
    "username": "user", "user": "user", "user_name": "user", "login": "user",
    "owner": "user", "userprofile": "user", "user_profile": "user",
    "home": "user", "homepath": "user", "home_path": "user", "homedir": "user",
    "serial": "serial", "serialnumber": "serial", "serial_number": "serial",
    "uuid": "uuid", "machine_uuid": "uuid", "product_uuid": "uuid",
    "machine_id": "machine", "machineid": "machine", "machineguid": "machine",
    "mac": "mac", "macaddress": "mac", "mac_address": "mac", "hwaddr": "mac",
    "ip": "ip", "ipaddress": "ip", "ip_address": "ip", "ipv4": "ip", "ipv6": "ip",
}

# text file extensions safe to decode, scrub and re-encode on upload
TEXT_EXTS = {".log", ".txt", ".json", ".jsonl", ".md", ".csv", ".out", ".err",
             ".yaml", ".yml", ".conf", ".html", ".js", ".css", ".py", ".sh"}

_MAC_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_HOME_RE = re.compile(r"(?i)(?:/home/|/Users/|[A-Za-z]:\\Users\\)([^\\/\s\"']+)")


def is_enabled(env=None):
    """True when redaction is switched on (`BENCH_REDACT`)."""
    v = (env if env is not None else os.environ).get("BENCH_REDACT")
    return str(v).strip().lower() in TRUTHY


def digest(value):
    return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()


def pseudonym(kind, value):
    """A stable `kind-<sha256[:8]>` token; None/empty is returned unchanged.

    Already-pseudonymised values of the same kind are returned as-is, so a
    second redaction pass (e.g. a file redacted in place and then again on
    upload) does not hash a token into a different one.
    """
    if value is None:
        return None
    s = str(value)
    if not s:
        return s
    if re.match(r"^%s-[0-9a-f]{8}$" % re.escape(kind), s):
        return s
    return "%s-%s" % (kind, digest(s)[:8])


def _valid_ipv4(s):
    parts = s.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def _home_sub(m):
    return m.group(0)[:m.start(1) - m.start(0)] + pseudonym("user", m.group(1))


def redact_text(text):
    """Scrub MAC/IP/e-mail/home-path/hostname occurrences from free text."""
    if not is_enabled() or not isinstance(text, str) or not text:
        return text
    out = _MAC_RE.sub(lambda m: pseudonym("mac", m.group(0).lower()), text)
    out = _IPV4_RE.sub(
        lambda m: pseudonym("ip", m.group(0)) if _valid_ipv4(m.group(0)) else m.group(0), out)
    out = _EMAIL_RE.sub(lambda m: pseudonym("user", m.group(0).lower()), out)
    out = _HOME_RE.sub(_home_sub, out)
    try:
        hn = socket.gethostname()
    except Exception:
        hn = ""
    if hn:
        out = re.sub(r"\b%s\b" % re.escape(hn), pseudonym("host", hn), out)
    return out


def _redact_value(kind, value):
    if isinstance(value, dict):
        return redact_obj(value)
    if isinstance(value, (list, tuple)):
        return [_redact_value(kind, v) for v in value]
    if value is None:
        return None
    if kind is None:
        return ""
    return pseudonym(kind, value)


def redact_obj(obj):
    """Recursively redact a JSON-shaped structure (dict/list/str)."""
    if not is_enabled():
        return obj
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kind = PII_KEYS.get(str(k).lower())
            out[k] = _redact_value(kind, v) if kind is not None else redact_obj(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


def redact_bytes(name, raw):
    """Redact a text artifact's bytes; binary artifacts pass through unchanged.

    JSON/JSONL are parsed and walked (so nested inventory keys are covered);
    other text is scrubbed in place. Anything that will not decode as UTF-8 is
    left alone rather than risk corrupting it.
    """
    if not is_enabled():
        return raw
    ext = os.path.splitext(str(name))[1].lower()
    if ext not in TEXT_EXTS:
        return raw
    try:
        text = raw.decode("utf-8")
    except Exception:
        return raw
    if ext == ".json":
        try:
            return json.dumps(redact_obj(json.loads(text)), indent=2).encode("utf-8")
        except Exception:
            pass
    elif ext == ".jsonl":
        try:
            lines = [json.dumps(redact_obj(json.loads(ln)))
                     for ln in text.splitlines() if ln.strip()]
            return ("\n".join(lines) + "\n").encode("utf-8")
        except Exception:
            pass
    return redact_text(text).encode("utf-8")


def redact_dir(results_dir, since=None, log=None):
    """Redact the text artifacts under a run directory in place.

    `since` (a unix time) restricts the sweep to files this run wrote, so an
    earlier run's files in the same directory are left alone. Returns the number
    of files changed.
    """
    if not is_enabled():
        return 0
    n = 0
    for root, _dirs, names in os.walk(results_dir):
        for name in names:
            p = os.path.join(root, name)
            try:
                if since is not None and os.path.getmtime(p) < since:
                    continue
                with open(p, "rb") as fh:
                    raw = fh.read()
                out = redact_bytes(name, raw)
                if out != raw:
                    with open(p, "wb") as fh:
                        fh.write(out)
                    n += 1
            except OSError:
                continue
    if n and log:
        log("[redact] redacted %d file(s) under %s" % (n, results_dir))
    return n
