#!/usr/bin/env python3
"""Offline checks for the collector's path plan and bookkeeping.

    python3 tests/test_collect_ohlcv.py

Nothing here touches S3. duckdb is stubbed only when genuinely absent, so this
runs in the stdlib-only lane without shadowing the real module for anything
collected alongside it.
"""

import datetime as dt
import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

if importlib.util.find_spec("duckdb") is None:
    sys.modules["duckdb"] = types.SimpleNamespace(
        Error=Exception, connect=None, DuckDBPyConnection=object)

import collect_ohlcv as collector  # noqa: E402

UTC = dt.timezone.utc
HOUR_MS = 3_600_000

_passed = 0
_failed = 0


def check_that(name: str, condition: bool, detail: str = "") -> None:
    global _passed, _failed
    if condition:
        _passed += 1
    else:
        _failed += 1
        print(f"  ✗ {name} {detail}")


# ── Path plan ──────────────────────────────────────────────────────────────

def test_day_patterns_one_per_day():
    start = dt.datetime(2026, 9, 15, 10, tzinfo=UTC)
    end = dt.datetime(2026, 9, 17, 10, tzinfo=UTC)
    patterns, days = collector.day_patterns("deribit", "btc", start, end)
    check_that("one glob per calendar day touched", len(patterns) == 3,
               f"got {len(patterns)}")
    check_that("days line up with patterns", len(days) == len(patterns))
    check_that("days are the ones spanned",
               days == ["20260915", "20260916", "20260917"], f"got {days}")


def test_day_patterns_shape():
    start = dt.datetime(2026, 9, 15, 10, tzinfo=UTC)
    patterns, _ = collector.day_patterns(
        "deribit", "btc", start, start + dt.timedelta(hours=1))
    pattern = patterns[0]
    check_that("normalized source, not raw", "/normalized/" in pattern, pattern)
    check_that("never the hot prefix", "hot" not in pattern, pattern)
    check_that("reads the 5m rows level", "level=5m" in pattern, pattern)
    check_that("rows files, not aggregates", "__rows__" in pattern, pattern)
    check_that("scoped to the venue", "exchange=deribit" in pattern, pattern)
    check_that("scoped to the currency", "currency=btc" in pattern, pattern)
    check_that("scoped to the day",
               "year=2026/month=09/day=15" in pattern, pattern)


def test_day_patterns_single_day_window():
    start = dt.datetime(2026, 9, 15, 1, tzinfo=UTC)
    patterns, _ = collector.day_patterns(
        "deribit", "btc", start, start + dt.timedelta(hours=2))
    check_that("a two-hour window lists one day", len(patterns) == 1,
               f"got {len(patterns)}")


# ── Missing days become absent buckets ─────────────────────────────────────

def test_absent_buckets_expands_a_missing_day():
    start = dt.datetime(2026, 9, 15, tzinfo=UTC)
    start_ms = int(start.timestamp() * 1000)
    end_ms = start_ms + 86_400_000
    buckets = collector.absent_buckets(["20260915"], HOUR_MS, start_ms, end_ms)
    check_that("a missing day is 24 hourly buckets", len(buckets) == 24,
               f"got {len(buckets)}")
    check_that("first bucket is midnight", buckets[0] == start_ms)
    check_that("last bucket is 23:00",
               buckets[-1] == start_ms + 23 * HOUR_MS)


def test_absent_buckets_clipped_to_the_window():
    day = dt.datetime(2026, 9, 15, tzinfo=UTC)
    midnight = int(day.timestamp() * 1000)
    # Window covers only 10:00–14:00 of that day.
    start_ms = midnight + 10 * HOUR_MS
    end_ms = midnight + 14 * HOUR_MS
    buckets = collector.absent_buckets(["20260915"], HOUR_MS, start_ms, end_ms)
    check_that("only the requested part of the day", len(buckets) == 4,
               f"got {len(buckets)}")
    check_that("nothing before the window start", min(buckets) >= start_ms)
    check_that("nothing at or after the window end", max(buckets) < end_ms)


