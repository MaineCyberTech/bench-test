#!/usr/bin/env python3
"""Run the whole bench-test test suite (stdlib only).

    python tests/run_all.py
    python tests/run_all.py --allow-skips     # tolerate platform/tooling gaps

Covers the control-plane API, the reporting client, preset parity across the
GUI/TUI/web, the dashboard phase-table logic, the web UI's structural
accessibility invariants, control-plane observability (structured logs,
request ids, /health vs /ready, /metrics) and data/schema integrity (schema
version, foreign keys, hashed runner keys). Each module starts its own
in-process benchd on an ephemeral port, so nothing external is needed.

Skips are load-bearing: a module that cannot run a whole group (no display, no
windows-curses, no powershell/bash, no torch) records it via harness.skip(), and
this runner prints a SKIP summary and fails unless --allow-skips (or
BENCH_TEST_ALLOW_SKIPS=1) is given. That keeps a provisioning gap from being
mistaken for a green suite.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import harness  # noqa: E402
import test_control_plane  # noqa: E402
import test_reporting  # noqa: E402
import test_import  # noqa: E402
import test_update  # noqa: E402
import test_presets  # noqa: E402
import test_dashboards  # noqa: E402
import test_soak  # noqa: E402
import test_version  # noqa: E402
import test_bootstrap  # noqa: E402
import test_packaging  # noqa: E402
import test_sbom  # noqa: E402
import test_a11y  # noqa: E402
import test_lifecycle  # noqa: E402
import test_hardening  # noqa: E402
import test_observability  # noqa: E402
import test_schema  # noqa: E402
import test_server_p3  # noqa: E402
import test_server_left  # noqa: E402
import test_alerts  # noqa: E402
import test_server_p2  # noqa: E402
import test_contract  # noqa: E402
import test_build_left  # noqa: E402
import test_robustness  # noqa: E402

MODULES = [test_control_plane, test_reporting, test_import, test_update,
           test_presets, test_dashboards, test_soak, test_version, test_bootstrap,
           test_packaging, test_sbom, test_a11y, test_lifecycle, test_hardening,
           test_observability, test_schema, test_server_p3, test_server_left,
           test_alerts, test_server_p2, test_contract, test_build_left,
           test_robustness]


def _allow_skips(argv):
    return "--allow-skips" in argv or os.environ.get("BENCH_TEST_ALLOW_SKIPS") == "1"


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    allow = _allow_skips(argv)
    fails = []
    skips = []
    for mod in MODULES:
        print("== %s ==" % mod.__name__)
        harness.take_skips()                     # clear before each module
        try:
            fails += mod.run()
        except Exception as e:
            import traceback
            traceback.print_exc()
            fails.append("%s: crashed (%s)" % (mod.__name__, e))
        for s in harness.take_skips():
            skips.append("%s: %s (%s)" % (mod.__name__, s["label"], s["reason"]))
        print()
    if skips:
        print("SKIP SUMMARY (%d)" % len(skips))
        for s in skips:
            print("  -", s)
        if allow:
            print("  (allowed by --allow-skips / BENCH_TEST_ALLOW_SKIPS)")
        print()
    bad = len(fails) + (0 if allow else len(skips))
    if bad:
        print("RESULT: FAIL (%d)" % bad)
        for f in fails:
            print("  -", f)
        for s in skips if not allow else []:
            print("  - skipped group:", s)
        harness.cleanup_tmpdirs()
        return 1
    print("RESULT: PASS")
    harness.cleanup_tmpdirs()
    return 0


if __name__ == "__main__":
    sys.exit(main())
