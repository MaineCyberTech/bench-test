#!/usr/bin/env python3
"""Recover a raw log slot written by bench-collect v4 when nothing could be
mounted. The archive (BENCHLOG1 magic + size + tar.gz) sits in the last
64 MiB of the partition - free-cluster space of the FAT, so reading it back
does not need the filesystem either.

Usage: rawlog.py <partition-device> <outdir>     e.g. rawlog.py /dev/sdb1 /tmp/recovered
"""
import os
import subprocess
import sys

MAGIC = b"BENCHLOG1\n"
SLOT = 64 * 1024 * 1024


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    dev, out = sys.argv[1], sys.argv[2]
    size = int(subprocess.check_output(["blockdev", "--getsize64", dev]))
    slot = min(SLOT, size)
    with open(dev, "rb") as fh:
        fh.seek(size - slot)
        data = fh.read(slot)
    i = data.find(MAGIC)
    if i < 0:
        print("no BENCHLOG1 magic in the last %d MiB of %s" % (slot // 1048576, dev))
        return 1
    j = data.find(b"\n", i + len(MAGIC))
    try:
        ln = int(data[i + len(MAGIC):j])
    except ValueError:
        print("malformed BENCHLOG1 header")
        return 1
    blob = data[j + 1:j + 1 + ln]
    if len(blob) != ln:
        print("truncated log blob (%d of %d bytes)" % (len(blob), ln))
    tgz = "/tmp/benchlog-recovered.tgz"
    with open(tgz, "wb") as fh:
        fh.write(blob)
    os.makedirs(out, exist_ok=True)
    subprocess.check_call(["tar", "xzf", tgz, "-C", out])
    print("recovered %d bytes -> %s (tarball kept at %s)" % (len(blob), out, tgz))
    return 0


if __name__ == "__main__":
    sys.exit(main())
