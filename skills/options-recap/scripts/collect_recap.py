#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.3", "polars>=1.0", "boto3>=1.35"]
# ///
"""Collect bounded direct exchange evidence without deciding the recap narrative."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import re
import sys
import tempfile
from dataclasses import dataclass
from typing import Any
from pathlib import Path

import duckdb

BUCKET = "s3://dt-exchange-venue-data"
VENUES = ("deribit", "deribit-usdc", "okex-options", "bybit-options", "bullish")


def parse_window(value: str) -> dt.timedelta:
    match = re.fullmatch(r"([1-9][0-9]*)([mhd])", value.lower())
    if not match:
        raise ValueError("window must be a positive Nm, Nh, or Nd value")
    amount, unit = int(match.group(1)), match.group(2)
    width = dt.timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[unit]: amount})
    # No 24h clamp — partitions serve any window — but the execution tape keeps
    # only 30 days, and an unbounded window globs every hour of it per venue.
    if width > dt.timedelta(days=30):
        raise ValueError("window must be 30d or less; the execution tape keeps 30 days")
    return width


def hour_patterns(source: str, venue: str, data_type: str, currency: str,
                  start: dt.datetime, end: dt.datetime) -> tuple[list[str], list[str]]:
    """Day-level globs, plus the hours they are expected to cover.

    One listing per day returns exactly the objects that one listing per hour
    does, and every returned key carries its own hour — so coverage is read off
    the result rather than probed for, at 1/24 of the S3 calls. A 30-day window
    was 10,090 listings, which was most of its seven-minute runtime.
    """
    base = (f"{BUCKET}/{source}/exchange={venue}/data_type={data_type}/"
            f"currency={currency}/level=5m")
    cursor = start.replace(minute=0, second=0, microsecond=0)
    hours: list[str] = []
    days: list[str] = []
    while cursor < end:
        hours.append(f"{cursor:%Y%m%dT%H}")
        day = f"{base}/year={cursor:%Y}/month={cursor:%m}/day={cursor:%d}/**/*__rows__*.parquet"
        if day not in days:
            days.append(day)
        cursor += dt.timedelta(hours=1)
    return days, hours


def snapshot_patterns(venue: str, currency: str, start: dt.datetime,
                      end: dt.datetime) -> list[str]:
    """Exact window-open and latest-complete five-minute snapshot buckets."""
    floor = lambda value: value.replace(minute=value.minute - value.minute % 5,
                                        second=0, microsecond=0)
    open_point = floor(start)
    points = sorted({open_point, max(open_point, floor(end) - dt.timedelta(minutes=10))})
    patterns = []
    for point in points:
        patterns.append(
            f"{BUCKET}/normalized/exchange={venue}/data_type=option_summary/currency={currency}/"
            f"level=5m/year={point:%Y}/month={point:%m}/day={point:%d}/hour={point:%H}/"
            f"start_minute={point:%M}/*__rows__*.parquet"
        )
    return patterns


def sql_list(values: list[str]) -> str:
    return "[" + ",".join("'" + value.replace("'", "''") + "'" for value in values) + "]"


DUCKDB_PREFIX = """
INSTALL httpfs; LOAD httpfs;
INSTALL aws; LOAD aws;
CREATE OR REPLACE SECRET dime_s3 (
  TYPE S3, PROVIDER CREDENTIAL_CHAIN, REGION 'ap-northeast-1',
  ENDPOINT 's3.ap-northeast-1.amazonaws.com'
);
"""

MAX_READ_THREADS = 64


def query_workers(width: dt.timedelta) -> int:
    """How many of these queries can hold their window in memory at once.

    A trade query materialises its whole window — a 30-day venue is ~1.3M rows —
    so wide windows have to give up concurrency to stay inside the container's
    memory limit. Exceeding it kills the agent runtime, not just the query.
    """
    days = width / dt.timedelta(days=1)
    return 3 if days <= 2 else 2 if days <= 10 else 1


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


def tuning_statements(workers: int = 3) -> str:
    """Size DuckDB for reading many small remote objects, not for local CPU work.

    DuckDB derives `threads` from the CPU quota — 2 in this container — and then
    uses it to cap in-flight HTTP requests too. These reads are latency-bound,
    thousands of objects per window, so the threads sit blocked on the network
    rather than competing for the quota and a far higher count is correct.
    Connection caching matters for the same reason: without it every object pays
    a fresh TLS handshake.
    """
    limit = container_memory_bytes()
    # DuckDB is not the only tenant: --render hands every trade row back as
    # Python dicts, which polars then copies. Half the container is its share.
    per_query = int(limit * 0.5 / workers) if limit else None
    statements = [
        f"SET threads={MAX_READ_THREADS};",
        "SET httpfs_connection_caching=true;",
        "SET preserve_insertion_order=false;",
        f"SET temp_directory='{tempfile.gettempdir()}/duckdb_recap';",
    ]
    if per_query:
        statements.append(f"SET memory_limit='{per_query // (1 << 20)}MB';")
    return " ".join(statements)


TUNING = tuning_statements()


def connect() -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect()
    connection.execute(DUCKDB_PREFIX)
    connection.execute(TUNING)
    return connection


def set_budget(workers: int) -> None:
    global TUNING
    TUNING = tuning_statements(workers)


@dataclass(frozen=True)
class Query:
    name: str
    paths: list[str]
    sql: str
    units: dict[str, str]
    required: bool = False
    # Hours the day-level globs should cover; empty means report per pattern.
    expected_hours: tuple[str, ...] = ()


HOUR_IN_KEY = re.compile(r"__rows__(\d{8}T\d{2})")


def resolve_paths(connection: duckdb.DuckDBPyConnection, patterns: list[str],
                  expected_hours: tuple[str, ...] = ()) -> tuple[list[str], list[str]]:
    """Resolve the globs to real objects, and name what the window is missing.

    read_parquet() fails the entire list when any single pattern matches no
    object, so the current hour — which producers only write ~10 minutes in —
    would otherwise erase every other hour in the window. glob() tolerates a
    miss, so the read is scoped to partitions that exist and the absent ones
    are reported rather than silently taking the whole source down with them.

    One glob() call walks its patterns in sequence, so a 30-day window spends
    half a minute listing before it reads anything. The patterns are independent
    LISTs, so issue them concurrently and merge.
    """
    def listing(pattern: str) -> list[str]:
        scoped = connection.cursor()
        try:
            return [row[0] for row in
                    scoped.execute(f"SELECT file FROM glob('{pattern}')").fetchall()]
        finally:
            scoped.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(patterns))) as pool:
        files = sorted({path for group in pool.map(listing, patterns) for path in group})
    if expected_hours:
        present = {match.group(1) for path in files
                   if (match := HOUR_IN_KEY.search(path))}
        return files, [hour for hour in expected_hours if hour not in present]
    missing = []
    for pattern in patterns:
        prefix = pattern.split("*", 1)[0]
        if not any(f.startswith(prefix) for f in files):
            missing.append(pattern)
    return files, missing


def run_query(query: Query) -> tuple[dict[str, Any], list[Any]]:
    source: dict[str, Any] = {
        "name": query.name,
        "path_plan": {"pattern_count": len(query.expected_hours or query.paths),
                      "first_pattern": query.paths[0], "last_pattern": query.paths[-1]},
        "units": query.units,
    }
    try:
        connection = connect()
        files, missing = resolve_paths(connection, query.paths, query.expected_hours)
        # Threads here overlap network round trips, so they are worth only as
        # much as there are objects to fetch. A two-file query given 64 of them
        # buys nothing and runs its window functions out of memory.
        connection.execute(f"SET threads={min(MAX_READ_THREADS, max(4, len(files)))};")
        source["path_plan"].update(resolved_file_count=len(files),
                                   missing_pattern_count=len(missing))
        if missing:
            source["path_plan"]["missing_patterns"] = missing[:10]
        if not files:
            source.update(status="unavailable", row_count=0,
                          error=f"no objects matched any of {len(query.paths)} partition "
                                f"patterns between {query.paths[0]} and {query.paths[-1]}")
            return source, []
        result = connection.execute(query.sql.replace("__PATHS__", sql_list(files)))
        columns = [column[0] for column in result.description]
        rows = [dict(zip(columns, row)) for row in result.fetchall()]
    except duckdb.Error as exc:
        source.update(status="unavailable", row_count=0, error=str(exc)[-1000:])
        return source, []
    finally:
        if "connection" in locals():
            connection.close()
    source.update(status="ok", row_count=len(rows))
    source["partition_coverage"] = "partial" if missing else "all_planned_patterns_present"
    if query.name.startswith("option_surface_"):
        source["missing_observations"] = sorted(
            {"window_open", "latest"} - {row["observation"] for row in rows})
    if query.name.startswith("venue_blocks_"):
        source["selection"] = "all_matching_rows_in_window; boundary groups may be incomplete"
    if query.name.startswith("option_trades_"):
        source["selection"] = "all-row aggregates plus top 25 known-turnover-first sample; not a complete trade list"
    timestamps = [row.get("max_event_at") for row in rows if isinstance(row, dict) and row.get("max_event_at")]
    if timestamps:
        source["max_event_at"] = max(timestamps)
    return source, rows


def build_queries(asset: str, start: dt.datetime, end: dt.datetime, *, render=False) -> list[Query]:
    currency = asset.lower()
    start_iso, end_iso = start.isoformat(), end.isoformat()
    between = f"TRY_CAST(timestamp AS TIMESTAMPTZ) >= TIMESTAMPTZ '{start_iso}' AND TRY_CAST(timestamp AS TIMESTAMPTZ) < TIMESTAMPTZ '{end_iso}'"
    queries: list[Query] = []
    for venue in VENUES:
        trade_paths, trade_hours = hour_patterns("normalized", venue, "option_trade", currency, start, end)
        queries.append(Query(f"option_trades_{venue}", trade_paths, f"""
          WITH trades AS MATERIALIZED (
            -- Named columns, not *: Parquet only fetches the ones asked for, and
            -- the whole window is held in memory here.
            SELECT exchange, timestamp, symbol, side, amount, price, iv, index_price,
                   turnover_usd, block_id, id, filename AS source_path
            FROM read_parquet(__PATHS__, union_by_name=true,
                              hive_partitioning=true, filename=true)
            WHERE {between}
          ), largest AS (
            SELECT * FROM trades
            ORDER BY turnover_usd DESC NULLS LAST, amount DESC NULLS LAST LIMIT 25
          )
          SELECT 'aggregate' AS record_type, exchange, count(*) AS trade_count,
                 sum(amount) AS amount_native,
                 CASE WHEN count(turnover_usd)=count(*) THEN sum(turnover_usd) END AS premium_turnover_usd,
                 sum(turnover_usd) AS known_premium_turnover_usd,
                 count(*) - count(turnover_usd) AS missing_turnover_count,
                 count(iv) AS iv_count,
                 least(count(*), 25) AS sampled_trade_count,
                 count(*) FILTER (WHERE lower(side)='buy') AS buy_count,
                 count(*) FILTER (WHERE lower(side)='sell') AS sell_count,
                 max(timestamp) AS max_event_at
          FROM trades GROUP BY exchange
          UNION ALL BY NAME
          SELECT 'trade' AS record_type, *, max(timestamp) OVER () AS max_event_at FROM largest
        """, {"amount_native": "venue-native; do not combine without metadata",
               "premium_turnover_usd": "USD; null unless every matching row has turnover",
               "known_premium_turnover_usd": "USD partial sum, not a complete total",
               "iv": "venue-native; normalized column names do not harmonize units"},
               True, tuple(trade_hours)))

        summary_paths = snapshot_patterns(venue, currency, start, end)
        open_point = start.replace(minute=start.minute - start.minute % 5, second=0, microsecond=0)
        latest_point = max(open_point, end.replace(minute=end.minute - end.minute % 5,
                                                 second=0, microsecond=0) - dt.timedelta(minutes=10))
        queries.append(Query(f"option_surface_{venue}", summary_paths, f"""
          WITH anchors(observation, bucket_start, anchor_at) AS (
            VALUES ('window_open', TIMESTAMPTZ '{open_point.isoformat()}', TIMESTAMPTZ '{start_iso}'),
                   ('latest', TIMESTAMPTZ '{latest_point.isoformat()}', TIMESTAMPTZ '{end_iso}')
          ), observations AS (
            SELECT *, row_number() OVER (
                PARTITION BY observation, exchange, symbol
                ORDER BY CASE WHEN observation='window_open' THEN timestamp END ASC,
                         timestamp DESC) AS snapshot_rank
            FROM read_parquet(__PATHS__, union_by_name=true, hive_partitioning=true), anchors
            WHERE {between}
              AND TRY_CAST(timestamp AS TIMESTAMPTZ) >= bucket_start
              AND TRY_CAST(timestamp AS TIMESTAMPTZ) < bucket_start + INTERVAL 5 MINUTE
              AND TRY_CAST(expirationDate AS TIMESTAMPTZ) > anchor_at
          ), snapshots AS (
            SELECT *, count(*) OVER (PARTITION BY observation) AS snapshot_symbol_count
            FROM observations WHERE snapshot_rank=1
          ), nodes AS (
            SELECT observation, CAST(anchor_at AS VARCHAR) AS anchor_at,
                   CAST(bucket_start AS VARCHAR) AS bucket_start, snapshot_symbol_count,
                   exchange, timestamp, symbol, expirationDate, strikePrice, optionType,
                   markIV, bestBidIV, bestAskIV, markPrice, bestBidPrice, bestAskPrice,
                   delta, gamma, vega, theta, openInterest, underlyingPrice,
                   target_delta, 'nearest_delta_per_expiry_and_type' AS selection,
                   max(timestamp) OVER (PARTITION BY observation) AS max_event_at,
                   row_number() OVER (
                     PARTITION BY observation, expirationDate, optionType, target_delta
                     ORDER BY abs(abs(delta)-target_delta) NULLS LAST, symbol
                   ) AS evidence_rank
            FROM snapshots CROSS JOIN (VALUES (0.25), (0.50)) AS targets(target_delta)
          )
          SELECT * EXCLUDE(evidence_rank), count(*) OVER (PARTITION BY observation) AS selected_node_count
          FROM nodes WHERE evidence_rank=1
          ORDER BY observation, expirationDate, optionType, target_delta
        """, {"markIV": "venue-native; convert using event-applicable instrument metadata",
               "openInterest": "venue-native snapshot sample, not full-chain OI"}, True))

        block_paths, block_hours = hour_patterns("raw", venue, "option_trade", currency, start, end)
        native_predicates = {
            "deribit": "block_trade_id IS NOT NULL OR block_rfq_id IS NOT NULL",
            "deribit-usdc": "block_trade_id IS NOT NULL OR block_rfq_id IS NOT NULL",
            "okex-options": "block_trade_id IS NOT NULL",
            "bybit-options": "is_block_trade=1",
            "bullish": "otc_trade_id IS NOT NULL OR otc_match_id IS NOT NULL",
        }
        queries.append(Query(f"venue_blocks_{venue}", block_paths, f"""
          SELECT *, filename AS source_path, max(timestamp) OVER () AS max_event_at
          FROM read_parquet(__PATHS__, union_by_name=true,
                            hive_partitioning=true, filename=true)
          WHERE {between} AND ({native_predicates[venue]})
          ORDER BY timestamp DESC
        """, {"numeric_fields": "venue-native; consult instrument metadata"},
               expected_hours=tuple(block_hours)))

    dvol_paths, dvol_hours = hour_patterns("raw", "deribit", "dvol", currency, start, end)
    queries.append(Query("dvol_window", dvol_paths, f"""
      SELECT asset, index_name, arg_min(volatility, timestamp) AS open,
             arg_max(volatility, timestamp) AS close, min(volatility) AS low,
             max(volatility) AS high, max(timestamp) AS max_event_at
      FROM read_parquet(__PATHS__, union_by_name=true, hive_partitioning=true)
      WHERE {between} GROUP BY asset, index_name
    """, {"open": "vol points", "close": "vol points", "low": "vol points", "high": "vol points"},
           expected_hours=tuple(dvol_hours)))

    for venue in ("deribit", "okex-options", "bybit-options"):
        perp_paths, perp_hours = hour_patterns("normalized", venue, "perp_summary", currency, start, end)
        queries.append(Query(f"perpetual_snapshot_{venue}", perp_paths, f"""
          SELECT exchange, timestamp, symbol, funding_rate, funding_interval_hours,
                 index_price, mark_price, open_interest_coin, open_interest_usd,
                 max(timestamp) OVER () AS max_event_at
          FROM read_parquet(__PATHS__, union_by_name=true, hive_partitioning=true)
          WHERE {between}
          QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY timestamp DESC)=1
        """, {"funding_rate": "published rate per funding_interval_hours", "index_price": "USD"},
               expected_hours=tuple(perp_hours)))
    if render:
        # Calculators need complete trades and a complete Deribit snapshot, not
        # the bounded examples formerly sent to the language model.
        queries = [Query(q.name, q.paths,
                         q.sql.replace(" LIMIT 25", "").replace(
                             "WHERE evidence_rank=1", "WHERE target_delta=0.50"),
                         q.units, q.required, q.expected_hours)
                   for q in queries if q.name.startswith("option_trades_")
                   or q.name in ("option_surface_deribit", "dvol_window")]
    return queries


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset", required=True)
    parser.add_argument("--window", required=True)
    parser.add_argument("--now", help="UTC ISO-8601 end time; intended for reproducible tests")
    parser.add_argument("--render", action="store_true")
    args = parser.parse_args()
    try:
        width = parse_window(args.window)
    except ValueError as exc:
        parser.error(str(exc))
    end = dt.datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else dt.datetime.now(dt.timezone.utc)
    end = end.astimezone(dt.timezone.utc)
    start = end - width
    if args.render:
        from direct_inputs import run
        print(run(args.asset.upper(), args.window, start, end))
        return 0
    queries = build_queries(args.asset.upper(), start, end)
    workers = query_workers(width)
    set_budget(workers)
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(workers, len(queries))) as pool:
        results = list(pool.map(run_query, queries))
    sources = [source for source, _ in results]
    for source in sources:
        source["requested_event_time"] = {"start_at": start.isoformat(), "end_at": end.isoformat()}
    evidence = {query.name: rows for query, (_, rows) in zip(queries, results)}
    gaps = [{"source": source["name"], "reason": source.get("error", "no rows")}
            for source in sources if source["status"] != "ok" or source["row_count"] == 0]
    gaps += [{"source": source["name"],
              "reason": f"partial coverage: {source['path_plan']['missing_pattern_count']} of "
                        f"{source['path_plan']['pattern_count']} partitions absent",
              "missing_patterns": source["path_plan"].get("missing_patterns", [])}
             for source in sources
             if source["status"] == "ok" and source["row_count"] > 0
             and source["path_plan"].get("missing_pattern_count")]
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "data-discovery" / "scripts"))
    from execution_tape import read_executions
    try:
        executions = read_executions(start, end, asset=args.asset)
        evidence["paradigm_executions"] = executions.pop("rows")
        sources.append({"name": "partitioned_paradigm_executions", "status": "ok",
                        "row_count": len(evidence["paradigm_executions"]), **executions})
    except Exception as exc:
        evidence["paradigm_executions"] = []
        sources.append({"name": "partitioned_paradigm_executions", "status": "unavailable", "error": str(exc)})
        gaps.append({"source": "partitioned_paradigm_executions", "reason": str(exc)})
    document = {
        "schema_version": "dime.recap.evidence.v1",
        "request": {"asset": args.asset.upper(), "window": args.window,
                    "start_at": start.isoformat(), "end_at": end.isoformat()},
        "sources": sources,
        "evidence": evidence,
        "gaps": gaps,
    }
    print(json.dumps(document, separators=(",", ":"), default=str))
    required_ok = any(source["status"] == "ok" and source["row_count"] > 0
                      for source, query in zip(sources, queries) if query.required)
    if not required_ok:
        print("recap: no core direct-data source produced evidence", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
