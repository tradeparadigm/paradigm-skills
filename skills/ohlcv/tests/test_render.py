#!/usr/bin/env python3
"""Self-running checks for the fixed /ohlcv rendering.

    python3 tests/test_render.py

The contract is references/output-format.md; eval case 3 in evals/evals.json
asserts against the same fixture, so the last test here renders that fixture
and checks the eval's claims hold. Without it the renderer and the eval drift
and only a grader notices.
"""

import datetime as dt
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import bars as bar_math  # noqa: E402
import render_ohlcv  # noqa: E402

UTC = dt.timezone.utc
HOUR = 3_600_000
T10 = int(dt.datetime(2026, 9, 16, 10, tzinfo=UTC).timestamp() * 1000)

_passed = 0
_failed = 0


def check_that(name: str, condition: bool, detail: str = "") -> None:
    global _passed, _failed
    if condition:
        _passed += 1
    else:
        _failed += 1
        print(f"  ✗ {name} {detail}")


def evidence(bars, **overrides):
    base = {
        "asset": "BTC", "venue": "deribit", "interval": "1h", "window": "6h",
        "start_ms": T10, "end_ms": T10 + 6 * HOUR, "status": "ok",
        "bars": bars, "coverage": [], "findings": [],
        "summary": bar_math.summarize(bars),
        "volume_unknown": sum(b.get("volume_unknown", 0) for b in bars),
        "path_plan": {"missing_days": []},
    }
    base.update(overrides)
    return base


def bar(offset_hours, o, h, l, c, v, unknown=0):
    return {"time": T10 + offset_hours * HOUR, "open": o, "high": h,
            "low": l, "close": c, "volume": v, "volume_unknown": unknown}


# ── Header ─────────────────────────────────────────────────────────────────

def test_header_shape():
    out = render_ohlcv.render(evidence([bar(0, 1.0, 2.0, 0.5, 1.5, 10.0)]))
    first = out.splitlines()[0]
    check_that("header is bold", first.startswith("**") and first.endswith("**"),
               first)
    check_that("names the symbol", "BTC-PERP" in first, first)
    check_that("names the interval", "1h" in first, first)
    check_that("names the venue", "deribit" in first, first)
    check_that("ends in UTC", first.rstrip("*").endswith("UTC"), first)


def test_header_dates_only_for_long_windows():
    short = render_ohlcv.render(
        evidence([bar(0, 1.0, 2.0, 0.5, 1.5, 10.0)])).splitlines()[0]
    check_that("a 6h window uses bare clock times",
               "Sep" not in short, short)
    long_window = render_ohlcv.render(evidence(
        [bar(0, 1.0, 2.0, 0.5, 1.5, 10.0)],
        window="24h", end_ms=T10 + 24 * HOUR)).splitlines()[0]
    check_that("a 24h window stamps dates", "Sep" in long_window, long_window)


# ── Table ──────────────────────────────────────────────────────────────────

def test_table_columns_and_separator():
    out = render_ohlcv.render(evidence([bar(0, 1.0, 2.0, 0.5, 1.5, 10.0)]))
    lines = out.splitlines()
    header = next(line for line in lines if line.startswith("Time"))
    order = [c for c in ("Time", "Open", "High", "Low", "Close", "Volume")
             if c in header]
    check_that("columns in contract order",
               order == ["Time", "Open", "High", "Low", "Close", "Volume"],
               header)
    check_that("volume header carries the unproven unit",
               "Volume (native)" in header, header)
    separator = lines[lines.index(header) + 1]
    check_that("a separator row follows the header",
               set(separator.replace(" ", "")) == {"-"}, separator)


def test_time_column_dates_follow_the_rendered_bars():
    same_day = render_ohlcv.render(evidence(
        [bar(0, 1.0, 2.0, 0.5, 1.5, 10.0), bar(1, 1.5, 2.0, 1.0, 1.8, 5.0)]))
    check_that("one date renders bare clock times",
               "Sep" not in same_day.split("Time")[1], same_day)
    across = render_ohlcv.render(evidence(
        [bar(0, 1.0, 2.0, 0.5, 1.5, 10.0), bar(20, 1.5, 2.0, 1.0, 1.8, 5.0)],
        window="24h", end_ms=T10 + 24 * HOUR))
    check_that("two dates stamp the Time column",
               "Sep" in across.split("Time")[1], across)


# ── Summary ────────────────────────────────────────────────────────────────

