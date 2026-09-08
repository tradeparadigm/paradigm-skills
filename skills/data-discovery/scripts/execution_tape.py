"""Bounded current Paradigm execution reads; no listing or private UM access."""

from datetime import datetime, timedelta, timezone
from io import BytesIO

import boto3
import polars as pl

BUCKET = "dt-exchange-venue-data"
PREFIX = "paradigm_trade_tape"


def read_executions(start, end, *, rfq_id=None, asset=None, s3=None, now=None):
    """Read exact UTC daily objects, returning every matching execution leg.

    A missing, unreadable or stale day raises; it is never silently skipped.
    Publication can lag the requested end: report observed coverage explicitly.
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
        coverage_start = int(metadata["build_window_start_ms"])
        coverage_end = int(metadata["build_window_end_ms"])
        if not timedelta(0) <= now - published <= timedelta(minutes=20):
            raise RuntimeError(f"stale or future-dated execution partition: {key}")
        if coverage_start > int(start.timestamp() * 1000):
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
                "build_window_end_ms": coverage_end,
            }
        )
        day += timedelta(days=1)
    result = pl.concat(frames).sort(["traded_at", "trade_id"])
    if (
        result["trade_id"].null_count()
        or result["trade_id"].n_unique() != result.height
    ):
        raise RuntimeError(
            "execution read has duplicate/null trade IDs; retry after publication completes"
        )
    return {
        "rows": result.to_dicts(),
        "sources": sources,
        "build_window_end_ms": min(item["build_window_end_ms"] for item in sources),
        "units": {
            "quantity": "product-native",
            "trade_price": "instrument-native premium price",
            "mark_price": "instrument-native premium price",
            "notional_volume_usd": "USD notional, not premium turnover",
        },
    }
