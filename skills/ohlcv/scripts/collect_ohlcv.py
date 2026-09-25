#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.3", "boto3>=1.35", "obstore>=0.3", "pyarrow>=17"]
# ///
"""Read bounded exchange partitions and build one market's OHLCV bars.

Reads normalized `perp_trade` rows at the 5m level and buckets them up to the
requested interval. `level` is file granularity, not sampling — the catalog is
explicit that 1m, 5m and 1h files carry the same events — and rows exist only
at 1m and 5m, so 5m is the coarsest level a candle can be built from and the
listing cost is flat in the interval.

The arithmetic lives in bars.py; this module is the read and the bookkeeping
around it: which objects, which were missing, and how fresh the newest one is.
"""

import argparse
import datetime as dt
import json
import re
import sys
import tempfile
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
# The shared readers live in data-discovery, beside execution_tape.py, and are
# imported lazily where they are used so the offline tests never need boto3 or
# obstore. Same direction as collect_recap.py and direct_inputs.py: skills
# import from data-discovery, never sideways from each other.
SHARED_SCRIPTS = (
    Path(__file__).resolve().parents[2] / "data-discovery" / "scripts")

import bars as bar_math  # noqa: E402

BUCKET = "dt-exchange-venue-data"
S3_ENDPOINT = "s3.ap-northeast-1.amazonaws.com"
REGION = "ap-northeast-1"

# The only level with rows that a coarse interval can be built from.
LEVEL = "5m"
DATA_TYPE = "perp_trade"
COLUMNS = ("timestamp", "price", "amount")

# DuckDB does listing here and nothing else — the parquet is read by
# s3_async/pyarrow — so it needs concurrency for the LIST calls and almost no
# memory. Sizing it like a scan costs the container headroom it does not get
# back: the agent process, the model context and this subprocess share one
# limit, and going over kills openclaw rather than just failing the query.
MAX_READ_THREADS = 16
DUCKDB_MEMORY_MB = 256

DUCKDB_PREFIX = f"""
INSTALL httpfs; LOAD httpfs;
INSTALL aws;    LOAD aws;
CREATE OR REPLACE SECRET s3_irsa (
  TYPE S3,
  PROVIDER CREDENTIAL_CHAIN,
  REGION '{REGION}',
  ENDPOINT '{S3_ENDPOINT}'
);
"""

DAY_IN_KEY = re.compile(r"/year=(\d{4})/month=(\d{2})/day=(\d{2})/")


