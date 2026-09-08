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


def rfq_predicate(column: str, core: str) -> str:
    """Exact opaque IDs; only explicitly known RFQ namespaces are candidates."""
    ids = [core] if core.startswith(("DRFQv2-", "GRFQ-")) else [core, f"DRFQv2-{core}", f"GRFQ-{core}"]
    quoted = ",".join("'" + item.replace("'", "''") + "'" for item in ids)
    return f"CAST({column} AS VARCHAR) IN ({quoted})"


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
    parser.add_argument("--render", action="store_true")
    args = parser.parse_args()
    core = re.sub(r"^(DRFQv2-|GRFQ-)", "", args.rfq_id)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", core):
        parser.error("invalid RFQ id")

    if args.render:
        return render_current_analysis(args.rfq_id)

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "data-discovery" / "scripts"))
    from execution_tape import AmbiguousRfqError, read_executions
    now = dt.datetime.now(dt.timezone.utc)
    execution, execution_error = None, None
    try:
        execution = read_executions(now - dt.timedelta(days=30), now, rfq_id=args.rfq_id, now=now)
    except AmbiguousRfqError:
        raise
    except Exception as exc:
        execution_error = str(exc)
    execution_rows = execution["rows"] if execution else []

    request_rows, request_error = run_sql(
        f"SELECT * FROM read_csv_auto('{RFQ_TAPE}', union_by_name=true) "
        f"WHERE {rfq_predicate('RFQ_ID', args.rfq_id)}"
    )
    request_date = str(value(request_rows[0], "DATE") or "")[:10] if request_rows else ""
    historical_applicable = not request_rows or not request_date or request_date <= FREEZE_DATE
    if historical_applicable:
        historical_rows, historical_error = run_sql(
            f"SELECT * FROM read_csv_auto('{TRADE_TAPE}', union_by_name=true) "
            f"WHERE {rfq_predicate('RFQ_ID', args.rfq_id)}"
        )
    else:
        historical_rows, historical_error = [], None

    identities = {str(value(row, "rfq_id")) for row in execution_rows + request_rows + historical_rows
                  if value(row, "rfq_id") is not None}
    if len(identities) > 1:
        raise AmbiguousRfqError(f"Ambiguous RFQ ID; specify the exact namespace: {sorted(identities)}")

    anchor = execution_rows[0] if execution_rows else (request_rows[0] if request_rows else (historical_rows[0] if historical_rows else {}))
    venue_ids = sorted({str(row["venue_block_trade_id"]) for row in execution_rows
                        if row.get("venue_block_trade_id")})
    venue_paths = raw_deribit_paths(anchor) if venue_ids else []
    venue_rows: list[dict[str, Any]] = []
    venue_error: str | None = None
    try:
        venue_paths = existing_paths(venue_paths) if venue_paths else venue_paths
    except RuntimeError as exc:
        venue_error = str(exc)
        venue_paths = []
    if venue_paths:
        quoted = "[" + ",".join("'" + path + "'" for path in venue_paths) + "]"
        block_ids = ",".join("'" + item.replace("'", "''") + "'" for item in venue_ids)
        venue_rows, venue_error = run_sql(
            "SELECT *, filename AS source_path FROM read_parquet("
            f"{quoted}, union_by_name=true, hive_partitioning=true, filename=true) "
            f"WHERE CAST(block_trade_id AS VARCHAR) IN ({block_ids}) ORDER BY timestamp"
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
         "units": {"PRICE": "instrument-native", "REF_PRICE": "instrument-native", "NOTIONAL_VOLUME_USD": "USD notional, not premium turnover"},
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
    # Incomplete coverage is not an error, so it must be reported as a gap in
    # its own right: an uncovered tail is missing evidence, never no trading.
    if execution and not execution.get("coverage_complete", False):
        gaps.append({"source": "partitioned_paradigm_executions",
                     "reason": execution.get("coverage_note", "execution coverage incomplete")})
    if request_error:
        gaps.append({"source": "current_paradigm_rfq_tape", "reason": request_error})
    if venue_error:
        gaps.append({"source": "raw_deribit_option_trades", "reason": venue_error})
    if status == "request_found_execution_unresolved":
        gaps.append({"field": "execution", "reason": "No authoritative id-linked execution was available."})
    anchor_product = str(value(anchor, "PRODUCT", "product") or "").upper()
    if not venue_paths and anchor and "DBT" in anchor_product:
        gaps.append({"source": "raw_exchange_trades", "reason": "No proven venue block ID and bounded asset/event date lookup was available; RFQ suffixes are not venue identities."})

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


def render_current_analysis(rfq_id):
    """Resolve once from partitions, then reuse the established analyst."""
    from collections import defaultdict
    from analyze import analyze_rows, render
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "data-discovery" / "scripts"))
    from execution_tape import AmbiguousRfqError, calculation_rows, read_executions

    now = dt.datetime.now(dt.timezone.utc)
    try:
        tape = read_executions(now - dt.timedelta(days=30), now, now=now)
    except Exception as exc:
        print(f"RFQ {rfq_id} not resolved — partitioned executions unavailable: {exc}")
        return 1
    candidates = {rfq_id} if rfq_id.startswith(("DRFQv2-", "GRFQ-")) else {
        rfq_id, f"DRFQv2-{rfq_id}", f"GRFQ-{rfq_id}"}
    selected = [row for row in tape["rows"] if row["rfq_id"] in candidates]
    if len({row["rfq_id"] for row in selected}) > 1:
        raise AmbiguousRfqError("ambiguous RFQ ID; specify the exact namespace")
    if not selected:
        # Distinguish "searched complete data, not there" from "searched data
        # that stops short of now" — the second is not a negative result.
        if not tape.get("coverage_complete", False):
            print(
                f"RFQ {rfq_id} not resolved — and execution coverage is incomplete: "
                f"{tape.get('coverage_note', 'coverage unknown')} "
                "This is missing evidence, not proof the RFQ did not trade."
            )
            return 1
        print(f"RFQ {rfq_id} not resolved — no authoritative asset, structure, or fill available.")
        return 0
    for row in selected:
        if (row["quantity"] is None or row["quantity"] <= 0
                or row["trade_price"] is None or row["mark_price"] is None
                or row["taker_side"] not in ("BUY", "SELL")):
            raise ValueError("execution leg is missing required quantity, fill, mark or side")

    def signature(rows):
        quantities = defaultdict(float)
        for row in rows:
            quantities[(row["venue"], row["instrument_name"], row["taker_side"])] += row["quantity"]
        base = min(quantities.values())
        return tuple(sorted((*key, qty / base) for key, qty in quantities.items()))

    groups = defaultdict(list)
    for row in tape["rows"]:
        if row["block_trade_id"] and row["rfq_id"] not in candidates:
            groups[(row["venue"], row["block_trade_id"])].append(row)
    wanted = signature(selected)
    history = [row for group in groups.values()
               if all(r["quantity"] is not None and r["quantity"] > 0 for r in group)
               and signature(group) == wanted for row in group]
    result = analyze_rows(combine_fills(calculation_rows(selected)), calculation_rows(history), int(now.timestamp() * 1000))
    print(render(result))
    return 0


def combine_fills(rows):
    """Multiple clips of one RFQ: total size and quantity-weighted leg prices."""
    import polars as pl
    keys = ["PRODUCT", "DESCRIPTION", "SIDE", "QUOTE_CURRENCY"]
    preserved = [key for key in rows[0] if key not in keys + ["QTY", "PRICE", "REF_PRICE", "NOTIONAL_VOLUME_USD"]]
    return (pl.from_dicts(rows, infer_schema_length=None).lazy()
            .group_by(keys, maintain_order=True)
            .agg(pl.col("QTY").sum(),
                 ((pl.col("PRICE") * pl.col("QTY")).sum() / pl.col("QTY").sum()).alias("PRICE"),
                 ((pl.col("REF_PRICE") * pl.col("QTY")).sum() / pl.col("QTY").sum()).alias("REF_PRICE"),
                 pl.col("NOTIONAL_VOLUME_USD").sum(),
                 *[pl.col(key).first() for key in preserved])
            .collect().to_dicts())


if __name__ == "__main__":
    raise SystemExit(main())
