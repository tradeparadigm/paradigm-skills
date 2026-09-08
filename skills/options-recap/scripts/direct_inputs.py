"""Non-hot inputs for the existing recap calculator and renderer."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
import sys

import boto3
import polars as pl

import recap
from collect_recap import VENUES, build_queries, run_query

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "data-discovery" / "scripts"))
from execution_tape import calculation_rows, read_executions


def metadata(venue, asset, start, end):
    """One predecessor snapshot plus snapshots within the requested window."""
    s3 = boto3.client("s3", region_name="ap-northeast-1")
    prefix = f"meta/instruments/exchange={venue}/currency={asset.lower()}/"
    objects = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket="dt-exchange-venue-data", Prefix=prefix):
        for item in page.get("Contents", []):
            key = item["Key"]
            stamp = datetime.strptime(key.rsplit("__", 1)[1], "%Y%m%dT%H%M%SZ.parquet").replace(tzinfo=timezone.utc)
            if stamp <= end:
                objects.append((stamp, key))
    before = [item for item in objects if item[0] <= start]
    chosen = ([max(before)] if before else []) + [item for item in objects if start < item[0] <= end]
    if not chosen:
        raise ValueError(f"no event-applicable instrument metadata for {venue}")
    frames = []
    for _, key in sorted(chosen):
        obj = s3.get_object(Bucket="dt-exchange-venue-data", Key=key)
        frames.append(pl.scan_parquet(BytesIO(obj["Body"].read())).select(
            "symbol", "captured_at", "iv_unit", "oi_unit", "contract_size", "price_unit"))
    return pl.concat(frames).with_columns(
        pl.col("captured_at").str.to_datetime(time_zone="UTC")
    ).sort("captured_at").collect()


def with_units(rows, specs):
    """An as-of join never applies a future instrument spec to a past trade."""
    return (pl.from_dicts(rows, infer_schema_length=None).lazy()
            .with_columns(pl.col("timestamp").cast(pl.String).str.to_datetime(time_zone="UTC").alias("event_at"))
            .sort("event_at")
            .join_asof(specs.lazy().sort("captured_at"), left_on="event_at", right_on="captured_at",
                       by="symbol", strategy="backward", check_sortedness=False)
            .collect().to_dicts())


def inputs(evidence, specs, gaps):
    snapshot = {"trades_by_venue": {}, "trades_total": 0, "put_trades": 0, "call_trades": 0}
    turnover, missing_values = 0.0, 0
    blocks = []
    for venue in VENUES:
        rows = [r for r in evidence.get(f"option_trades_{venue}", []) if r["record_type"] == "trade"]
        snapshot["trades_by_venue"][venue] = len(rows)
        snapshot["trades_total"] += len(rows)
        snapshot["put_trades"] += sum(r["symbol"].endswith("-P") for r in rows)
        snapshot["call_trades"] += sum(r["symbol"].endswith("-C") for r in rows)
        converted = with_units(rows, specs[venue]) if rows and venue in specs else rows
        groups = {}
        for row in converted:
            amount = row.get("amount")
            coin = (amount if row.get("oi_unit") == "coin" else
                    amount * row["contract_size"] if amount is not None
                    and row.get("oi_unit") == "contracts" and row.get("contract_size") else None)
            premium = row.get("turnover_usd")
            if premium is None and coin is not None and row.get("price") is not None:
                if row.get("price_unit") == "quote_usd":
                    premium = coin * row["price"]
                elif row.get("price_unit") == "coin" and row.get("index_price"):
                    premium = coin * row["price"] * row["index_price"]
            if premium is None:
                missing_values += 1
            else:
                turnover += premium
            if not row.get("block_id"):
                continue
            bid = str(row["block_id"])
            group = groups.setdefault(bid, {"exchange": venue, "block_id": bid,
                "volume_coin": 0.0, "iv_sum": 0.0, "iv_count": 0, "leg_count": 0,
                "bucket_at": int(datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00")).timestamp() * 1000),
                "complete": True})
            group["complete"] &= coin is not None
            group["volume_coin"] += coin or 0
            group["leg_count"] += 1
            iv = row.get("iv")
            if iv is not None and row.get("iv_unit") in ("decimal", "vol_points"):
                group["iv_sum"] += iv * (100 if row["iv_unit"] == "decimal" else 1)
                group["iv_count"] += 1
        blocks.extend(g for g in groups.values() if g["complete"])
        if any(not g["complete"] for g in groups.values()):
            gaps.append(f"{venue}: block notional unavailable for groups lacking event-time unit metadata")
    snapshot.update(turnover_usd=turnover, turnover_complete=missing_values == 0)
    if missing_values:
        gaps.append(f"Volume: {missing_values} trades lack a provable USD premium; shown sum is the valued subset")
    dvol = evidence.get("dvol_window", [])
    if dvol:
        snapshot.update(dvol=dvol[0]["close"], dvol_open=dvol[0]["open"],
                        dvol_low=dvol[0]["low"], dvol_high=dvol[0]["high"])
    surface = evidence.get("option_surface_deribit", [])
    if surface and "deribit" in specs:
        surface = with_units(surface, specs["deribit"])
        for observation, key in (("window_open", "vs_open"), ("latest", "vs_now")):
            snapshot[key] = {r["symbol"]: {"mark_iv": r["markIV"] * (100 if r["iv_unit"] == "decimal" else 1),
                                           "delta": r["delta"]}
                             for r in surface if r["observation"] == observation
                             and r.get("markIV") is not None and r.get("iv_unit") in ("decimal", "vol_points")}
    return snapshot, blocks, turnover


def run(asset, window, start, end):
    recap.WARNINGS.clear()
    queries = build_queries(asset, start, end, render=True)
    end_ms, start_ms = int(end.timestamp() * 1000), int(start.timestamp() * 1000)
    gaps, specs, evidence = [], {}, {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        reads = [(q, pool.submit(run_query, q)) for q in queries]
        meta = {v: pool.submit(metadata, v, asset, start, end) for v in VENUES}
        tape = pool.submit(read_executions, start, end, asset=asset, now=end)
        closes = pool.submit(recap.fetch_7d_closes, asset, end_ms)
        # Same Deribit perpetual-price proxy and realized-vol definition as before.
        market = pool.submit(recap._fetch_market_fallback, asset, start_ms, end_ms, want_surface=False)
        for query, future in reads:
            source, rows = future.result()
            if query.name in ("dvol_window", "option_surface_deribit") and rows:
                latest = [r for r in rows if r.get("observation", "latest") == "latest"]
                times = [datetime.fromisoformat(str(r["max_event_at"]).replace("Z", "+00:00"))
                         for r in latest if r.get("max_event_at")]
                if not times or not timedelta(0) <= end - max(times) <= timedelta(minutes=45):
                    gaps.append(f"{query.name}: latest observation stale or freshness unverified; excluded")
                    rows = []
            evidence[query.name] = rows
            if source["status"] != "ok":
                gaps.append(f"{query.name}: unavailable — {source['error']}")
            elif source["path_plan"].get("missing_pattern_count"):
                gaps.append(f"{query.name}: {source['path_plan']['missing_pattern_count']}/{source['path_plan']['pattern_count']} hourly/bucket paths absent; partial coverage")
            elif not rows:
                gaps.append(f"{query.name}: no usable observations")
        for venue, future in meta.items():
            try:
                specs[venue] = future.result()
            except Exception as exc:
                gaps.append(f"{venue}: unit metadata unavailable — {exc}")
        try:
            executions = calculation_rows(tape.result()["rows"])
        except Exception as exc:
            executions = []
            gaps.append(f"Paradigm executions unavailable — {exc}")
        deri = {}
        for key, future in (("closes_7d", closes), ("market", market)):
            try:
                deri[key] = future.result()
            except Exception as exc:
                gaps.append(f"Deribit {key} unavailable — {exc}")
    snapshot, blocks, known_turnover = inputs(evidence, specs, gaps)
    if not snapshot["trades_total"] and not evidence.get("option_surface_deribit"):
        raise RuntimeError("recap: no usable core direct-data source; " + "; ".join(gaps))
    result = recap.build(asset, window, start_ms, end_ms, deri, snapshot, executions, blocks)
    # Direct inputs cover the requested window, not the retired 24h rollup.
    result["hot_horizon"] = None
    result["snapshot"]["volume_usd_m"] = round(known_turnover / 1e6, 2)
    result["snapshot"]["volume_scope"] = "observed valued trades; USD premium"
    result["snapshot"]["activity_scope"] = "observed trades; see coverage"
    result["source_gaps"] = gaps
    return recap.render_md(result)
