#!/usr/bin/env python3
"""Collect bounded direct exchange evidence without deciding the recap narrative."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

BUCKET = "s3://dt-exchange-venue-data"
VENUES = ("deribit", "deribit-usdc", "okex-options", "bybit-options", "bullish")


MAX_WINDOW = dt.timedelta(days=31)


def parse_window(value: str) -> dt.timedelta:
    match = re.fullmatch(r"([1-9][0-9]{0,6})([mhd])", value.lower())
    if not match:
        raise ValueError("window must be a positive Nm, Nh, or Nd value")
    amount, unit = int(match.group(1)), match.group(2)
    width = dt.timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[unit]: amount})
    if width > MAX_WINDOW:
        raise ValueError("window too large — max 31d")
    return width


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


def snapshot_points(start: dt.datetime, end: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """Window-open bucket and the most recent bucket old enough to be complete.

    The latest point sits two five-minute buckets back from the window end so a
    bucket still being written is never treated as the closing observation."""
    floor = lambda value: value.replace(minute=value.minute - value.minute % 5,
                                        second=0, microsecond=0)
    open_point = floor(start)
    latest_point = max(open_point, floor(end) - dt.timedelta(minutes=10))
    return open_point, latest_point


def snapshot_patterns(venue: str, currency: str, start: dt.datetime,
                      end: dt.datetime) -> list[str]:
    """Exact five-minute snapshot buckets for the two snapshot_points."""
    points = sorted(set(snapshot_points(start, end)))
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
SET TimeZone='UTC';
LOAD httpfs;
LOAD aws;
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


def run_query(query: Query) -> tuple[dict[str, Any], list[Any]]:
    source: dict[str, Any] = {
        "name": query.name,
        "path_plan": {"pattern_count": len(query.paths),
                      "first_pattern": query.paths[0], "last_pattern": query.paths[-1]},
        "units": query.units,
    }
    try:
        completed = subprocess.run(
            [os.environ.get("DIME_DUCKDB", "duckdb"), "-json"],
            input=DUCKDB_PREFIX + query.sql, capture_output=True, check=False,
            text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        source.update(status="unavailable", row_count=0, error=str(exc))
        return source, []
    if completed.returncode:
        source.update(status="unavailable", row_count=0,
                      error=(completed.stderr.strip() or "DuckDB query failed")[-1000:])
        return source, []
    try:
        rows = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError as exc:
        source.update(status="unavailable", row_count=0,
                      error=f"DuckDB returned invalid JSON: {exc}")
        return source, []
    source.update(status="ok", row_count=len(rows))
    timestamps = [row.get("max_event_at") for row in rows if isinstance(row, dict) and row.get("max_event_at")]
    if timestamps:
        source["max_event_at"] = max(timestamps)
    return source, rows


def build_queries(asset: str, start: dt.datetime, end: dt.datetime) -> list[Query]:
    currency = asset.lower()
    start_iso, end_iso = start.isoformat(), end.isoformat()
    # CAST (not TRY_CAST): a timestamp column the cast cannot handle must fail
    # the query visibly, not silently filter every row into a "no rows" gap.
    # DUCKDB_PREFIX pins the session TimeZone to UTC so the cast is
    # deterministic regardless of the host's TZ.
    between = f"CAST(timestamp AS TIMESTAMPTZ) >= TIMESTAMPTZ '{start_iso}' AND CAST(timestamp AS TIMESTAMPTZ) < TIMESTAMPTZ '{end_iso}'"
    queries: list[Query] = []
    for venue in VENUES:
        trade_paths = hour_patterns("normalized", venue, "option_trade", currency, start, end)
        queries.append(Query(f"option_trades_{venue}", trade_paths, f"""
          WITH trades AS MATERIALIZED (
            SELECT * FROM read_parquet({sql_list(trade_paths)}, union_by_name=true,
                                       hive_partitioning=true, filename=true)
            WHERE {between}
          ), largest AS (
            SELECT exchange, timestamp, symbol, side, amount, price, iv, index_price,
                   turnover_usd, block_id, id, filename AS source_path
            FROM trades ORDER BY turnover_usd DESC NULLS LAST, amount DESC NULLS LAST LIMIT 25
          )
          SELECT 'aggregate' AS record_type, exchange, count(*) AS trade_count,
                 sum(amount) AS amount_native, sum(turnover_usd) AS premium_turnover_usd,
                 count(turnover_usd) AS turnover_rows,
                 count(*) FILTER (WHERE lower(side)='buy') AS buy_count,
                 count(*) FILTER (WHERE lower(side)='sell') AS sell_count,
                 count(*) FILTER (WHERE side IS NULL OR lower(side) NOT IN ('buy','sell'))
                   AS side_unclassified,
                 max(timestamp) AS max_event_at
          FROM trades GROUP BY exchange
          UNION ALL BY NAME
          SELECT 'trade' AS record_type, *, max(timestamp) OVER () AS max_event_at FROM largest
        """, {"amount_native": "venue-native; do not combine without metadata",
               "premium_turnover_usd": "USD; partial when turnover_rows < trade_count",
               "iv": "normalized vol points"}, True))

        open_point, latest_point = snapshot_points(start, end)
        open_cutoff = (open_point + dt.timedelta(minutes=5)).isoformat()
        bucket_lo, bucket_hi = open_point.isoformat(), (latest_point + dt.timedelta(minutes=5)).isoformat()
        summary_paths = snapshot_patterns(venue, currency, start, end)
        # Label rows by which five-minute bucket they landed in (the paths read
        # exactly two), not by per-symbol rank: rank-labeling promoted a symbol
        # listed intraday to a bogus window_open observation. The window filter
        # is the bucket span, not [start, end) — a snapshot written at the top
        # of the open bucket is still the window-open observation even when
        # `start` falls mid-bucket. Rows are capped per (observation, expiry)
        # nearest |delta|=0.5 so window_open rows and every expiry survive; the
        # old global `ORDER BY observation ... LIMIT 60` sorted 'latest' first
        # and starved window_open entirely.
        queries.append(Query(f"option_surface_{venue}", summary_paths, f"""
          WITH observations AS (
            SELECT *, CASE WHEN CAST(timestamp AS TIMESTAMPTZ) < TIMESTAMPTZ '{open_cutoff}'
                           THEN 'window_open' ELSE 'latest' END AS observation
            FROM read_parquet({sql_list(summary_paths)}, union_by_name=true, hive_partitioning=true)
            WHERE CAST(timestamp AS TIMESTAMPTZ) >= TIMESTAMPTZ '{bucket_lo}'
              AND CAST(timestamp AS TIMESTAMPTZ) < TIMESTAMPTZ '{bucket_hi}'
          ), deduped AS (
            SELECT * FROM observations
            QUALIFY row_number() OVER (PARTITION BY observation, symbol ORDER BY timestamp DESC) = 1
          )
          SELECT observation, exchange, timestamp, symbol, expirationDate, strikePrice, optionType,
                 markIV, bestBidIV, bestAskIV, markPrice, bestBidPrice, bestAskPrice,
                 delta, gamma, vega, theta, openInterest, underlyingPrice,
                 max(timestamp) OVER () AS max_event_at
          FROM deduped
          QUALIFY row_number() OVER (PARTITION BY observation, expirationDate
                                     ORDER BY abs(abs(delta) - 0.5)) <= 8
        """, {"markIV": "vol points", "openInterest": "coin where normalized metadata supports it",
               "row_policy": "per observation and expiry: 8 rows nearest |delta|=0.5"}, True))

        block_paths = hour_patterns("raw", venue, "option_trade", currency, start, end)
        native_predicates = {
            "deribit": "block_trade_id IS NOT NULL OR block_rfq_id IS NOT NULL",
            "deribit-usdc": "block_trade_id IS NOT NULL OR block_rfq_id IS NOT NULL",
            "okex-options": "block_trade_id IS NOT NULL",
            "bybit-options": "TRY_CAST(is_block_trade AS BOOLEAN) IS TRUE",
            "bullish": "otc_trade_id IS NOT NULL OR otc_match_id IS NOT NULL",
        }
        queries.append(Query(f"venue_blocks_{venue}", block_paths, f"""
          SELECT *, filename AS source_path, max(timestamp) OVER () AS max_event_at
          FROM read_parquet({sql_list(block_paths)}, union_by_name=true,
                            hive_partitioning=true, filename=true)
          WHERE {between} AND ({native_predicates[venue]})
          ORDER BY timestamp DESC LIMIT 50
        """, {"numeric_fields": "venue-native; consult instrument metadata"}))

    dvol_paths = hour_patterns("raw", "deribit", "dvol", currency, start, end)
    queries.append(Query("dvol_window", dvol_paths, f"""
      SELECT asset, index_name, arg_min(volatility, timestamp) AS open,
             arg_max(volatility, timestamp) AS close, min(volatility) AS low,
             max(volatility) AS high, max(timestamp) AS max_event_at
      FROM read_parquet({sql_list(dvol_paths)}, union_by_name=true, hive_partitioning=true)
      WHERE {between} GROUP BY asset, index_name
    """, {"open": "vol points", "close": "vol points", "low": "vol points", "high": "vol points"}))

    for venue in ("deribit", "okex-options", "bybit-options"):
        perp_paths = hour_patterns("normalized", venue, "perp_summary", currency, start, end)
        queries.append(Query(f"perpetual_snapshot_{venue}", perp_paths, f"""
          SELECT exchange, timestamp, symbol, funding_rate, funding_interval_hours,
                 index_price, mark_price, open_interest_coin, open_interest_usd,
                 max(timestamp) OVER () AS max_event_at
          FROM read_parquet({sql_list(perp_paths)}, union_by_name=true, hive_partitioning=true)
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
    except (ValueError, OverflowError) as exc:
        parser.error(str(exc))
    end = dt.datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else dt.datetime.now(dt.timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=dt.timezone.utc)
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
