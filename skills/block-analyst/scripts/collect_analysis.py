#!/usr/bin/env python3
"""Collect authoritative RFQ and raw venue evidence without rendering analysis."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from typing import Any

RFQ_TAPE = "s3://dt-paradigm-data/paradigm_data/paradigm_rfq_tape_slim.csv.gz"
TRADE_TAPE = "s3://dt-paradigm-data/paradigm_data/paradigm_trade_tape_slim.csv.gz"
FREEZE_DATE = "2026-08-10"

PREFIX = """
LOAD httpfs;
LOAD aws;
CREATE OR REPLACE SECRET dime_s3 (
  TYPE S3, PROVIDER CREDENTIAL_CHAIN, REGION 'ap-northeast-1'
);
"""


def run_sql(sql: str) -> tuple[list[dict[str, Any]], str | None]:
    try:
        completed = subprocess.run(
            [os.environ.get("DIME_DUCKDB", "duckdb"), "-json"],
            input=PREFIX + sql, capture_output=True, check=False, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], str(exc)
    if completed.returncode:
        return [], (completed.stderr.strip() or "DuckDB query failed")[-1000:]
    try:
        return json.loads(completed.stdout or "[]"), None
    except json.JSONDecodeError as exc:
        return [], f"DuckDB returned invalid JSON: {exc}"


def suffix_predicate(column: str, core: str) -> str:
    # Exact id, or the core after a separator ('DRFQv2-<core>'). A bare
    # '%<core>' suffix also matched unrelated ids whose tail happened to end
    # in the supplied digits.
    escaped = core.replace("'", "''")
    return (f"upper(CAST({column} AS VARCHAR)) = upper('{escaped}') "
            f"OR upper(CAST({column} AS VARCHAR)) LIKE upper('%-{escaped}')")


def pick_anchor(rows: list[dict[str, Any]], supplied: str, core: str
                ) -> tuple[dict[str, Any], str | None]:
    """The row that drives partition selection. Prefer an exact id match;
    refuse to anchor when distinct RFQ ids match the suffix, so a loose
    suffix can never source evidence for the wrong trade."""
    if not rows:
        return {}, None
    wanted = {supplied.upper(), core.upper()}
    for row in rows:
        if str(value(row, "RFQ_ID") or "").upper() in wanted:
            return row, None
    distinct = {str(value(row, "RFQ_ID") or "").upper() for row in rows}
    if len(distinct) > 1:
        return {}, ("multiple distinct RFQ ids match the supplied suffix; "
                    "refusing to anchor partition reads on any of them")
    return rows[0], None


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
    # Asset comes from the tape's structured PRODUCT field only. DESCRIPTION is
    # free text; letting it establish the asset would let unstructured content
    # steer which partitions get read.
    product = str(value(row, "PRODUCT", "product") or "").upper()
    asset_match = re.search(r"\b(BTC|ETH|SOL|XRP)\b", product)
    date_text = value(row, "DATE", "CREATED_AT", "REQUESTED_AT", "RFQ_CREATED_AT", "TIMESTAMP")
    time_text = value(row, "TIME")
    if not asset_match or not date_text:
        return []
    timed = False
    try:
        event_at = dt.datetime.fromisoformat(str(date_text).replace("Z", "+00:00"))
        timed = "T" in str(date_text) or " " in str(date_text).strip()
    except ValueError:
        try:
            event_at = dt.datetime.combine(dt.date.fromisoformat(str(date_text)[:10]), dt.time())
        except ValueError:
            return []
    if time_text:
        try:
            event_at = dt.datetime.combine(event_at.date(), dt.time.fromisoformat(str(time_text)))
            timed = True
        except ValueError:
            pass
    if timed:
        hours = (event_at - dt.timedelta(hours=1), event_at, event_at + dt.timedelta(hours=1))
    else:
        # No time of day established — read the whole event date rather than
        # silently anchoring on midnight and missing an afternoon execution.
        hours = tuple(dt.datetime.combine(event_at.date(), dt.time(hour))
                      for hour in range(24))
    paths = []
    for selected in hours:
        pattern = (
            "s3://dt-exchange-venue-data/raw/exchange=deribit/data_type=option_trade/"
            f"currency={asset_match.group(1).lower()}/level=5m/year={selected:%Y}/month={selected:%m}/"
            f"day={selected:%d}/hour={selected:%H}/**/*__rows__*.parquet"
        )
        if pattern not in paths:
            paths.append(pattern)
    return paths


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rfq-id", required=True)
    args = parser.parse_args()
    core = re.sub(r"^(DRFQv2-|GRFQ-)", "", args.rfq_id)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", core):
        parser.error("invalid RFQ id")

    request_rows, request_error = run_sql(
        f"SELECT * FROM read_csv_auto('{RFQ_TAPE}', union_by_name=true) "
        f"WHERE {suffix_predicate('RFQ_ID', core)} LIMIT 20"
    )
    request_anchor, anchor_error = pick_anchor(request_rows, args.rfq_id, core)
    request_date = str(value(request_anchor, "DATE") or "")[:10] if request_anchor else ""
    historical_applicable = not request_rows or not request_date or request_date <= FREEZE_DATE
    if historical_applicable:
        historical_rows, historical_error = run_sql(
            f"SELECT * FROM read_csv_auto('{TRADE_TAPE}', union_by_name=true) "
            f"WHERE {suffix_predicate('RFQ_ID', core)} LIMIT 100"
        )
    else:
        historical_rows, historical_error = [], None

    anchor = request_anchor
    if not anchor:
        anchor, historical_anchor_error = pick_anchor(historical_rows, args.rfq_id, core)
        anchor_error = anchor_error or historical_anchor_error
    venue_paths = raw_deribit_paths(anchor)
    venue_rows: list[dict[str, Any]] = []
    venue_error: str | None = None
    if venue_paths:
        quoted = "[" + ",".join("'" + path + "'" for path in venue_paths) + "]"
        venue_rows, venue_error = run_sql(
            "SELECT *, filename AS source_path FROM read_parquet("
            f"{quoted}, union_by_name=true, hive_partitioning=true, filename=true) "
            f"WHERE {suffix_predicate('block_rfq_id', core)} ORDER BY timestamp LIMIT 100"
        )

    if venue_rows:
        status, confidence = "execution_resolved_by_venue_rfq_id", "authoritative"
    elif historical_rows:
        status, confidence = "historical_execution_resolved", "authoritative_historical"
    elif request_rows:
        status, confidence = "request_found_execution_unresolved", "request_only"
    elif request_error:
        # A failed tape read is not evidence of absence: never report a
        # fabricated not_found when the authoritative source was unreadable.
        status, confidence = "sources_unavailable", "none"
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
    # Every unavailable source is a gap: an agent reading gaps as the list of
    # what is missing must see failed historical/venue reads too, not only
    # the RFQ-tape failure.
    gaps = [{"source": source["name"], "reason": source.get("error", "unavailable")}
            for source in sources if source["status"] == "unavailable"]
    if anchor_error:
        gaps.append({"source": "anchor", "reason": anchor_error})
    if status == "request_found_execution_unresolved":
        gaps.append({"field": "execution", "reason": "No authoritative id-linked execution was available."})
    if not venue_paths and anchor:
        gaps.append({"source": "raw_exchange_trades", "reason": "RFQ evidence did not establish both asset and event date for a bounded partition read."})

    print(json.dumps({
        "schema_version": "dime.analysis.evidence.v1",
        "request": {"rfq_id": args.rfq_id, "core_id": core},
        "resolution": {"status": status, "confidence": confidence},
        "rfq_requests": request_rows,
        "execution_candidates": {"raw_venue": venue_rows, "historical_tape": historical_rows},
        "sources": sources,
        "gaps": gaps,
    }, separators=(",", ":"), default=str))
    if status == "sources_unavailable":
        print("analyze: no authoritative source could be read", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
