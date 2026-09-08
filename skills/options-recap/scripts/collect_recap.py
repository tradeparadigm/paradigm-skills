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
    return dt.timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[unit]: amount})


def hour_patterns(source: str, venue: str, data_type: str, currency: str,
                  start: dt.datetime, end: dt.datetime) -> list[str]:
    cursor = start.replace(minute=0, second=0, microsecond=0)
    patterns: list[str] = []
    while cursor < end:
        patterns.append(
            f"{BUCKET}/{source}/exchange={venue}/data_type={data_type}/currency={currency}/"
            f"level=5m/year={cursor:%Y}/month={cursor:%m}/day={cursor:%d}/hour={cursor:%H}/"
            "**/*__rows__*.parquet"
        )
        cursor += dt.timedelta(hours=1)
    return patterns


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
  TYPE S3, PROVIDER CREDENTIAL_CHAIN, REGION 'ap-northeast-1'
);
"""


@dataclass(frozen=True)
class Query:
    name: str
    paths: list[str]
    sql: str
    units: dict[str, str]
    required: bool = False


def resolve_paths(connection: duckdb.DuckDBPyConnection,
                  patterns: list[str]) -> tuple[list[str], list[str]]:
    """Split the partition patterns into the ones S3 actually has, and the rest.

    read_parquet() fails the entire list when any single pattern matches no
    object, so the current hour — which producers only write ~10 minutes in —
    would otherwise erase every other hour in the window. glob() tolerates a
    miss, so the read is scoped to partitions that exist and the absent ones
    are reported rather than silently taking the whole source down with them.
    """
    files = [row[0] for row in
             connection.execute(f"SELECT file FROM glob({sql_list(patterns)}) ORDER BY file").fetchall()]
    present, missing = [], []
    for pattern in patterns:
        prefix = pattern.split("*", 1)[0]
        (present if any(f.startswith(prefix) for f in files) else missing).append(pattern)
    return files, missing


def run_query(query: Query) -> tuple[dict[str, Any], list[Any]]:
    source: dict[str, Any] = {
        "name": query.name,
        "path_plan": {"pattern_count": len(query.paths),
                      "first_pattern": query.paths[0], "last_pattern": query.paths[-1]},
        "units": query.units,
    }
    try:
        connection = duckdb.connect()
        connection.execute(DUCKDB_PREFIX)
        files, missing = resolve_paths(connection, query.paths)
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


def build_queries(asset: str, start: dt.datetime, end: dt.datetime) -> list[Query]:
    currency = asset.lower()
    start_iso, end_iso = start.isoformat(), end.isoformat()
    between = f"TRY_CAST(timestamp AS TIMESTAMPTZ) >= TIMESTAMPTZ '{start_iso}' AND TRY_CAST(timestamp AS TIMESTAMPTZ) < TIMESTAMPTZ '{end_iso}'"
    queries: list[Query] = []
    for venue in VENUES:
        trade_paths = hour_patterns("normalized", venue, "option_trade", currency, start, end)
        queries.append(Query(f"option_trades_{venue}", trade_paths, f"""
          WITH trades AS MATERIALIZED (
            SELECT * FROM read_parquet(__PATHS__, union_by_name=true,
                                       hive_partitioning=true, filename=true)
            WHERE {between}
          ), largest AS (
            SELECT exchange, timestamp, symbol, side, amount, price, iv, index_price,
                   turnover_usd, block_id, id, filename AS source_path
            FROM trades ORDER BY turnover_usd DESC NULLS LAST, amount DESC NULLS LAST LIMIT 25
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
               "iv": "venue-native; normalized column names do not harmonize units"}, True))

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

        block_paths = hour_patterns("raw", venue, "option_trade", currency, start, end)
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
        """, {"numeric_fields": "venue-native; consult instrument metadata"}))

    dvol_paths = hour_patterns("raw", "deribit", "dvol", currency, start, end)
    queries.append(Query("dvol_window", dvol_paths, f"""
      SELECT asset, index_name, arg_min(volatility, timestamp) AS open,
             arg_max(volatility, timestamp) AS close, min(volatility) AS low,
             max(volatility) AS high, max(timestamp) AS max_event_at
      FROM read_parquet(__PATHS__, union_by_name=true, hive_partitioning=true)
      WHERE {between} GROUP BY asset, index_name
    """, {"open": "vol points", "close": "vol points", "low": "vol points", "high": "vol points"}))

    for venue in ("deribit", "okex-options", "bybit-options"):
        perp_paths = hour_patterns("normalized", venue, "perp_summary", currency, start, end)
        queries.append(Query(f"perpetual_snapshot_{venue}", perp_paths, f"""
          SELECT exchange, timestamp, symbol, funding_rate, funding_interval_hours,
                 index_price, mark_price, open_interest_coin, open_interest_usd,
                 max(timestamp) OVER () AS max_event_at
          FROM read_parquet(__PATHS__, union_by_name=true, hive_partitioning=true)
          WHERE {between}
          QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY timestamp DESC)=1
        """, {"funding_rate": "published rate per funding_interval_hours", "index_price": "USD"}))
    return queries


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset", required=True)
    parser.add_argument("--window", required=True)
    parser.add_argument("--now", help="UTC ISO-8601 end time; intended for reproducible tests")
    args = parser.parse_args()
    try:
        width = parse_window(args.window)
    except ValueError as exc:
        parser.error(str(exc))
    end = dt.datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else dt.datetime.now(dt.timezone.utc)
    end = end.astimezone(dt.timezone.utc)
    start = end - width
    queries = build_queries(args.asset.upper(), start, end)
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(queries))) as pool:
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
