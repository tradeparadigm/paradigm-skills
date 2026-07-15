#!/usr/bin/env python3
"""
Tests for run_recap.sh argument normalization — no creds, no network.

run_recap.sh resolves `<asset> <window>` from positional args and drops a stray
"options"/"option" keyword that some users include (`/recap btc options 8h`).
Left in, that token would land in the window slot and break the run
(hot__recap_options.parquet doesn't exist; parse_window_ms raises). We invoke the
REAL script with RECAP_PRINT_ARGS=1, which echoes the resolved "ASSET WIN" and
exits 0 before any STS/DuckDB work — so this exercises the actual parsing in CI
with no AWS creds and no S3.

Run: python3 tests/test_run_recap.py
"""

import os
import subprocess
import sys

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "run_recap.sh")

_passed = 0
_failed = 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  ✗ {name}  {detail}")


def resolve(*args):
    """Run run_recap.sh with the print-args hook; return (stdout.strip(), rc)."""
    env = dict(os.environ, RECAP_PRINT_ARGS="1")
    r = subprocess.run(["bash", SCRIPT, *args], capture_output=True, text=True,
                       env=env, timeout=20)
    return r.stdout.strip(), r.returncode


def plan(*args):
    """Run with the print-plan hook; return ("ASSET WIN SECS PRESET", rc). Also
    surfaces stderr on non-zero rc so bad-window guards are checkable."""
    env = dict(os.environ, RECAP_PRINT_PLAN="1")
    r = subprocess.run(["bash", SCRIPT, *args], capture_output=True, text=True,
                       env=env, timeout=20)
    return (r.stdout.strip() or r.stderr.strip()), r.returncode


def test_plain_args():
    out, rc = resolve("BTC", "8h")
    check("BTC 8h passes through", out == "BTC 8h", out)
    check("exit 0", rc == 0, rc)


def test_asset_uppercased():
    out, _ = resolve("btc", "4h")
    check("lowercase asset uppercased", out == "BTC 4h", out)


def test_strips_options_keyword():
    out, rc = resolve("btc", "options", "8h")
    check("'options' dropped → BTC 8h", out == "BTC 8h", out)
    check("exit 0 (not the break path)", rc == 0, rc)


def test_strips_options_case_insensitive():
    check("OPTIONS dropped", resolve("BTC", "OPTIONS", "8h")[0] == "BTC 8h")
    check("singular 'option' dropped", resolve("eth", "option", "4h")[0] == "ETH 4h")


def test_options_with_1d_window():
    # 1d→24h normalization still applies after the keyword is stripped.
    out, _ = resolve("eth", "options", "1d")
    check("eth options 1d → ETH 24h", out == "ETH 24h", out)


def test_defaults():
    check("no args → BTC 8h", resolve()[0] == "BTC 8h")
    check("asset only → BTC 8h", resolve("btc")[0] == "BTC 8h")


# ── Dynamic-window parsing + preset gating (RECAP_PRINT_PLAN) ────────────────
# The old preset `case` silently defaulted any non-preset window (e.g. 3h) to
# 8h, so surface deltas were computed against the wrong window-open and the
# hot__recap_<win>.parquet read missed. Windows are now parsed generically.

def test_preset_window_plan():
    # Preset: SECS from the window, PRESET=1 (label only — same rolling-file path).
    check("8h → 28800s, preset", plan("btc", "8h") == ("BTC 8h 28800 1", 0))
    check("1h → 3600s, preset", plan("btc", "1h") == ("BTC 1h 3600 1", 0))


def test_dynamic_window_resolves_correctly():
    # The bug: 3h must resolve to 10800s (not the old 8h/28800 default), PRESET=0.
    check("3h → 10800s, non-preset", plan("btc", "3h") == ("BTC 3h 10800 0", 0))
    check("90m → 5400s, non-preset", plan("btc", "90m") == ("BTC 90m 5400 0", 0))
    check("6h → 21600s, non-preset", plan("btc", "6h") == ("BTC 6h 21600 0", 0))


def test_1d_normalizes_to_preset():
    # 1d → 24h happens before parsing, so it stays on the fast preset path.
    check("1d → 24h, 86400s, preset", plan("btc", "1d") == ("BTC 24h 86400 1", 0))


def test_windows_beyond_24h_cap():
    # Every flow source retains only ~24h, so longer windows clamp to 24h (the
    # live path also prepends a disclosure banner). 24h itself is NOT capped.
    check("2d caps to 24h", plan("eth", "2d") == ("ETH 24h 86400 1", 0))
    check("48h caps to 24h", plan("btc", "48h") == ("BTC 24h 86400 1", 0))
    check("25h caps to 24h", plan("btc", "25h") == ("BTC 24h 86400 1", 0))
    check("24h itself not capped", plan("btc", "24h") == ("BTC 24h 86400 1", 0))
    check("1440m (=24h) not capped", plan("btc", "1440m") == ("BTC 1440m 86400 0", 0))
    # Regression: the old substring 1d→24h substitution turned 31d into "324h"
    # (13.5 days); exact-match normalization + the cap now yield a plain 24h.
    check("31d caps to 24h (not 324h)", plan("btc", "31d") == ("BTC 24h 86400 1", 0))


def test_bad_window_exits_2():
    for w in ("3x", "foo", "0h", "h", "-2h"):
        out, rc = plan("btc", w)
        check(f"bad window '{w}' exits 2", rc == 2, f"rc={rc}")
        check(f"bad window '{w}' names it", "bad window" in out, out)


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    print(f"Running {len(tests)} test functions...")
    for t in tests:
        t()
    print(f"\n{_passed} checks passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
