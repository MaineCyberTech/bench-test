#!/usr/bin/env python3
"""Live-runner self-update: refresh the toolkit and stage ISOs without reflashing.

The live USB boots a read-only ISO, so "updating" means pointing the toolkit at a
copy on the stick's writable partition. Kernel/driver changes still need a new
image, which this can download, verify and (to another device) flash.

    python bench/live_update.py toolkit [--ref <sha|tag|branch>] [--sha256 H]
    python bench/live_update.py boot-link
    python bench/live_update.py iso-list <tag>
    python bench/live_update.py iso-fetch <tag> [--gpu nvidia-legacy] [--dest DIR]
    python bench/live_update.py iso-write <iso> --device /dev/sdX --yes

The dashboards and the agent expose the same operations as control-plane
commands (`update_toolkit`, `iso_fetch`, `iso_write`).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from benchreport import (iso_fetch, iso_write, iso_write_slot, toolkit_boot_link,  # noqa: E402
                         update_toolkit, gh_release, ensure_torch_packages)


def _log(m):
    print(m, flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("toolkit", help="fetch the toolkit and switch the runner onto it")
    t.add_argument("--ref", default="main")
    t.add_argument("--sha256", default=None, help="expected tarball hash (optional)")
    t.add_argument("--force", action="store_true", help="allow switching a non-live install")

    sub.add_parser("boot-link", help="re-point the toolkit at the persisted copy")

    sub.add_parser("torch-link", help="keep the image's PyTorch importable after a re-point")

    ls = sub.add_parser("iso-list", help="list a release's assets")
    ls.add_argument("tag")

    f = sub.add_parser("iso-fetch", help="download + verify a release ISO")
    f.add_argument("tag")
    f.add_argument("--gpu", default="nvidia-legacy")
    f.add_argument("--dest", default=None)

    ws = sub.add_parser("iso-write-slot", help="A/B update: write the inactive slot + flip the boot flag")
    ws.add_argument("iso")
    ws.add_argument("--yes", action="store_true")

    w = sub.add_parser("iso-write", help="write a staged ISO to a block device")
    w.add_argument("iso")
    w.add_argument("--device", required=True)
    w.add_argument("--yes", action="store_true", help="confirm the destructive write")

    a = ap.parse_args()

    if a.cmd == "toolkit":
        if a.force:
            os.environ["BENCH_ALLOW_TOOLKIT_SWITCH"] = "1"
        return 0 if update_toolkit(a.ref, a.sha256, log=_log).get("ok") else 1
    if a.cmd == "boot-link":
        return 0 if toolkit_boot_link(log=_log) else 1
    if a.cmd == "torch-link":
        return 0 if ensure_torch_packages(log=_log) else 1
    if a.cmd == "iso-list":
        rel, err = gh_release(a.tag)
        if err:
            print(err)
            return 1
        print("release %s" % rel["tag"])
        for x in rel["assets"]:
            print("  %-52s %10d B" % (x["name"], x["size"] or 0))
        return 0
    if a.cmd == "iso-fetch":
        r = iso_fetch(a.tag, a.gpu, a.dest, log=_log)
        print(r)
        return 0 if r.get("ok") else 1
    if a.cmd == "iso-write":
        r = iso_write(a.iso, a.device, confirm=a.yes, log=_log)
        print(r)
        return 0 if r.get("ok") else 1
    if a.cmd == "iso-write-slot":
        r = iso_write_slot(a.iso, confirm=a.yes, log=_log)
        print(r)
        return 0 if r.get("ok") else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
