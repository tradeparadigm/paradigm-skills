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
import re as _re
import subprocess
import sys
import time

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


def sources(now_s, *args):
    """Run with the print-sources hook and a pinned clock; return
    "ASSET WIN START_MS <agg days,> <surface-open hour>". Exercises the real
    partition resolution (window-start date math → the UTC day globs for the
    5-min aggregates and the hour holding the window-open surface) with no
    creds."""
    env = dict(os.environ, RECAP_PRINT_SOURCES="1", RECAP_NOW_S=str(now_s))
    r = subprocess.run(["bash", SCRIPT, *args], capture_output=True, text=True,
                       env=env, timeout=20)
    return r.stdout.strip()


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


# ── Partition resolution (RECAP_PRINT_SOURCES) ──────────────────────────────
# Both stores are read as globs built from window-start date math, so the bash
# must be UTC and zero-padded on GNU and BSD date alike:
#   • the 5-min market aggregates, one glob per UTC DAY the window touches;
#   • the vol surface, whose window-open snapshot is in the HOUR partition
#     holding window-start — a wrong path there silently degrades every Δ
#     column to n/a, which is exactly the bug that shipped when the cold store
#     was empty.
# One day glob, not one per hour: a day-level pattern always matches at least
# one object, while an hour-level one is empty for the first ~5 minutes of every
# hour and DuckDB errors on a glob that matches nothing.


def expect(asset, win, now_s, secs):
    start = now_s - secs
    days = sorted({time.strftime("%Y%m%d", time.gmtime(t)) for t in (start, now_s)})
    hour = time.strftime("%Y/%m/%d/%H", time.gmtime(start))
    return f"{asset} {win} {start * 1000} {','.join(days)} {hour}"


def test_sources_single_day_window():
    now = 1_784_536_200  # 2026-07-20 08:30:00 UTC
    out = sources(now, "btc", "30m")
    check("30m stays within one day glob", out == expect("BTC", "30m", now, 1800), out)
    check("30m surface-open hour is 08", out.endswith("2026/07/20/08"), out)
    out = sources(now, "btc", "8h")
    check("8h stays within one day glob", out == expect("BTC", "8h", now, 28800), out)
    check("8h surface-open hour zero-padded/UTC",
          out.endswith("20260720 2026/07/20/00"), out)
    check("90m resolves for ETH too",
          sources(now, "eth", "90m") == expect("ETH", "90m", now, 5400))


def test_sources_day_boundary():
    # Window-start crosses midnight UTC: the day list must carry BOTH days and
    # the surface-open hour must roll back into the previous one.
    now = 1_784_514_600  # 2026-07-20 02:30:00 UTC
    out = sources(now, "btc", "8h")
    check("8h across midnight globs both days",
          "20260719,20260720" in out, out)
    check("8h across midnight → open hour 18 on day 19",
          out.endswith("2026/07/19/18"), out)
    check("8h across midnight full line", out == expect("BTC", "8h", now, 28800), out)


def test_sources_24h_window_spans_two_days():
    # The cap makes 24h the widest read, so it is the worst case for the day
    # globs: exactly two, never more.
    now = 1_784_536_200  # 2026-07-20 08:30:00 UTC
    out = sources(now, "btc", "24h")
    check("24h globs exactly two days", out.split()[3] == "20260719,20260720", out)
    check("24h full line", out == expect("BTC", "24h", now, 86400), out)


# ── The generated DuckDB session (RECAP_PRINT_SQL) ──────────────────────────
# Asserting on the real emitted plan rather than on the script's source text:
# these are the properties that make reading partitioned stores safe, and every
# one of them is a silent failure if it regresses.

def plan_sql(*args, now_s=1_784_536_200):
    env = dict(os.environ, RECAP_PRINT_SQL="1", RECAP_NOW_S=str(now_s))
    r = subprocess.run(["bash", SCRIPT, *args], capture_output=True, text=True,
                       env=env, timeout=20)
    return r.stdout


