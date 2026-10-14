#!/usr/bin/env python3
"""Run the whole bench-test test suite (stdlib only).

    python tests/run_all.py

Covers the control-plane API, the reporting client, preset parity across the
GUI/TUI/web, the dashboard phase-table logic, and the web UI's structural
accessibility invariants. Each module starts its own in-process benchd on an
ephemeral port, so nothing external is needed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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

MODULES = [test_control_plane, test_reporting, test_import, test_update,
           test_presets, test_dashboards, test_soak, test_version, test_bootstrap,
           test_packaging, test_sbom, test_a11y, test_lifecycle]


def main():
    fails = []
    for mod in MODULES:
        print("== %s ==" % mod.__name__)
        try:
            fails += mod.run()
        except Exception as e:
            import traceback
            traceback.print_exc()
            fails.append("%s: crashed (%s)" % (mod.__name__, e))
        print()
    if fails:
        print("RESULT: FAIL (%d)" % len(fails))
        for f in fails:
            print("  -", f)
        import harness
        harness.cleanup_tmpdirs()
        return 1
    print("RESULT: PASS")
    import harness
    harness.cleanup_tmpdirs()
    return 0


if __name__ == "__main__":
    sys.exit(main())