def test_absent_buckets_covers_the_bucket_holding_the_start():
    """The grid coverage() walks starts at bucket_start(start_ms).

    A wall-clock window essentially never begins on a boundary, so if that
    first bucket is not marked absent, a missing partition comes back labelled
    a quiet market — the one distinction this skill exists to keep.
    """
    import bars as bar_math

    day = dt.datetime(2026, 9, 15, tzinfo=UTC)
    midnight = int(day.timestamp() * 1000)
    start_ms = midnight + 10 * HOUR_MS + 1_800_000  # 10:30, deliberately off-grid
    end_ms = midnight + 13 * HOUR_MS
    absent = collector.absent_buckets(["20260915"], HOUR_MS, start_ms, end_ms)
    first_visited = bar_math.bucket_start(start_ms, HOUR_MS)
    check_that("the bucket containing the start is marked absent",
               first_visited in absent, f"{first_visited} not in {absent}")

    found = bar_math.coverage([], HOUR_MS, start_ms, end_ms, absent=absent)
    reasons = {f["reason"] for f in found if f["kind"] == "gap"}
    check_that("a missing day reads as absent, never as quiet",
               reasons == {bar_math.ABSENT}, f"got {reasons}")


def test_absent_buckets_ignores_non_day_tokens():
    check_that("a glob that could not be parsed is skipped",
               collector.absent_buckets(["s3://whatever/**"], HOUR_MS, 0, 1) == [])


# ── Timestamp normalisation ────────────────────────────────────────────────

def test_to_millis_handles_every_producer_shape():
    moment = dt.datetime(2026, 9, 15, 10, tzinfo=UTC)
    expected = int(moment.timestamp() * 1000)
    check_that("aware datetime", collector.to_millis([moment]) == [expected])
    naive = moment.replace(tzinfo=None)
    check_that("naive datetime read as UTC",
               collector.to_millis([naive]) == [expected])
    check_that("milliseconds pass through",
               collector.to_millis([expected]) == [expected])
    check_that("seconds scale up",
               collector.to_millis([expected // 1000]) == [expected])
    check_that("microseconds scale down",
               collector.to_millis([expected * 1000]) == [expected])
    check_that("iso string", collector.to_millis(["2026-09-15T10:00:00Z"])
               == [expected])
    check_that("null survives", collector.to_millis([None]) == [None])
    check_that("unparseable becomes null",
               collector.to_millis(["not a time"]) == [None])


# ── Contract wiring ────────────────────────────────────────────────────────

def test_to_millis_scales_nanoseconds_all_the_way():
    moment = dt.datetime(2026, 9, 15, 10, tzinfo=UTC)
    expected = int(moment.timestamp() * 1000)
    check_that("nanoseconds reach milliseconds",
               collector.to_millis([expected * 1_000_000]) == [expected],
               f"got {collector.to_millis([expected * 1_000_000])}")


def test_collect_refuses_a_wildcard_venue():
    """A glob in the venue would list every venue, past the day bound."""
    for asset, venue in (("BTC", "*"), ("BTC", "../other"), ("*", "deribit")):
        try:
            collector.collect(asset, venue, "1h", "24h")
            check_that(f"refuses venue={venue!r} asset={asset!r}", False,
                       "accepted")
        except ValueError:
            check_that(f"refuses venue={venue!r} asset={asset!r}", True)
        except Exception as exc:  # noqa: BLE001
            check_that(f"refuses venue={venue!r} asset={asset!r}", False,
                       f"raised {type(exc).__name__} instead of ValueError")


def test_glob_is_inlined_not_bound():
    """DuckDB does not reliably bind parameters in table-function arguments."""
    source = open(os.path.join(ROOT, "scripts", "collect_ohlcv.py"),
                  encoding="utf-8").read()
    check_that("glob pattern is inlined", "glob('{pattern}')" in source,
               "parameter binding would fail only at runtime")


def test_constants_match_the_committed_contract():
    check_that("reads normalized", collector.LEVEL == "5m")
    check_that("perp trade rows", collector.DATA_TYPE == "perp_trade")
    check_that("projects only what bars needs",
               collector.COLUMNS == ("timestamp", "price", "amount"),
               str(collector.COLUMNS))
    check_that("endpoint pinned to the region",
               collector.S3_ENDPOINT == "s3.ap-northeast-1.amazonaws.com")
    check_that("credential chain, no inline keys",
               "CREDENTIAL_CHAIN" in collector.DUCKDB_PREFIX
               and "s3_access_key_id" not in collector.DUCKDB_PREFIX)
    check_that("never reads the hot prefix",
               "hot" not in collector.DUCKDB_PREFIX)


def test_skill_md_and_script_agree_on_the_level():
    skill = open(os.path.join(ROOT, "SKILL.md"), encoding="utf-8").read()
    check_that("SKILL.md documents the level the collector reads",
               f"`{collector.LEVEL}` rows level" in skill
               or f"the `{collector.LEVEL}` rows" in skill, "level drift")


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
