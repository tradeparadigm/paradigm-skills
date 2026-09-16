"""Bounded current Paradigm execution reads; no listing or private UM access."""

from datetime import datetime, timedelta, timezone
from io import BytesIO

import boto3
import polars as pl

BUCKET = "dt-exchange-venue-data"
PREFIX = "paradigm_trade_tape"

# Dead-writer detection ONLY. The producer runs every 15 minutes with an
# activeDeadlineSeconds of 10 minutes, so a healthy publication can legitimately
# be ~25 minutes old after one slow or skipped tick; 45 tolerates two. This
# threshold says nothing about how current the DATA is -- that is the watermark
# below. The previous 20-minute rule conflated the two, which both rejected
# healthy partitions and, when it passed, let a lagging landing read as a
# quiet market.
MAX_PUBLICATION_AGE = timedelta(minutes=45)


class AmbiguousRfqError(RuntimeError):
    """A bare RFQ ID resolved to more than one source namespace."""


def calculation_rows(rows):
    """Map published leg fields to the existing calculators' tape interface.

    Geometry comes from the typed instrument columns, not the RFQ's shorthand
    description, which can describe the entire package on every leg.
    """
    result = []
    for row in rows:
        event = datetime.fromtimestamp(row["traded_at"] / 1000, timezone.utc)
        description = row["description"]
        if row["instrument_kind"] == "OPTION":
            expiry = datetime.fromisoformat(row["expiry_date"])
            description = (f"{row['option_kind'].title()} {expiry:%d %b %y} "
                           f"{row['strike_price']:g}")
        result.append({
            "RFQ_ID": row["rfq_id"], "BLOCK_TRADE_ID": row["block_trade_id"],
            "VENUE_BLOCK_TRADE_ID": row["venue_block_trade_id"],
            "PRODUCT": row["product"], "DESCRIPTION": description,
            "QUOTE_CURRENCY": row["asset"], "QTY": row["quantity"],
            "PRICE": row["trade_price"], "REF_PRICE": row["mark_price"],
            "SIDE": row["taker_side"], "NOTIONAL_VOLUME_USD": row["notional_volume_usd"],
            "DATE": event.strftime("%Y-%m-%d"), "TIME": event.strftime("%H:%M:%S"),
        })
    return result