def test_summary_line():
    out = render_ohlcv.render(evidence(
        [bar(0, 100.0, 110.0, 90.0, 105.0, 10.0),
         bar(1, 105.0, 120.0, 95.0, 110.0, 5.0)]))
    line = next(l for l in out.splitlines() if " bars · " in l)
    check_that("bar count", line.startswith("2 bars"), line)
    check_that("signed percentage", "+10.00%" in line, line)
    check_that("labelled high", "high 120.0" in line, line)
    check_that("labelled low", "low 90.0" in line, line)
    check_that("labelled volume", "vol 15.0" in line, line)


def test_single_bar_is_singular_and_has_no_percentage():
    out = render_ohlcv.render(evidence([bar(0, 100.0, 110.0, 90.0, 105.0, 3.0)]))
    line = next(l for l in out.splitlines() if l.startswith("1 bar"))
    check_that("singular noun", line.startswith("1 bar ·") or line == "1 bar",
               line)
    check_that("no fabricated percentage", "%" not in line, line)


# ── Coverage ───────────────────────────────────────────────────────────────

def test_gap_run_expands_into_periods():
    """coverage() merges runs; the contract reports the periods in them."""
    gap = {"kind": "gap", "from": T10 + 3 * HOUR, "to": T10 + 5 * HOUR,
           "reason": bar_math.ABSENT}
    out = render_ohlcv.render(evidence(
        [bar(0, 1.0, 2.0, 0.5, 1.5, 10.0)], coverage=[gap]))
    line = next(l for l in out.splitlines() if "unavailable" in l)
    check_that("counts the periods", line.startswith("⚠ 2 periods"), line)
    check_that("names each period", "13:00, 14:00" in line, line)
    check_that("plural reason for a multi-period run",
               line.endswith("partitions absent"), line)
    check_that("no raw epochs leak", "1789" not in line, line)


def test_single_period_gap_is_singular():
    gap = {"kind": "gap", "from": T10 + 3 * HOUR, "to": T10 + 4 * HOUR,
           "reason": bar_math.ABSENT}
    out = render_ohlcv.render(evidence(
        [bar(0, 1.0, 2.0, 0.5, 1.5, 10.0)], coverage=[gap]))
    line = next(l for l in out.splitlines() if "unavailable" in l)
    check_that("singular period", "1 period unavailable" in line, line)
    check_that("singular reason", line.endswith("partition absent"), line)


def test_absent_and_quiet_are_never_merged():
    out = render_ohlcv.render(evidence([bar(0, 1.0, 2.0, 0.5, 1.5, 10.0)],
        coverage=[
            {"kind": "gap", "from": T10 + 2 * HOUR, "to": T10 + 3 * HOUR,
             "reason": bar_math.ABSENT},
            {"kind": "gap", "from": T10 + 3 * HOUR, "to": T10 + 4 * HOUR,
             "reason": bar_math.NO_TRADES}]))
    absent = [l for l in out.splitlines() if "absent" in l]
    quiet = [l for l in out.splitlines() if "no trades" in l]
    check_that("absent reported separately", len(absent) == 1, str(absent))
    check_that("quiet reported separately", len(quiet) == 1, str(quiet))


def test_unproven_things_are_always_stated():
    out = render_ohlcv.render(evidence([bar(0, 1.0, 2.0, 0.5, 1.5, 10.0)]))
    check_that("tick size unknown is stated", "tick size is unknown" in out, out)
    check_that("native volume units stated", "native units" in out, out)


def test_unreported_sizes_are_counted():
    out = render_ohlcv.render(evidence(
        [bar(0, 1.0, 2.0, 0.5, 1.5, 10.0, unknown=3)]))
    line = next(l for l in out.splitlines() if "unreported size" in l)
    check_that("counts the trades", "3 trades" in line, line)
    check_that("calls volume a lower bound", "proven subset" in line, line)


def test_structural_findings_surface():
    out = render_ohlcv.render(evidence(
        [bar(0, 1.0, 2.0, 0.5, 1.5, 10.0)],
        findings=["bar 123: high below open/close"]))
    check_that("a structural finding is reported, not dropped",
               "high below open/close" in out, out)


# ── Empty results ──────────────────────────────────────────────────────────

