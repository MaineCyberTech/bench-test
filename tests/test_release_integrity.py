"""Release/download integrity tests (stdlib only, fully offline).

Locks in the fail-closed contract of `iso_fetch()`: a release whose SHA256SUMS is
missing, unreadable, mismatched, or lacks an `.iso` entry must never report `ok`
and must not leave a staged ISO behind for a later destructive `iso_write` to
accept. `gh_release`/`_download` are stubbed, so nothing touches the network.
"""
import hashlib
import os

from harness import Checker, tmpdir
import benchreport as br


def run():
    c = Checker("release_integrity")
    sand = tmpdir()

    saved_rel, saved_dl = br.gh_release, br._download

    body = b"A" * 512 + b"B" * 256
    parts = {"part.a": body[:512], "part.b": body[512:]}
    base_assets = [
        {"name": "bench-live-nvidia-legacy.iso.part.a", "url": "part.a", "size": 512},
        {"name": "bench-live-nvidia-legacy.iso.part.b", "url": "part.b", "size": 256},
    ]

    def _release(assets):
        def _rel(tag):
            return {"tag": tag, "assets": assets}, None
        return _rel

    def _download_from(sums):
        """Stub _download: parts by URL, SHA256SUMS from `sums` (an Exception raises)."""
        def _dl(url, dest, log=None, timeout=180, every=100 << 20):
            if url == "sums":
                if isinstance(sums, Exception):
                    raise sums
                data = sums.encode()
            else:
                data = parts[url]
            if hasattr(dest, "write"):
                dest.write(data)
            else:
                with open(dest, "wb") as fh:
                    fh.write(data)
            return len(data)
        return _dl

    def _with_sums(text):
        return list(base_assets) + [{"name": "SHA256SUMS", "url": "sums", "size": len(text)}]

    good = "%s  bench-live-nvidia-legacy.iso\n" % hashlib.sha256(body).hexdigest()

    try:
        # ---- the happy path still verifies and keeps the ISO ----
        br.gh_release = _release(_with_sums(good))
        br._download = _download_from(good)
        ok = br.iso_fetch("v1", "nvidia-legacy", dest=os.path.join(sand, "ok"))
        c.check("verified fetch reports ok", ok.get("ok") is True, ok)
        c.check("verified fetch reports verified", ok.get("verified") is True, ok)
        c.check("verified fetch leaves the ISO", os.path.isfile(ok.get("iso", "")), ok)

        # ---- a mismatched sum fails closed and removes the ISO ----
        bad = "%s  bench-live-nvidia-legacy.iso\n" % ("0" * 64)
        br.gh_release = _release(_with_sums(bad))
        br._download = _download_from(bad)
        d2 = os.path.join(sand, "bad")
        r2 = br.iso_fetch("v1", "nvidia-legacy", dest=d2)
        iso2 = os.path.join(d2, "bench-live-nvidia-legacy.iso")
        c.check("mismatched sum fails closed (ok False)", r2.get("ok") is False, r2)
        c.check("mismatched sum reports unverified", r2.get("verified") is False, r2)
        c.check("mismatched sum names the error", "mismatch" in str(r2.get("error")), r2)
        c.check("mismatched sum removes the staged ISO", not os.path.isfile(iso2), iso2)
        c.check("iso_write refuses the rejected ISO",
                "no such iso" in str(br.iso_write(iso2, "/dev/null", confirm=True).get("error")), "")

        # ---- a release with no SHA256SUMS asset fails closed ----
        br.gh_release = _release(list(base_assets))
        br._download = _download_from(good)
        d3 = os.path.join(sand, "nosums")
        r3 = br.iso_fetch("v1", "nvidia-legacy", dest=d3)
        iso3 = os.path.join(d3, "bench-live-nvidia-legacy.iso")
        c.check("missing SHA256SUMS fails closed", r3.get("ok") is False, r3)
        c.check("missing SHA256SUMS names the error", "SHA256SUMS" in str(r3.get("error")), r3)
        c.check("missing SHA256SUMS removes the staged ISO", not os.path.isfile(iso3), iso3)

        # ---- an unreadable SHA256SUMS fails closed ----
        br.gh_release = _release(_with_sums(good))
        br._download = _download_from(IOError("boom"))
        d4 = os.path.join(sand, "sumerr")
        r4 = br.iso_fetch("v1", "nvidia-legacy", dest=d4)
        iso4 = os.path.join(d4, "bench-live-nvidia-legacy.iso")
        c.check("unreadable SHA256SUMS fails closed", r4.get("ok") is False, r4)
        c.check("unreadable SHA256SUMS removes the staged ISO", not os.path.isfile(iso4), iso4)

        # ---- a SHA256SUMS with no .iso entry fails closed ----
        noentry = "%s  some-other-file.tar.gz\n" % ("a" * 64)
        br.gh_release = _release(_with_sums(noentry))
        br._download = _download_from(noentry)
        d5 = os.path.join(sand, "noentry")
        r5 = br.iso_fetch("v1", "nvidia-legacy", dest=d5)
        iso5 = os.path.join(d5, "bench-live-nvidia-legacy.iso")
        c.check("SHA256SUMS without an .iso entry fails closed", r5.get("ok") is False, r5)
        c.check("no .iso entry removes the staged ISO", not os.path.isfile(iso5), iso5)
    finally:
        br.gh_release, br._download = saved_rel, saved_dl

    return c.fails


if __name__ == "__main__":
    import sys
    sys.exit(1 if run() else 0)
