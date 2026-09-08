#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.3", "polars>=1.0", "boto3>=1.35"]
# ///
"""Collect authoritative RFQ and raw venue evidence without rendering analysis."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from typing import Any
from pathlib import Path

import duckdb

RFQ_TAPE = "s3://dt-paradigm-data/paradigm_data/paradigm_rfq_tape_slim.csv.gz"
TRADE_TAPE = "s3://dt-paradigm-data/paradigm_data/paradigm_trade_tape_slim.csv.gz"
FREEZE_DATE = "2026-08-10"

PREFIX = """
INSTALL httpfs; LOAD httpfs;
INSTALL aws; LOAD aws;
CREATE OR REPLACE SECRET dime_s3 (
  TYPE S3, PROVIDER CREDENTIAL_CHAIN, REGION 'ap-northeast-1'
);
"""


def run_sql(sql: str) -> tuple[list[dict[str, Any]], str | None]:
    try:
        connection = duckdb.connect()
        connection.execute(PREFIX)
        result = connection.execute(sql)
        columns = [column[0] for column in result.description]
        return [dict(zip(columns, row)) for row in result.fetchall()], None
    except duckdb.Error as exc:
        return [], str(exc)[-1000:]
    finally:
        if "connection" in locals():
            connection.close()


def existing_paths(patterns: list[str]) -> list[str]:
    """Keep only the partition patterns S3 actually has.

    read_parquet() fails the whole list on a single unmatched pattern, so the
    forward hour around a recent trade — which producers have not written yet —
    would drop the venue read entirely and silently downgrade the analysis
    confidence from authoritative to request-only. glob() tolerates a miss.
    """
    quoted = "[" + ",".join("'" + path + "'" for path in patterns) + "]"
    files, error = run_sql(f"SELECT file FROM glob({quoted}) ORDER BY file")
    if error:
        raise RuntimeError(error)
    matched = [row["file"] for row in files]
    return [pattern for pattern in patterns
            if any(f.startswith(pattern.split("*", 1)[0]) for f in matched)]


def suffix_predicate(column: str, core: str) -> str:
    escaped = core.replace("'", "''")
    return f"upper(CAST({column} AS VARCHAR)) = upper('{escaped}') OR upper(CAST({column} AS VARCHAR)) LIKE upper('%{escaped}')"


def value(row: dict[str, Any], *names: str) -> Any:
    folded = {key.lower(): val for key, val in row.items()}
    return next((folded[name.lower()] for name in names if folded.get(name.lower()) not in (None, "")), None)


def event_bounds(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    observed = []
    for row in rows:
        date = value(row, "DATE", "timestamp")
        time = value(row, "TIME")
        if date:
            observed.append(f"{date}T{time}" if time else str(date))
    return {"min_event_at": min(observed), "max_event_at": max(observed)} if observed else None


def raw_deribit_paths(row: dict[str, Any]) -> list[str]:
    product = str(value(row, "PRODUCT", "product") or "").upper()
    description = str(value(row, "DESCRIPTION", "description") or "").upper()
    if not re.search(r"(?:^|\s|-)DBT(?:$|\s|-)", product):
        return []
    asset_match = re.search(r"\b(BTC|ETH|SOL|XRP)\b", product + " " + description)
    date_text = value(row, "traded_at_iso", "DATE", "CREATED_AT", "REQUESTED_AT", "RFQ_CREATED_AT", "TIMESTAMP")
    time_text = value(row, "TIME")
    if not asset_match or not date_text:
        return []
    try:
        event_at = dt.datetime.fromisoformat(str(date_text).replace("Z", "+00:00"))
    except ValueError:
        try:
            event_at = dt.datetime.combine(dt.date.fromisoformat(str(date_text)[:10]), dt.time())
        except ValueError:
            return []
    if time_text:
        try:
            event_at = dt.datetime.combine(event_at.date(), dt.time.fromisoformat(str(time_text)))
        except ValueError:
            pass
    hours = (event_at - dt.timedelta(hours=1), event_at, event_at + dt.timedelta(hours=1))
    paths = []
    for selected in hours:
        paths.append(
            "s3://dt-exchange-venue-data/raw/exchange=deribit/data_type=option_trade/"
            f"currency={asset_match.group(1).lower()}/level=5m/year={selected:%Y}/month={selected:%m}/"
            f"day={selected:%d}/hour={selected:%H}/**/*__rows__*.parquet"
        )
    return paths


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rfq-id", required=True)
    args = parser.parse_args()
    core = re.sub(r"^(DRFQv2-|GRFQ-)", "", args.rfq_id)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", core):
        parser.error("invalid RFQ id")

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "data-discovery" / "scripts"))
    from execution_tape import read_executions
    now = dt.datetime.now(dt.timezone.utc)
    execution, execution_error = None, None
    try:
        execution = read_executions(now - dt.timedelta(days=30), now, rfq_id=args.rfq_id, now=now)
    except Exception as exc:
        execution_error = str(exc)
    execution_rows = execution["rows"] if execution else []

    request_rows, request_error = run_sql(
        f"SELECT * FROM read_csv_auto('{RFQ_TAPE}', union_by_name=true) "
        f"WHERE {suffix_predicate('RFQ_ID', core)} LIMIT 20"
    )
    request_date = str(value(request_rows[0], "DATE") or "")[:10] if request_rows else ""
    historical_applicable = not request_rows or not request_date or request_date <= FREEZE_DATE
    if historical_applicable:
        historical_rows, historical_error = run_sql(
            f"SELECT * FROM read_csv_auto('{TRADE_TAPE}', union_by_name=true) "
            f"WHERE {suffix_predicate('RFQ_ID', core)} LIMIT 100"
        )
    else:
        historical_rows, historical_error = [], None

    anchor = execution_rows[0] if execution_rows else (request_rows[0] if request_rows else (historical_rows[0] if historical_rows else {}))
    venue_paths = raw_deribit_paths(anchor)
    venue_rows: list[dict[str, Any]] = []
    venue_error: str | None = None
    try:
        venue_paths = existing_paths(venue_paths) if venue_paths else venue_paths
    except RuntimeError as exc:
        venue_error = str(exc)
        venue_paths = []
    if venue_paths:
        quoted = "[" + ",".join("'" + path + "'" for path in venue_paths) + "]"
        venue_rows, venue_error = run_sql(
            "SELECT *, filename AS source_path FROM read_parquet("
            f"{quoted}, union_by_name=true, hive_partitioning=true, filename=true) "
            f"WHERE {suffix_predicate('block_rfq_id', core)} ORDER BY timestamp LIMIT 100"
        )

    if execution_rows:
        status, confidence = "execution_resolved_by_paradigm_rfq_id", "authoritative"
    elif venue_rows:
        status, confidence = "execution_resolved_by_venue_rfq_id", "authoritative"
    elif historical_rows:
        status, confidence = "historical_execution_resolved", "authoritative_historical"
    elif request_rows:
        status, confidence = "request_found_execution_unresolved", "request_only"
    else:
        status, confidence = "not_found", "none"

    sources = [
        {"name": "current_paradigm_rfq_tape", "path": RFQ_TAPE,
         "status": "unavailable" if request_error else "ok", "row_count": len(request_rows),
         "event_time_bounds": event_bounds(request_rows),
         "units": {"QTY": "product-native", "NOTIONAL_VOLUME_USD": "USD"},
         **({"error": request_error} if request_error else {})},
        {"name": "historical_paradigm_trade_tape", "path": TRADE_TAPE,
         "status": "not_applicable" if not historical_applicable else ("unavailable" if historical_error else "ok"),
         "row_count": len(historical_rows),
         "event_time_bounds": event_bounds(historical_rows),
         "units": {"PRICE": "QUOTE_CURRENCY", "REF_PRICE": "QUOTE_CURRENCY", "NOTIONAL_VOLUME_USD": "USD"},
         "valid_through": FREEZE_DATE, **({"error": historical_error} if historical_error else {})},
    ]
    if venue_paths:
        sources.append({"name": "raw_deribit_option_trades", "path_patterns": venue_paths,
                        "status": "unavailable" if venue_error else "ok", "row_count": len(venue_rows),
                        "event_time_bounds": event_bounds(venue_rows),
                        "units": {"amount": "coin", "price": "coin", "iv": "vol points"},
                        **({"error": venue_error} if venue_error else {})})
    gaps = []
    sources.append({"name": "partitioned_paradigm_executions",
                    "status": "unavailable" if execution_error else "ok",
                    "row_count": len(execution_rows),
                    **({k: v for k, v in execution.items() if k != "rows"} if execution else {}),
                    **({"error": execution_error} if execution_error else {})})
    if execution_error:
        gaps.append({"source": "partitioned_paradigm_executions", "reason": execution_error})
    if request_error:
        gaps.append({"source": "current_paradigm_rfq_tape", "reason": request_error})
    if venue_error:
        gaps.append({"source": "raw_deribit_option_trades", "reason": venue_error})
    if status == "request_found_execution_unresolved":
        gaps.append({"field": "execution", "reason": "No authoritative id-linked execution was available."})
    anchor_product = str(value(anchor, "PRODUCT", "product") or "").upper()
    if not venue_paths and anchor and "DBT" in anchor_product:
        gaps.append({"source": "raw_exchange_trades", "reason": "RFQ evidence did not establish both asset and event date for a bounded partition read."})

    print(json.dumps({
        "schema_version": "dime.analysis.evidence.v1",
        "request": {"rfq_id": args.rfq_id, "core_id": core},
        "resolution": {"status": status, "confidence": confidence},
        "rfq_requests": request_rows,
        "execution_candidates": {"paradigm_tape": execution_rows, "raw_venue": venue_rows, "historical_tape": historical_rows},
        "sources": sources,
        "gaps": gaps,
    }, separators=(",", ":"), default=str))
    if request_error and historical_error and not execution_rows:
        print("analyze: no authoritative source could be read", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