def test_unavailable_and_quiet_read_differently():
    unread = render_ohlcv.render(evidence(
        [], status="unavailable",
        path_plan={"missing_days": ["20260916", "20260917"]}))
    check_that("unreadable says partitions absent",
               "No bars — all 2 partitions absent" in unread, unread)
    quiet = render_ohlcv.render(evidence([]))
    check_that("readable but empty says no trades",
               "No bars — no trades in the window" in quiet, quiet)
    check_that("no empty table is printed", "Open" not in quiet, quiet)


# ── Chart spec ─────────────────────────────────────────────────────────────

def test_spec_props_match_the_component_schema():
    built = render_ohlcv.spec(evidence(
        [bar(0, 1.0, 2.0, 0.5, 1.5, 10.0, unknown=2)],
        coverage=[{"kind": "gap", "from": T10 + HOUR, "to": T10 + 2 * HOUR,
                   "reason": bar_math.ABSENT},
                  {"kind": "partial", "from": T10, "to": T10 + 60,
                   "reason": "window opens mid-period"}]),
        "paradex-ohlcv_chart")
    props = built["children"][0]["props"]
    check_that("layout is a stack", built["layout"] == "stack")
    check_that("component id is the one passed in",
               built["children"][0]["component"] == "paradex-ohlcv_chart")
    check_that("props are exactly the schema's fields",
               set(props) == {"bars", "symbol", "interval", "venue",
                              "priceDecimals", "gaps"}, str(sorted(props)))
    check_that("bars carry no bookkeeping field",
               set(props["bars"][0]) == {"time", "open", "high", "low",
                                         "close", "volume"},
               str(sorted(props["bars"][0])))
    check_that("time stays epoch ms", props["bars"][0]["time"] == T10)
    check_that("only real gaps become bands", len(props["gaps"]) == 1,
               str(props["gaps"]))
    check_that("gap entries drop the kind key",
               set(props["gaps"][0]) == {"from", "to", "reason"},
               str(sorted(props["gaps"][0])))
    check_that("spec is JSON-serialisable",
               json.loads(render_ohlcv.spec_json(
                   evidence([bar(0, 1.0, 2.0, 0.5, 1.5, 10.0)]),
                   "x")) is not None)


def test_spec_refuses_an_empty_series():
    try:
        render_ohlcv.spec(evidence([]), "paradex-ohlcv_chart")
        check_that("empty series has no spec", False, "accepted")
    except ValueError:
        check_that("empty series has no spec", True)


# ── The eval fixture ───────────────────────────────────────────────────────

def test_header_uses_the_contract_date_order():
    """The header writes `Sep 16 10:00`; only the Time column uses `16Sep`."""
    out = render_ohlcv.render(evidence(
        [bar(0, 1.0, 2.0, 0.5, 1.5, 10.0)],
        window="24h", end_ms=T10 + 24 * HOUR))
    header = out.splitlines()[0]
    check_that("month precedes the day in the header",
               "Sep 16 10:00" in header, header)
    check_that("not the Time column's order", "16Sep" not in header, header)


def test_partial_lines_print_times_not_epochs():
    finished = render_ohlcv.render(evidence(
        [bar(0, 1.0, 2.0, 0.5, 1.5, 10.0)],
        coverage=[{"kind": "partial", "reason": "period incomplete",
                   "from": T10, "to": T10 + HOUR,
                   "period_end": T10 + HOUR, "reaches": T10 + 2_820_000}]))
    line = next(l for l in finished.splitlines() if "partial" in l)
    check_that("names the period end as a clock time",
               "period ends 11:00" in line, line)
    check_that("names the watermark as a clock time",
               "data reaches 10:47" in line, line)
    check_that("no raw epoch in the output", "1789" not in line, line)

    opened = render_ohlcv.render(evidence(
        [bar(0, 1.0, 2.0, 0.5, 1.5, 10.0)],
        coverage=[{"kind": "partial", "reason": "opens mid-period",
                   "from": T10, "to": T10 + 1_800_000,
                   "period_start": T10, "window_edge": T10 + 1_800_000}]))
    line = next(l for l in opened.splitlines() if "partial" in l)
    check_that("first-bar partial names both instants",
               "period opens 10:00" in line and "window starts 10:30" in line,
               line)