def test_plan_reads_partitioned_stores_not_hot_rollups():
    sql = plan_sql("btc", "8h")
    check("no recap-aggregates rollup",
          "hot__recap_aggregates" not in sql, sql[:200])
    check("no _hot vol surface", "_hot.parquet" not in sql, sql[:200])
    check("reads the 5-min aggregate partitions",
          "market_aggregates_5m/market_aggregates_5m__" in sql)
    check("reads the normalized option summaries",
          "data_type=option_summary" in sql)
    check("reads the instrument specs", "meta/instruments/exchange=" in sql)
    # The Paradigm tape is the one rollup still read — its upstream lives in a
    # bucket this role cannot reach. Pin that to exactly one object so a future
    # rollup read cannot slip back in unnoticed.
    hot = [ln for ln in sql.splitlines() if "dt-exchange-venue-data/hot/" in ln]
    check("exactly one hot/ read remains", len(hot) == 1, hot)
    check("and it is the Paradigm block tape",
          hot and "hot__paradigm_trade_tape_30d.parquet" in hot[0], hot)
    # No credential material in the printed plan, ever.
    check("credentials elided from the plan",
          "s3_secret_access_key" not in sql and "s3_session_token" not in sql)


def test_plan_loads_each_glob_in_its_own_statement():
    # A glob matching zero objects is a DuckDB error, and the first minutes of a
    # UTC day/hour legitimately have none. One statement per glob confines that
    # to the missing slice; a single read over all globs would lose the run.
    sql = plan_sql("btc", "24h")           # widest window → two day globs
    agg = [ln for ln in sql.splitlines() if ln.startswith("INSERT INTO agg")]
    check("one aggregate INSERT per UTC day", len(agg) == 2, len(agg))
    check("each aggregate glob is day-scoped, not hour-scoped",
          all(_re.search(r"market_aggregates_5m__\d{8}\*\.parquet", ln) for ln in agg), agg[:1])
    # Schema drift is live upstream: guard BOTH a dropped and an added column.
    check("union_by_name across objects in a glob",
          all("union_by_name=true" in ln for ln in agg), agg[:1])
    check("zero-row template supplies dropped columns",
          all("UNION ALL BY NAME SELECT * FROM agg WHERE false" in ln for ln in agg), agg[:1])
    check("explicit projection discards added columns",
          all("SELECT row_type, exchange, asset," in ln for ln in agg), agg[:1])
    osum = [ln for ln in sql.splitlines() if ln.startswith("INSERT INTO osum")]
    check("one surface INSERT per hour", len(osum) >= 2, len(osum))
    inst = [ln for ln in sql.splitlines() if ln.startswith("INSERT INTO inst")]
    check("one spec INSERT per venue", len(inst) == 5, len(inst))
    check("spec globs name the venue in the key, not a wildcard directory",
          all("exchange=*" not in ln for ln in inst), inst[:1])


def test_plan_stages_once_then_copies_from_tables():
    # Every COPY re-reading the parquet was free against one rollup object and
    # is ~300 objects per statement against the partitions.
    sql = plan_sql("btc", "8h")
    copies = [ln for ln in sql.splitlines() if ln.startswith("COPY (")]
    # dvol_spot, volume, venue_blocks, surface_now, surface_open, blocks,
    # freshness_rec, freshness_vs — one CSV each, recap.py reads all eight.
    check("every CSV recap.py reads is written", len(copies) == 8, len(copies))
    for ln in copies:
        target = ln.rsplit("/", 1)[-1]
        if "blocks.csv" in target and "venue" not in target:
            continue                      # the Paradigm tape read, by design
        check(f"{target} copies from a staging table",
              "read_parquet" not in ln, ln[:140])


def test_plan_applies_contract_specs_with_the_right_failure_mode():
    sql = plan_sql("btc", "8h")
    vol = next(ln for ln in sql.splitlines() if "/volume.csv'" in ln)
    blk = next(ln for ln in sql.splitlines() if "/venue_blocks.csv'" in ln)
    # volume.csv: every field recap.py reads is safe at contract_size 1.0
    # (turnover/trade_count need no scaling; volume_sum is summed for Deribit
    # only), and 1.0 is the true spec for the venues whose metadata publishes
    # irregularly — so a missing spec must NOT drop the venue's activity.
    check("volume tolerates a missing spec", "LEFT JOIN spec" in vol, vol[:160])
    check("volume defaults contract_size to 1.0",
          "coalesce(s.contract_size, 1.0)" in vol, vol[:160])
    # venue_blocks.csv: the multiplier IS load-bearing (blocks are priced
    # volume_coin x spot and ranked against the Paradigm tape), so an unscaled
    # OKX block reads 100x its size. Drop the venue instead of assuming 1.0.
    check("venue blocks require a spec",
          "JOIN spec" in blk and "LEFT JOIN spec" not in blk, blk[:160])
    check("venue blocks scale by contract_size",
          "a.volume_sum * s.contract_size" in blk, blk[:160])


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
