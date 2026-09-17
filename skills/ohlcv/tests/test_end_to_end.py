#!/usr/bin/env python3
"""The whole path, offline: collect() to a rendered string.

    python3 tests/test_end_to_end.py

Every other suite stops short of this. The shell tests exit before the
collector runs, the collector tests never call collect(), and the render tests
hand-build their evidence — so nothing proved the four scripts connect.

S3 and DuckDB are stubbed at four points: the duckdb import, connect(),
resolve_paths() and read_columns(). Patching resolve_paths alone is not enough
— collect() opens a connection before it, and INSTALL httpfs reaches the
network.
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

import bars as bar_math  # noqa: E402
import collect_ohlcv as collector  # noqa: E402
import render_ohlcv  # noqa: E402

UTC = dt.timezone.utc
HOUR = 3_600_000
NOW = dt.datetime(2026, 9, 17, tzinfo=UTC)
DAY15 = dt.datetime(2026, 9, 15, tzinfo=UTC)
DAY16 = dt.datetime(2026, 9, 16, tzinfo=UTC)

_passed = 0
_failed = 0


def check_that(name: str, condition: bool, detail: str = "") -> None:
    global _passed, _failed
    if condition:
        _passed += 1
    else:
        _failed += 1
        print(f"  ✗ {name} {detail}")


class FakeConnection:
    def close(self):
        return None


def ms(moment: dt.datetime) -> int:
    return int(moment.timestamp() * 1000)


def run_collect(columns, *, missing_days, files=("s3://x/one.parquet",),
                interval="1h", window="2d", component=None):
    """Drive the real collect() with the S3 boundary stubbed out."""
    original = (collector.connect, collector.resolve_paths,
                collector.read_columns)
    collector.connect = lambda: FakeConnection()
    collector.resolve_paths = lambda connection, patterns: (
        list(files), list(missing_days))
    collector.read_columns = lambda paths: columns
    try:
        evidence = collector.collect("BTC", "deribit", interval, window, NOW)
    finally:
        (collector.connect, collector.resolve_paths,
         collector.read_columns) = original
    if component:
        return evidence, render_ohlcv.spec_json(evidence, component)
    return evidence, render_ohlcv.render(evidence)


def canned():
    """Two 'objects' worth of rows, deliberately awkward.

    Out of order across objects, mixed timestamp units, one unreported size,
    one trade exactly on the window's end, and a quiet 12:00 on day 16.
    """
    day16 = ms(DAY16)
    return (
        [
            # "Object A" — afternoon, arriving first.
            day16 + 14 * HOUR, day16 + 14 * HOUR + 60_000,
            # "Object B" — morning, arriving second. Open must come from here.
            (day16 + 10 * HOUR) // 1000,          # seconds
            (day16 + 10 * HOUR + 60_000) * 1_000_000,  # nanoseconds
            # Exactly at the window end: excluded from the bars, but it is
            # still data the source reached.
            ms(NOW),
        ],
        [100.0, 105.0, 90.0, 95.0, 999.0],
        [1.5, 2.5, 3.0, None, 7.0],
    )


# ── The whole path ─────────────────────────────────────────────────────────

def test_renders_from_canned_partitions():
    evidence, out = run_collect(canned(), missing_days=["20260915"])
    check_that("collect reports ok", evidence["status"] == "ok",
               str(evidence.get("status")))
    check_that("render produced a table", "Open" in out and "Close" in out, out)
    check_that("header names the venue", "deribit" in out.splitlines()[0])


def test_open_comes_from_the_earliest_trade_not_the_first_object():
    """The reader gives no ordering across objects; this is where it shows."""
    evidence, out = run_collect(canned(), missing_days=["20260915"])
    ten = [b for b in evidence["bars"]
           if b["time"] == ms(DAY16) + 10 * HOUR]
    check_that("the 10:00 bar exists", len(ten) == 1, str(evidence["bars"]))
    if ten:
        check_that("open is the earlier of the two morning trades",
                   ten[0]["open"] == 90.0, f"got {ten[0]['open']}")
        check_that("close is the later one", ten[0]["close"] == 95.0,
                   f"got {ten[0]['close']}")


def test_mixed_timestamp_units_land_in_the_same_window():
    evidence, _ = run_collect(canned(), missing_days=["20260915"])
    times = sorted(b["time"] for b in evidence["bars"])
    check_that("seconds and nanoseconds both resolved to day 16",
               all(ms(DAY16) <= t < ms(NOW) for t in times), str(times))


def test_trade_on_the_window_end_is_excluded_but_still_seen():
    evidence, _ = run_collect(canned(), missing_days=["20260915"])
    check_that("no bar at the exclusive end",
               all(b["time"] < ms(NOW) for b in evidence["bars"]))
    check_that("it still set the watermark",
               evidence["watermark_ms"] == ms(NOW),
               str(evidence.get("watermark_ms")))


def test_missing_day_and_quiet_hour_are_told_apart():
    _, out = run_collect(canned(), missing_days=["20260915"])
    absent = [l for l in out.splitlines() if "absent" in l]
    quiet = [l for l in out.splitlines() if "no trades" in l]
    check_that("the unread day is reported absent", absent, out)
    check_that("the quiet hours are reported separately", quiet, out)
    # Parse the column rather than matching " 0.0": volume precision adapts to
    # the smallest traded size, so a zero bar can print as 0.00 or 0.000 and
    # a literal-string check would pass vacuously.
    volumes = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 6 and ":" in parts[0] and "⚠" not in line:
            try:
                volumes.append(float(parts[-1]))
            except ValueError:
                pass
    check_that("every rendered bar has real volume",
               volumes and all(v > 0 for v in volumes), str(volumes))


def test_unreported_size_reaches_the_rendered_output():
    _, out = run_collect(canned(), missing_days=["20260915"])
    check_that("the sizeless trade is counted, not zeroed",
               "unreported size" in out, out)


def test_empty_read_renders_the_unavailable_wording():
    evidence, out = run_collect(([], [], []), missing_days=["20260915"],
                                files=())
    check_that("status is unavailable", evidence["status"] == "unavailable")
    check_that("says partitions absent, not no trades",
               "partitions absent" in out or "partition absent" in out, out)
    check_that("prints no table", "Open" not in out, out)


def test_readable_but_quiet_market():
    evidence, out = run_collect(([], [], []), missing_days=[])
    check_that("status is ok with no bars",
               evidence["status"] == "ok" and evidence["bars"] == [])
    check_that("says no trades", "no trades in the window" in out, out)


def test_component_id_switches_to_the_spec():
    _, payload = run_collect(canned(), missing_days=["20260915"],
                             component="paradex-ohlcv_chart")
    check_that("emits the catalog spec", '"layout": "stack"' in payload,
               payload[:120])
    check_that("uses the id it was given",
               '"paradex-ohlcv_chart"' in payload, payload[:200])
    check_that("no bookkeeping field leaks to the component",
               "volume_unknown" not in payload, payload[:400])


def test_main_drives_the_whole_cli():
    """argparse through to stdout — the flags run_ohlcv.sh actually passes."""
    import io
    import contextlib

    original = (collector.connect, collector.resolve_paths,
                collector.read_columns, sys.argv)
    collector.connect = lambda: FakeConnection()
    collector.resolve_paths = lambda connection, patterns: (
        ["s3://x/one.parquet"], ["20260915"])
    collector.read_columns = lambda paths: canned()

    def run_main(extra):
        sys.argv = ["collect_ohlcv.py", "--asset", "BTC", "--venue", "deribit",
                    "--interval", "1h", "--window", "2d",
                    "--now", "2026-09-17T00:00:00Z", *extra]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = collector.main()
        return code, out.getvalue(), err.getvalue()

    try:
        code, out, _ = run_main(["--render"])
        check_that("--render exits zero", code == 0, str(code))
        check_that("--render prints the table", "Close" in out, out[:200])

        code, out, _ = run_main([])
        check_that("no --render dumps evidence json", code == 0 and '"bars"' in out,
                   out[:200])

        code, out, _ = run_main(["--render", "--component", "paradex-ohlcv_chart"])
        check_that("--component prints the spec",
                   code == 0 and '"layout": "stack"' in out, out[:200])

        collector.read_columns = lambda paths: ([], [], [])
        code, out, err = run_main(["--render", "--component", "x"])
        check_that("no bars falls back to the table", "No bars" in out, out[:200])
        check_that("and says so on stderr rather than silently",
                   "no chart spec" in err, err[:200])
    finally:
        (collector.connect, collector.resolve_paths,
         collector.read_columns, sys.argv) = original


def test_main_reports_a_failure_as_a_line_not_a_traceback():
    import io
    import contextlib

    original = (collector.connect, sys.argv)

    def boom():
        raise RuntimeError("columns ['price'] absent from the partition schema")

    collector.connect = boom
    sys.argv = ["collect_ohlcv.py", "--asset", "BTC", "--venue", "deribit",
                "--interval", "1h", "--window", "24h", "--render"]
    try:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = collector.main()
        check_that("exits non-zero", code == 1, str(code))
        check_that("names the error on one line",
                   err.getvalue().startswith("ohlcv: RuntimeError:"),
                   err.getvalue()[:120])
        check_that("no traceback", "Traceback" not in err.getvalue())
    finally:
        collector.connect, sys.argv = original


def test_bars_never_contradict_themselves():
    evidence, _ = run_collect(canned(), missing_days=["20260915"])
    check_that("no structural findings", bar_math.check(evidence["bars"]) == [],
               str(bar_math.check(evidence["bars"])))


def test_the_shared_reader_resolves():
    """collect_ohlcv imports s3_async from data-discovery at read time.

    Shared readers live there beside execution_tape.py, and skills import from
    data-discovery rather than sideways from each other — a skill-to-skill
    import would be the only one in the repo.
    """
    check_that("data-discovery scripts directory exists",
               os.path.isdir(collector.SHARED_SCRIPTS),
               str(collector.SHARED_SCRIPTS))
    check_that("s3_async is there",
               os.path.isfile(os.path.join(collector.SHARED_SCRIPTS,
                                           "s3_async.py")),
               str(collector.SHARED_SCRIPTS))
    check_that("and is no longer in options-recap",
               not os.path.isfile(os.path.join(
                   os.path.dirname(os.path.dirname(collector.SHARED_SCRIPTS)),
                   "options-recap", "scripts", "s3_async.py")))


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