def test_long_gap_runs_are_summarised():
    """A seven-day 1m run would otherwise be a 14,000-character line."""
    minute = 60_000
    gap = {"kind": "gap", "from": T10, "to": T10 + 500 * minute,
           "reason": bar_math.NO_TRADES}
    out = render_ohlcv.render(evidence(
        [bar(0, 1.0, 2.0, 0.5, 1.5, 10.0)], interval="1m", coverage=[gap]))
    line = next(l for l in out.splitlines() if "unavailable" in l)
    check_that("still counts every period", "500 periods" in line, line)
    check_that("names a span instead of listing them", "–" in line, line)
    check_that("stays one readable line", len(line) < 120, f"{len(line)} chars")


def test_small_volumes_never_render_as_zero():
    """A 0.04-size bar at one decimal place is an invented zero-volume bar."""
    out = render_ohlcv.render(evidence(
        [bar(0, 1.0, 2.0, 0.5, 1.5, 0.04), bar(1, 1.5, 2.0, 1.0, 1.8, 1.2)]))
    rows = [l for l in out.splitlines() if l.startswith(("10:00", "11:00"))]
    check_that("the small size keeps a nonzero figure",
               any("0.04" in row for row in rows), str(rows))
    check_that("precision is uniform across the column",
               all(len(row.rsplit(" ", 1)[-1].split(".")[-1]) == 2
                   for row in rows), str(rows))
    check_that("summary matches the table's precision",
               "vol 1.24" in out, out)
    plain = render_ohlcv.render(evidence([bar(0, 1.0, 2.0, 0.5, 1.5, 10.0)]))
    check_that("ordinary sizes stay at one decimal place",
               "vol 10.0" in plain, plain)


def test_volume_decimals_chooses_the_least_that_works():
    check_that("ordinary", render_ohlcv.volume_decimals([1.5, 20.0]) == 1)
    check_that("small", render_ohlcv.volume_decimals([0.04]) == 2)
    check_that("tiny", render_ohlcv.volume_decimals([0.00004]) == 5)
    check_that("all zero falls back", render_ohlcv.volume_decimals([0.0]) == 1)
    check_that("empty falls back", render_ohlcv.volume_decimals([]) == 1)


def test_matches_eval_case_three():
    """Render eval 3's fixture and check the assertions that case makes."""
    start = int(dt.datetime(2026, 9, 16, 10, tzinfo=UTC).timestamp() * 1000)
    fixture = [
        bar(0, 63120.5, 63400.0, 63050.0, 63380.5, 142.7),
        bar(1, 63380.5, 63512.0, 63201.4, 63244.9, 98.3),
        bar(2, 63244.9, 63300.1, 62980.0, 63010.2, 201.5),
        bar(5, 62990.0, 63180.4, 62905.7, 63150.8, 77.4),
    ]
    gap = {"kind": "gap", "from": start + 3 * HOUR, "to": start + 5 * HOUR,
           "reason": bar_math.ABSENT}
    out = render_ohlcv.render(evidence(fixture, coverage=[gap]))

    check_that("renders exactly four bars",
               sum(1 for l in out.splitlines() if ":00" in l and "⚠" not in l
                   and "**" not in l) == 4, out)
    for absent in ("13:00 ", "14:00 "):
        check_that(f"{absent.strip()} is not a table row",
                   not any(l.startswith(absent) for l in out.splitlines()), out)
    check_that("the absent periods are a coverage line",
               "⚠ 2 periods unavailable (13:00, 14:00) — partitions absent"
               in out, out)
    check_that("summary reports 4 bars", "4 bars" in out, out)
    # (63150.8 - 63120.5) / 63120.5 = 0.048003% -> +0.05%
    check_that("change is first open to last close", "+0.05%" in out, out)
    check_that("window high", "high 63512.0" in out, out)
    check_that("window low", "low 62905.7" in out, out)
    check_that("volume totals the rendered bars", "vol 519.9" in out, out)
    for value in ("63120.5", "63400.0", "63050.0", "63380.5", "142.7",
                  "63512.0", "62905.7", "63150.8", "77.4"):
        check_that(f"{value} survives unrounded", value in out, out)
    check_that("no zero-volume filler bar", " 0.0\n" not in out, out)

    evals = json.load(open(os.path.join(ROOT, "evals", "evals.json"),
                           encoding="utf-8"))
    case = next(c for c in evals["evals"] if c["id"] == 3)
    check_that("eval 3 still opts out of simulate mode",
               case.get("simulate") is False, str(case.get("simulate")))
    for value in ("63120.5", "63512.0", "62905.7", "63150.8"):
        check_that(f"eval prompt still carries {value}",
                   value in case["prompt"], "fixture drift")


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