def container_memory_bytes() -> int | None:
    for path in ("/sys/fs/cgroup/memory.max",
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            value = Path(path).read_text().strip()
        except OSError:
            continue
        if value.isdigit() and int(value) < (1 << 50):
            return int(value)
    return None


def connect() -> duckdb.DuckDBPyConnection:
    """A connection sized for many small remote objects, not for local CPU."""
    connection = duckdb.connect()
    connection.execute(DUCKDB_PREFIX)
    connection.execute(" ".join([
        f"SET threads={MAX_READ_THREADS};",
        "SET httpfs_connection_caching=true;",
        f"SET temp_directory='{tempfile.gettempdir()}/duckdb_ohlcv';",
        f"SET memory_limit='{DUCKDB_MEMORY_MB}MB';",
    ]))
    return connection


# ── Paths ──────────────────────────────────────────────────────────────────

def day_patterns(venue: str, currency: str, start: dt.datetime,
                 end: dt.datetime) -> tuple[list[str], list[str]]:
    """One glob per UTC day, plus the days they are expected to cover.

    One listing per day returns exactly what one listing per hour does, at a
    twenty-fourth of the S3 calls — the recap skill measured that difference as
    most of a seven-minute run.
    """
    base = (f"{BUCKET}/normalized/exchange={venue}/data_type={DATA_TYPE}/"
            f"currency={currency}/level={LEVEL}")
    patterns: list[str] = []
    days: list[str] = []
    cursor = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while cursor < end:
        days.append(f"{cursor:%Y%m%d}")
        patterns.append(f"s3://{base}/year={cursor:%Y}/month={cursor:%m}/"
                        f"day={cursor:%d}/**/*__rows__*.parquet")
        cursor += dt.timedelta(days=1)
    return patterns, days


def resolve_paths(connection, patterns: list[str]) -> tuple[list[str], list[str]]:
    """Expand globs to real objects and name the days that matched nothing.

    read_parquet() fails the whole list when any one pattern matches no object,
    so the current day — which producers are still writing — would otherwise
    erase every other day in the window. glob() tolerates a miss, which turns
    an absent day into something reportable instead of a dead read.
    """
    found: list[str] = []
    missing: list[str] = []
    for pattern in patterns:
        # Inlined rather than bound: DuckDB does not reliably accept prepared
        # parameters in a table function's arguments, and collect_recap.py
        # inlines for the same reason. The pattern is built from a validated
        # venue and asset (see `collect`), never from free text.
        rows = connection.execute(
            f"SELECT file FROM glob('{pattern}')").fetchall()
        if rows:
            found.extend(row[0] for row in rows)
        else:
            match = DAY_IN_KEY.search(pattern)
            missing.append("".join(match.groups()) if match else pattern)
    return sorted(set(found)), missing


def absent_buckets(missing_days: list[str], interval_ms: int,
                   start_ms: int, end_ms: int) -> list[int]:
    """Every bucket inside a day that produced no objects.

    coverage() wants bucket starts, not globs: a missing day is not one gap but
    every interval bucket within it, and only the part of the day the window
    actually asked for.
    """
    buckets: list[int] = []
    for day in missing_days:
        if not re.fullmatch(r"\d{8}", day):
            continue
        midnight = int(dt.datetime.strptime(day, "%Y%m%d")
                       .replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
        bucket = bar_math.bucket_start(max(midnight, start_ms), interval_ms)
        day_end = min(midnight + 86_400_000, end_ms)
        while bucket < day_end:
            # Overlap, not containment. coverage() walks the grid from
            # bucket_start(start_ms), so the bucket CONTAINING start_ms is one
            # it visits; requiring `bucket >= start_ms` would skip exactly that
            # one on any window that does not begin on a boundary — which is
            # every wall-clock run — and the absent partition would come back
            # labelled a quiet market.
            if bucket + interval_ms > start_ms:
                buckets.append(bucket)
            bucket += interval_ms
    return buckets


# ── Read ───────────────────────────────────────────────────────────────────

def read_columns(files: list[str]) -> tuple[list, list, list]:
    """Fetch the window's objects and return the three columns bars.py wants.

    A column absent from an object is silently filled with nulls by the reader
    rather than raised, so a renamed field would arrive as an all-null column
    and render as an empty market. Check the names came back before trusting
    them.
    """
    sys.path.insert(0, str(SHARED_SCRIPTS))
    from s3_async import read_objects  # noqa: PLC0415

    table = read_objects(files, COLUMNS)
    missing = [name for name in COLUMNS if name not in table.column_names]
    if missing:
        raise RuntimeError(
            f"columns {missing} absent from the partition schema — "
            f"the read would have rendered an empty market")
    return (table.column("timestamp").to_pylist(),
            table.column("price").to_pylist(),
            table.column("amount").to_pylist())


def to_millis(values: list) -> list:
    """Timestamps arrive as datetimes, ints or strings depending on producer."""
    out = []
    for value in values:
        if value is None:
            out.append(None)
        elif isinstance(value, dt.datetime):
            stamp = (value.replace(tzinfo=dt.timezone.utc)
                     if value.tzinfo is None else value)
            out.append(int(stamp.timestamp() * 1000))
        elif isinstance(value, (int, float)):
            number = int(value)
            # Producers publish seconds, millis, micros and nanos; scale by
            # magnitude rather than trusting one unit across venues. Loop, so
            # nanoseconds land on milliseconds instead of stopping one
            # division short in the year 33658.
            while number > 1_000_000_000_000_000:
                number //= 1000
            while 0 < number < 100_000_000_000:
                number *= 1000
            out.append(number)
        else:
            try:
                out.append(int(dt.datetime.fromisoformat(
                    str(value).replace("Z", "+00:00")).timestamp() * 1000))
            except ValueError:
                out.append(None)
    return out


SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_-]+$")


def collect(asset: str, venue: str, interval: str, window: str,
            now: dt.datetime | None = None) -> dict:
    # run_ohlcv.sh validates these, but this module is documented as directly
    # invocable — and a venue of `*` would widen the listing across every
    # venue in the bucket, walking straight past the day bound the shell
    # script exists to enforce.
    for name, value in (("asset", asset), ("venue", venue)):
        if not SAFE_TOKEN.fullmatch(value):
            raise ValueError(f"{name} '{value}' is not a plain identifier")
    interval_ms = bar_math.parse_interval(interval)
    width = bar_math.parse_interval(window)
    end = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    end_ms = int(end.timestamp() * 1000)
    start_ms = end_ms - width
    start = dt.datetime.fromtimestamp(start_ms / 1000, dt.timezone.utc)

    evidence = {
        "asset": asset, "venue": venue, "interval": interval,
        "window": window, "start_ms": start_ms, "end_ms": end_ms,
        "level": LEVEL, "data_type": DATA_TYPE, "source": "normalized",
        # Recorded because exceeding it kills the agent process, not just this
        # query — a window that OOMs takes openclaw down with it.
        "container_memory_bytes": container_memory_bytes(),
    }

    patterns, _ = day_patterns(venue, asset.lower(), start, end)
    connection = connect()
    try:
        files, missing_days = resolve_paths(connection, patterns)
    finally:
        connection.close()

    evidence["path_plan"] = {
        "day_patterns": len(patterns), "resolved_objects": len(files),
        "missing_days": missing_days,
    }

    if not files:
        evidence.update(status="unavailable", bars=[], coverage=[{
            "kind": "gap", "from": start_ms, "to": end_ms,
            "reason": bar_math.ABSENT}], summary=bar_math.summarize([]))
        return evidence

    times, prices, sizes = read_columns(files)
    times = to_millis(times)
    inside = [i for i, ts in enumerate(times)
              if ts is not None and start_ms <= ts < end_ms]
    bars = bar_math.to_bars([times[i] for i in inside],
                            [prices[i] for i in inside],
                            [sizes[i] for i in inside], interval_ms)

    # Freshness is how far the SOURCE reaches, so it comes from every row read,
    # not from the window-filtered subset. Taking the filtered max would make
    # the newest in-window trade the watermark, which is below the last bar's
    # period end by construction — every run, historical ones included, would
    # claim its final period was incomplete.
    seen = [ts for ts in times if ts is not None]
    watermark = max(seen) if seen else None
    coverage = bar_math.coverage(
        bars, interval_ms, start_ms, end_ms,
        absent=absent_buckets(missing_days, interval_ms, start_ms, end_ms),
        watermark_ms=watermark)

    evidence.update(
        status="ok",
        bars=bars,
        coverage=coverage,
        findings=bar_math.check(bars),
        summary=bar_math.summarize(bars),
        watermark_ms=watermark,
        rows_read=len(times),
        rows_in_window=len(inside),
        volume_unknown=sum(bar["volume_unknown"] for bar in bars),
    )
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset", required=True)
    parser.add_argument("--venue", required=True)
    parser.add_argument("--interval", required=True)
    parser.add_argument("--window", required=True)
    parser.add_argument("--now", help="UTC ISO-8601 end time, for reproducible tests")
    parser.add_argument("--render", action="store_true")
    parser.add_argument(
        "--component",
        help="Catalog component id the client advertised. Given one, --render "
             "prints that component's spec instead of the table. The id comes "
             "from the caller; it is never assumed.")
    args = parser.parse_args()

    now = (dt.datetime.fromisoformat(args.now.replace("Z", "+00:00"))
           if args.now else None)
    try:
        evidence = collect(args.asset.upper(), args.venue, args.interval,
                           args.window, now)
    except Exception as exc:  # noqa: BLE001 — the CLI boundary
        # "Report the error and stop" means a line the relay can pass on, not
        # a traceback for the user to interpret.
        print(f"ohlcv: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.render:
        import render_ohlcv  # noqa: PLC0415
        # An empty series has no spec — the component's schema requires at
        # least one bar — so it falls back to the table, which can say why.
        if args.component and evidence.get("bars"):
            print(render_ohlcv.spec_json(evidence, args.component))
        else:
            if args.component:
                # A caller that asked for JSON and got a table should be told
                # why, not left to infer it from the shape of stdout.
                print("ohlcv: no bars, so no chart spec — rendering the table, "
                      "which can say why", file=sys.stderr)
            print(render_ohlcv.render(evidence))
    else:
        print(json.dumps(evidence, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
