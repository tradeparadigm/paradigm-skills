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
