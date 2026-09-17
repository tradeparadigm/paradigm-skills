#!/usr/bin/env python3
"""Offline checks for the /ohlcv command parser and its refusals.

    python3 tests/test_run_ohlcv.py

Drives the shell script with OHLCV_PRINT_ARGS/OHLCV_PRINT_PLAN, so nothing
here touches S3 or needs credentials.
"""

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "run_ohlcv.sh")
CATALOG = os.path.join(
    os.path.dirname(ROOT), "data-discovery", "references", "exchange-raw.md")

_passed = 0
_failed = 0


def check_that(name: str, condition: bool, detail: str = "") -> None:
    global _passed, _failed
    if condition:
        _passed += 1
    else:
        _failed += 1
        print(f"  ✗ {name} {detail}")


def run(*args, env=None):
    """Return (output, returncode). Stdout when it succeeded, stderr when not."""
    environment = dict(os.environ, OHLCV_PRINT_ARGS="1")
    environment.update(env or {})
    result = subprocess.run(["bash", SCRIPT, *args], capture_output=True,
                            text=True, env=environment)
    return ((result.stdout or result.stderr).strip(), result.returncode)


def plan(*args):
    environment = dict(os.environ, OHLCV_PRINT_PLAN="1")
    environment.pop("OHLCV_PRINT_ARGS", None)
    result = subprocess.run(["bash", SCRIPT, *args], capture_output=True,
                            text=True, env=environment)
    return ((result.stdout or result.stderr).strip(), result.returncode)


# ── Parsing ────────────────────────────────────────────────────────────────

def test_defaults():
    output, code = run()
    check_that("bare invocation succeeds", code == 0, output)
    check_that("defaults are BTC deribit 1h 24h", output == "BTC deribit 1h 24h",
               f"got {output!r}")


def test_interval_then_window_order():
    output, _ = run("btc", "1m", "2h")
    check_that("first period is the interval, second the window",
               output == "BTC deribit 1m 2h", f"got {output!r}")


def test_single_period_is_the_window():
    output, _ = run("4h")
    check_that("one period is the window", output.endswith("4h"), output)
    check_that("interval derived for 4h is 1m", " 1m " in f" {output} ", output)


def test_interval_derivation_matches_documented_table():
    for window, expected in (("2h", "1m"), ("4h", "1m"), ("24h", "1h"),
                             ("3d", "1h"), ("7d", "1d")):
        output, code = run(window)
        check_that(f"{window} derives {expected}",
                   code == 0 and f" {expected} " in f" {output} ",
                   f"got {output!r}")


def test_order_independence():
    a, _ = run("btc", "deribit", "1h", "24h")
    b, _ = run("24h", "deribit", "btc", "1h")
    check_that("tokens are order-independent", a == b, f"{a!r} vs {b!r}")
    check_that("both parse correctly", a == "BTC deribit 1h 24h", a)


def test_venue_beats_asset_for_known_names():
    output, _ = run("bullish")
    check_that("a known venue is the venue, not the asset",
               output == "BTC bullish 1h 24h", f"got {output!r}")
    output, _ = run("eth", "bullish")
    check_that("asset and venue together", output == "ETH bullish 1h 24h",
               f"got {output!r}")


def test_case_insensitive():
    output, _ = run("ETH", "DERIBIT", "1H", "24H")
    check_that("upper-case input normalises",
               output == "ETH deribit 1h 24h", f"got {output!r}")


# ── Refusals ───────────────────────────────────────────────────────────────

def test_rejects_duplicate_slots():
    for args, what in ((("btc", "eth"), "two assets"),
                       (("deribit", "bullish"), "two venues"),
                       (("1m", "5m", "2h"), "three periods")):
        output, code = run(*args)
        check_that(f"refuses {what}", code == 2, f"code {code}: {output!r}")
        check_that(f"{what} refusal says why", output != "", "empty stderr")


def test_rejects_unknown_venue():
    # okex-options carries no trade rows, so it can never produce candles.
    output, code = run("okex-options", "1h", "24h")
    check_that("refuses a venue with no trade rows", code == 2,
               f"code {code}: {output!r}")
    output, code = run("btc", "okex-options", "1h", "24h")
    check_that("refuses it even alongside a real asset", code == 2,
               f"code {code}: {output!r}")


