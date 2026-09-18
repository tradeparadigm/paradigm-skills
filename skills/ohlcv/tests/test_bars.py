#!/usr/bin/env python3
"""Self-running checks for bars.py — stdlib only, no network, no deps.

    python3 tests/test_bars.py

Exits non-zero on the first failing assertion set and prints what passed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from bars import (  # noqa: E402
    ABSENT, NO_TRADES, bucket_start, check, coverage, parse_interval,
    summarize, to_bars,
)

HOUR = 3_600_000
T0 = 1_757_930_400_000  # 2026-09-15T10:00:00Z, a clean hour boundary

_passed = 0
_failed = 0


def check_that(name: str, condition: bool, detail: str = "") -> None:
    global _passed, _failed
    if condition:
        _passed += 1
    else:
        _failed += 1
        print(f"  ✗ {name} {detail}")


# ── Intervals ──────────────────────────────────────────────────────────────

def test_parse_interval():
    check_that("1m", parse_interval("1m") == 60_000)
    check_that("15m", parse_interval("15m") == 900_000)
    check_that("1h", parse_interval("1h") == HOUR)
    check_that("1d", parse_interval("1d") == 86_400_000)
    check_that("case insensitive", parse_interval("4H") == 4 * HOUR)
    for bad in ("", "0m", "h", "1w", "-1h", "1.5h", "abc", "60"):
        try:
            parse_interval(bad)
            check_that(f"rejects {bad!r}", False, "accepted")
        except ValueError:
            check_that(f"rejects {bad!r}", True)


def test_bucket_start():
    check_that("floors to the hour",
               bucket_start(T0 + 1_800_000, HOUR) == T0)
    check_that("boundary is its own bucket", bucket_start(T0, HOUR) == T0)
    day = 86_400_000
    # 1d buckets must land on UTC midnight, not on the window's start.
    midnight = bucket_start(T0, day)
    check_that("1d lands on UTC midnight", midnight % day == 0)
    check_that("1d contains the sample", midnight <= T0 < midnight + day)
    try:
        bucket_start(T0, 0)
        check_that("rejects zero interval", False, "accepted")
    except ValueError:
        check_that("rejects zero interval", True)


# ── Bars ───────────────────────────────────────────────────────────────────

def test_to_bars_basic():
    times = [T0, T0 + 60_000, T0 + 120_000, T0 + HOUR]
    prices = [100.0, 110.0, 90.0, 95.0]
    sizes = [1.0, 2.0, 3.0, 4.0]
    bars = to_bars(times, prices, sizes, HOUR)
    check_that("two buckets", len(bars) == 2, f"got {len(bars)}")
    first = bars[0]
    check_that("open is first trade", first["open"] == 100.0)
    check_that("close is last trade", first["close"] == 90.0)
    check_that("high", first["high"] == 110.0)
    check_that("low", first["low"] == 90.0)
    check_that("volume summed", first["volume"] == 6.0)
    check_that("bucket time is floored", first["time"] == T0)


def test_to_bars_ignores_scan_order():
    """The async reader gives no cross-object ordering guarantee."""
    times = [T0 + 120_000, T0, T0 + 60_000]
    prices = [90.0, 100.0, 110.0]
    sizes = [3.0, 1.0, 2.0]
    bars = to_bars(times, prices, sizes, HOUR)
    check_that("open from earliest timestamp", bars[0]["open"] == 100.0,
               f"got {bars[0]['open']}")
    check_that("close from latest timestamp", bars[0]["close"] == 90.0,
               f"got {bars[0]['close']}")


def test_to_bars_edges():
    check_that("empty input", to_bars([], [], [], HOUR) == [])
    single = to_bars([T0], [100.0], [5.0], HOUR)
    check_that("single trade bucket", len(single) == 1)
    check_that("single trade is all four prices",
               single[0]["open"] == single[0]["high"] == single[0]["low"]
               == single[0]["close"] == 100.0)
    holed = to_bars([T0, None, T0 + 60_000], [100.0, 1.0, 105.0],
                    [1.0, 1.0, 1.0], HOUR)
    check_that("null timestamp dropped", holed[0]["volume"] == 2.0)
    check_that("bars ascend by bucket",
               [b["time"] for b in to_bars([T0 + HOUR, T0], [1.0, 2.0],
                                           [1.0, 1.0], HOUR)] == [T0, T0 + HOUR])
    try:
        to_bars([T0], [1.0, 2.0], [1.0], HOUR)
        check_that("rejects ragged input", False, "accepted")
    except ValueError:
        check_that("rejects ragged input", True)


# ── Coverage ───────────────────────────────────────────────────────────────

def test_coverage_gap_runs():
    bars = to_bars([T0, T0 + 4 * HOUR], [100.0, 101.0], [1.0, 1.0], HOUR)
    found = coverage(bars, HOUR, T0, T0 + 5 * HOUR)
    gaps = [f for f in found if f["kind"] == "gap"]
    check_that("one merged run", len(gaps) == 1, f"got {len(gaps)}")
    check_that("run starts after the first bar", gaps[0]["from"] == T0 + HOUR)
    check_that("run ends at the next bar", gaps[0]["to"] == T0 + 4 * HOUR)
    check_that("quiet market reason", gaps[0]["reason"] == NO_TRADES)


def test_coverage_never_merges_reasons():
    """An unreadable partition and a quiet market are separate facts."""
    bars = to_bars([T0, T0 + 3 * HOUR], [100.0, 101.0], [1.0, 1.0], HOUR)
    found = coverage(bars, HOUR, T0, T0 + 4 * HOUR,
                     absent=[T0 + 2 * HOUR])
    gaps = [f for f in found if f["kind"] == "gap"]
    check_that("split into two runs", len(gaps) == 2, f"got {len(gaps)}")
    reasons = [g["reason"] for g in gaps]
    check_that("one of each reason",
               NO_TRADES in reasons and ABSENT in reasons, f"got {reasons}")
    absent_run = [g for g in gaps if g["reason"] == ABSENT][0]
    check_that("absent run is the right bucket",
               absent_run["from"] == T0 + 2 * HOUR)


def test_coverage_absent_outranks_quiet():
    found = coverage([], HOUR, T0, T0 + HOUR, absent=[T0])
    check_that("absent wins", found[0]["reason"] == ABSENT)


def test_coverage_partial_bars():
    bars = to_bars([T0 + 1_800_000], [100.0], [1.0], HOUR)
    opened_late = coverage(bars, HOUR, T0 + 1_800_000, T0 + HOUR)
    check_that("mid-period open flagged",
               any(f["kind"] == "partial" for f in opened_late))
    truncated = coverage(bars, HOUR, T0, T0 + HOUR,
                         watermark_ms=T0 + 2_820_000)
    check_that("incomplete last period flagged",
               any(f["kind"] == "partial" and "incomplete" in f["reason"]
                   for f in truncated))
    complete = coverage(bars, HOUR, T0, T0 + HOUR, watermark_ms=T0 + HOUR)
    check_that("complete period not flagged",
               not any(f["kind"] == "partial" and "incomplete" in f["reason"]
                       for f in complete))


def test_coverage_all_missing():
    found = coverage([], HOUR, T0, T0 + 3 * HOUR)
    gaps = [f for f in found if f["kind"] == "gap"]
    check_that("one run covering everything", len(gaps) == 1)
    check_that("spans the window",
               gaps[0]["from"] == T0 and gaps[0]["to"] == T0 + 3 * HOUR)


# ── Validation and summary ─────────────────────────────────────────────────

def test_coverage_unaligned_window_end():
    """A window ending mid-bucket must not swallow the run that touches it."""
    found = coverage([], HOUR, T0, T0 + 2 * HOUR + 1_800_000)
    gaps = [f for f in found if f["kind"] == "gap"]
    check_that("unaligned end still reports the gap", len(gaps) == 1,
               f"got {gaps}")
    check_that("gap covers the whole requested window",
               gaps[0]["from"] == T0
               and gaps[0]["to"] == T0 + 2 * HOUR + 1_800_000,
               f"got {gaps[0] if gaps else None}")


def test_coverage_unaligned_window_start():
    """Never report time before the window the caller asked about."""
    start = T0 + 1_800_000
    found = coverage([], HOUR, start, start + 2 * HOUR)
    gaps = [f for f in found if f["kind"] == "gap"]
    check_that("clamped to the requested start", gaps[0]["from"] == start,
               f"got {gaps[0]['from']}")
    check_that("clamped to the requested end",
               gaps[0]["to"] == start + 2 * HOUR, f"got {gaps[0]['to']}")


def test_coverage_ignores_bars_outside_window():
    outside = to_bars([T0 - 5 * HOUR], [100.0], [1.0], HOUR)
    found = coverage(outside, HOUR, T0, T0 + HOUR)
    check_that("outside bar does not count as present",
               any(f["kind"] == "gap" for f in found))
    check_that("outside bar does not trigger a partial",
               not any(f["kind"] == "partial" for f in found))


def test_to_bars_same_timestamp_is_deterministic():
    """Ties must not resolve by arrival order — the reader has none."""
    forward = to_bars([T0, T0, T0], [100.0, 90.0, 110.0], [1.0, 1.0, 1.0], HOUR)
    reverse = to_bars([T0, T0, T0], [110.0, 90.0, 100.0], [1.0, 1.0, 1.0], HOUR)
    check_that("open stable across input order",
               forward[0]["open"] == reverse[0]["open"],
               f"{forward[0]['open']} vs {reverse[0]['open']}")
    check_that("close stable across input order",
               forward[0]["close"] == reverse[0]["close"],
               f"{forward[0]['close']} vs {reverse[0]['close']}")
    # Both ends take the same side, so a tie shows as a doji rather than an
    # up candle the tie-break invented.
    check_that("tie open is the lowest price", forward[0]["open"] == 90.0)
    check_that("tie close matches the open", forward[0]["close"] == 90.0,
               f"got {forward[0]['close']}")
    single = to_bars([T0, T0], [100.0, 100.0], [1.0, 1.0], HOUR)
    check_that("single-instant bucket is flat",
               single[0]["open"] == single[0]["close"])
    # Distinct timestamps must still decide on time alone.
    ordered = to_bars([T0, T0 + 1], [100.0, 90.0], [1.0, 1.0], HOUR)
    check_that("later timestamp wins the close regardless of price",
               ordered[0]["close"] == 90.0)
    check_that("earlier timestamp wins the open regardless of price",
               ordered[0]["open"] == 100.0)


def test_coverage_empty_window():
    check_that("aligned zero-width window", coverage([], HOUR, 0, 0) == [])
    check_that("unaligned zero-width window", coverage([], HOUR, 5, 5) == [],
               f"got {coverage([], HOUR, 5, 5)}")
    check_that("inverted window", coverage([], HOUR, T0 + HOUR, T0) == [])


def test_to_bars_unknown_size_is_not_zero():
    bars = to_bars([T0, T0 + 60_000], [100.0, 101.0], [None, 2.0], HOUR)
    check_that("known size still summed", bars[0]["volume"] == 2.0)
    check_that("unknown size counted, not zeroed",
               bars[0]["volume_unknown"] == 1)
    check_that("prices still set by the sizeless trade",
               bars[0]["open"] == 100.0)
    clean = to_bars([T0], [100.0], [5.0], HOUR)
    check_that("no unknowns on clean input", clean[0]["volume_unknown"] == 0)
    # A reported size of zero is a real observation, not a missing one.
    zero = to_bars([T0], [100.0], [0.0], HOUR)
    check_that("explicit zero size is known", zero[0]["volume_unknown"] == 0)
    check_that("explicit zero size sums to zero", zero[0]["volume"] == 0.0)
    infinite = to_bars([T0], [100.0], [float("inf")], HOUR)
    check_that("non-finite size counted unknown",
               infinite[0]["volume_unknown"] == 1)
    check_that("non-finite size not summed", infinite[0]["volume"] == 0.0)


def test_to_bars_rejects_nan_prices():
    bars = to_bars([T0, T0 + 60_000], [float("nan"), 101.0], [1.0, 1.0], HOUR)
    check_that("NaN price dropped", bars[0]["open"] == 101.0,
               f"got {bars[0]['open']}")
    check_that("NaN does not poison high", bars[0]["high"] == 101.0)
    check_that("NaN does not poison low", bars[0]["low"] == 101.0)
    none_left = to_bars([T0], [float("nan")], [1.0], HOUR)
    check_that("all-NaN bucket produces no bar", none_left == [])


def test_check_finds_and_does_not_raise():
    good = to_bars([T0], [100.0], [1.0], HOUR)
    check_that("clean bars have no findings", check(good) == [])
    check_that("high below close",
               any("high below" in f for f in check(
                   [{"time": T0, "open": 1.0, "high": 1.0, "low": 0.0,
                     "close": 5.0, "volume": 1.0}])))
    check_that("low above open",
               any("low above" in f for f in check(
                   [{"time": T0, "open": 1.0, "high": 9.0, "low": 5.0,
                     "close": 8.0, "volume": 1.0}])))
    duplicate = [{"time": T0, "open": 1.0, "high": 1.0, "low": 1.0,
                  "close": 1.0, "volume": 1.0}] * 2
    check_that("duplicate bucket",
               any("duplicate" in f for f in check(duplicate)))
    unsorted = [{"time": T0 + HOUR, "open": 1.0, "high": 1.0, "low": 1.0,
                 "close": 1.0, "volume": 1.0},
                {"time": T0, "open": 1.0, "high": 1.0, "low": 1.0,
                 "close": 1.0, "volume": 1.0}]
    check_that("out of order", any("out of order" in f for f in check(unsorted)))


def test_summarize():
    bars = to_bars([T0, T0 + HOUR], [100.0, 110.0], [1.0, 2.0], HOUR)
    summary = summarize(bars)
    check_that("count", summary["count"] == 2)
    check_that("first open", summary["first_open"] == 100.0)
    check_that("last close", summary["last_close"] == 110.0)
    check_that("change pct", abs(summary["change_pct"] - 10.0) < 1e-9,
               f"got {summary['change_pct']}")
    check_that("volume", summary["volume"] == 3.0)

    down = to_bars([T0, T0 + HOUR], [100.0, 90.0], [1.0, 1.0], HOUR)
    check_that("negative change", summarize(down)["change_pct"] < 0)

    single = summarize(to_bars([T0], [100.0], [1.0], HOUR))
    check_that("single bar has no change", single["change_pct"] is None)
    check_that("single bar counts one", single["count"] == 1)

    zero = summarize([{"time": T0, "open": 0.0, "high": 1.0, "low": 0.0,
                       "close": 1.0, "volume": 1.0},
                      {"time": T0 + HOUR, "open": 1.0, "high": 1.0,
                       "low": 1.0, "close": 1.0, "volume": 1.0}])
    check_that("zero first open yields no pct", zero["change_pct"] is None)

    empty = summarize([])
    check_that("empty summary", empty["count"] == 0
               and empty["change_pct"] is None and empty["volume"] == 0.0)


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