def read_executions(start, end, *, rfq_id=None, asset=None, s3=None, now=None):
    """Read exact UTC daily objects, returning every matching execution leg.

    A missing, unreadable or stale day raises; it is never silently skipped.

    Two independent checks, deliberately not merged:

    - PUBLICATION age (`generated_at_ms`) proves the writer is alive. A
      stale or future-dated build raises -- that is a broken producer.
    - COVERAGE (`source_watermark_ms`) bounds how far the data reaches.
      The UM landing syncs hourly, so a request ending after the watermark
      is the NORMAL case and must not raise; it returns
      `coverage_complete=False` plus the shortfall, so the caller reports a
      gap instead of presenting the uncovered tail as zero activity.

    The watermark is the newest event the producer saw, so it cannot separate
    an unsynced landing from a genuinely quiet market. It is therefore a
    lower bound: safe against inventing quiet, occasionally withholding real
    quiet as unknown.
    """
    now = now or datetime.now(timezone.utc)
    if start.tzinfo is None or end.tzinfo is None or not start < end:
        raise ValueError("expected timezone-aware start < end")
    start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    oldest = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=30)
    if start < oldest or end > now:
        raise ValueError("execution tape supports the trailing 30 days only")
    s3 = s3 or boto3.client("s3", region_name="ap-northeast-1")
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    frames, sources = [], []
    while day < end:
        key = (
            f"{PREFIX}/year={day:%Y}/month={day:%m}/day={day:%d}/"
            f"paradigm_trade_tape__{day:%Y%m%d}.parquet"
        )
        obj = s3.get_object(Bucket=BUCKET, Key=key)
        metadata = obj["Metadata"]
        published = datetime.fromtimestamp(
            int(metadata["generated_at_ms"]) / 1000, timezone.utc
        )
        build_start = int(metadata["build_window_start_ms"])
        build_end = int(metadata["build_window_end_ms"])
        # Objects published before the watermark field exists report coverage
        # as UNKNOWN. Never fall back to the build clock: that is the
        # overstatement this field was added to remove.
        raw_watermark = metadata.get("source_watermark_ms")
        watermark = int(raw_watermark) if raw_watermark else None
        if not timedelta(0) <= now - published <= MAX_PUBLICATION_AGE:
            raise RuntimeError(
                "execution partition publication stale or future-dated "
                f"(generated_at {published.isoformat()}, now {now.isoformat()}): {key}"
            )
        if build_start > int(start.timestamp() * 1000):
            raise RuntimeError(
                f"execution partition does not cover requested start: {key}"
            )
        frame = pl.scan_parquet(BytesIO(obj["Body"].read())).filter(
            (pl.col("traded_at") >= int(start.timestamp() * 1000))
            & (pl.col("traded_at") < int(end.timestamp() * 1000))
        )
        if rfq_id:
            core = rfq_id.removeprefix("DRFQv2-").removeprefix("GRFQ-")
            ids = (
                [rfq_id]
                if rfq_id.startswith(("DRFQv2-", "GRFQ-"))
                else [core, f"DRFQv2-{core}", f"GRFQ-{core}"]
            )
            frame = frame.filter(pl.col("rfq_id").is_in(ids))
        if asset:
            frame = frame.filter(
                (pl.col("asset") == asset.upper())
                & (pl.col("instrument_kind") == "OPTION")
            )
        frames.append(frame.collect())
        sources.append(
            {
                "path": f"s3://{BUCKET}/{key}",
                "generated_at": published.isoformat(),
                "build_window_end_ms": build_end,
                "source_watermark_ms": watermark,
            }
        )
        day += timedelta(days=1)
    result = pl.concat(frames).sort(["traded_at", "trade_id"])
    if rfq_id and result["rfq_id"].n_unique() > 1:
        raise AmbiguousRfqError("ambiguous RFQ ID; specify the exact DRFQv2- or GRFQ- namespace")
    if (
        result["trade_id"].null_count()
        or result["trade_id"].n_unique() != result.height
    ):
        raise RuntimeError(
            "execution read has duplicate/null trade IDs; retry after publication completes"
        )
    # Coverage is bounded by the WEAKEST watermark in the read set, and a
    # single unknown makes the whole read's coverage unknown -- an unknown
    # must never average away against a known-good sibling day.
    end_ms = int(end.timestamp() * 1000)
    watermarks = [item["source_watermark_ms"] for item in sources]
    watermark = None if any(w is None for w in watermarks) else min(watermarks)
    coverage_end_ms = None if watermark is None else min(end_ms, watermark)
    coverage_complete = coverage_end_ms is not None and coverage_end_ms >= end_ms
    shortfall_seconds = (
        None if coverage_end_ms is None else max(0, (end_ms - coverage_end_ms) / 1000)
    )
    if coverage_complete:
        coverage_note = "observed executions cover the full requested window"
    elif coverage_end_ms is None:
        coverage_note = (
            "coverage UNKNOWN: partitions predate the source watermark field. "
            "Absence of executions is not evidence of no trading."
        )
    else:
        coverage_note = (
            f"coverage ends {shortfall_seconds / 60:.0f} min before the requested end "
            "(hourly upstream sync). Report the uncovered tail as missing evidence, "
            "NOT as zero activity."
        )
    return {
        "rows": result.to_dicts(),
        "sources": sources,
        "build_window_end_ms": min(item["build_window_end_ms"] for item in sources),
        "source_watermark_ms": watermark,
        "coverage_end_ms": coverage_end_ms,
        "coverage_complete": coverage_complete,
        "coverage_shortfall_seconds": shortfall_seconds,
        "coverage_note": coverage_note,
        "units": {
            "quantity": "product-native",
            "trade_price": "instrument-native premium price",
            "mark_price": "instrument-native premium price",
            "notional_volume_usd": "USD notional, not premium turnover",
        },
    }