def test_rejects_unsupported_interval():
    output, code = run("3m", "24h")
    check_that("refuses an interval outside the set", code == 2, output)
    check_that("names the supported set", "1m" in output and "1d" in output,
               output)


def test_rejects_window_not_longer_than_interval():
    # Magnitude resolution means a longer-first pair is simply understood, so
    # equal periods are the only way left to ask for a one-bar window.
    for period in ("1h", "15m"):
        output, code = run(period, period)
        check_that(f"refuses {period} at {period}", code == 2,
                   f"code {code}: {output!r}")
        check_that("says window must exceed interval",
                   "longer than interval" in output, output)
    output, _ = run("4h", "1h")
    check_that("longer-first pair is understood, not refused",
               output == "BTC deribit 1h 4h", f"got {output!r}")


def test_rejects_too_many_bars():
    # 1m bars over 2d is 2,880 — over the 2,000-bar render bound.
    output, code = run("1m", "2d")
    check_that("refuses an unrenderable bar count", code == 2, output)
    check_that("names the bar bound", "2000" in output and "bar" in output,
               output)


def test_rejects_too_many_day_partitions():
    output, code = run("1d", "90d")
    check_that("refuses a long listing even at few bars", code == 2,
               f"code {code}: {output!r}")
    check_that("names the day bound", "90 day partitions" in output, output)
    check_that("says it is a listing-cost bound, not a data bound",
               "listing-cost" in output, output)
    check_that("tells the relay not to re-run at the bound",
               "do not re-run" in output.lower(), output)


def test_day_bound_is_overridable():
    output, code = run("1d", "30d", env={"OHLCV_MAX_DAYS": "40"})
    check_that("raising the bound admits a longer window", code == 0,
               f"code {code}: {output!r}")
    check_that("still parses correctly", output == "BTC deribit 1d 30d", output)


def test_rejects_garbage():
    for bad in ("1w", "--asset", "1.5h", "0h", "btc/usd"):
        _, code = run(bad)
        check_that(f"refuses {bad!r}", code == 2, f"code {code}")


# ── Plan ───────────────────────────────────────────────────────────────────

def test_plan_reports_the_derived_numbers():
    output, code = plan("btc", "1h", "24h")
    check_that("plan succeeds", code == 0, output)
    fields = output.split()
    check_that("plan has seven fields", len(fields) == 7, output)
    check_that("span is 24h in seconds", fields[4] == "86400", output)
    check_that("bar count", fields[5] == "24", output)
    check_that("day count", fields[6] == "1", output)


# ── The venue list against the catalog ─────────────────────────────────────

def test_venue_list_matches_the_catalog():
    """Fail when the catalog's trade-row matrix stops matching the script.

    The venue list is duplicated into the shell script by necessity; this is
    what stops it drifting silently from the source of truth.
    """
    if not os.path.exists(CATALOG):
        check_that("catalog is readable", False, CATALOG)
        return
    catalog = open(CATALOG, encoding="utf-8").read()
    # Only the "Available feeds" table describes venues; other tables in the
    # file map retired hot files and would otherwise be read as venue names.
    section = catalog.split("## Available feeds", 1)
    if len(section) < 2:
        check_that("catalog has an Available feeds table", False, CATALOG)
        return
    feeds = section[1].split("\n## ", 1)[0]
    trade_venues = set()
    for line in feeds.splitlines():
        if not line.startswith("| `"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        venue = cells[0].strip("`")
        if "perp_trade" in cells[1] or "spot_trade" in cells[1]:
            trade_venues.add(venue)
    declared = set(re.search(r'^VENUES="([^"]+)"', open(SCRIPT).read(),
                             re.M).group(1).split())
    check_that("script venues match catalog trade-row venues",
               declared == trade_venues,
               f"script {sorted(declared)} vs catalog {sorted(trade_venues)}")


def main() -> int:
    tests = [fn for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    print(f"Running {len(tests)} test functions...")
    for fn in tests:
        fn()
    print(f"\n{_passed} checks passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
