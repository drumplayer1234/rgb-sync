"""Run every hardware-free test as a subprocess; exit non-zero on any failure.

Included: test_nuphy_backup.py, test_restore.py, test_nuphy_edge.py, test_live_frame.py,
test_live_nuphy.py, test_live_engine.py,
test_league_client.py, test_league_colors.py, test_league_effects.py, test_league_sim.py,
test_gd_listener.py, test_gd_effects.py, test_gd_sim.py.
Not included (need real hardware / vendor software): smoke_test.py and
nuphy_read_state.py.

Run: python tests/run_all.py
"""
import pathlib
import subprocess
import sys

TESTS_DIR = pathlib.Path(__file__).resolve().parent
TESTS = ("test_nuphy_backup.py", "test_restore.py", "test_nuphy_edge.py",
         "test_live_frame.py", "test_live_nuphy.py", "test_live_engine.py",
         "test_league_client.py", "test_league_colors.py", "test_league_effects.py",
         "test_league_sim.py", "test_gd_listener.py", "test_gd_effects.py",
         "test_gd_sim.py")


def main() -> int:
    failed = []
    for name in TESTS:
        print(f"=== {name}", flush=True)
        result = subprocess.run([sys.executable, str(TESTS_DIR / name)])
        if result.returncode != 0:
            failed.append(name)
    print()
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 1
    print(f"all {len(TESTS)} test files passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
