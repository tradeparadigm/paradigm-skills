"""Non-hot inputs for the existing recap calculator and renderer."""

import gc
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
import sys

import boto3
import polars as pl

import recap
from collect_recap import VENUES, build_queries, query_workers, run_query, set_budget

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "data-discovery" / "scripts"))
from execution_tape import S3_ENDPOINT, calculation_rows, read_executions


SPEC_COLUMNS = ["symbol", "captured_at", "iv_unit", "oi_unit",
                "contract_size", "price_unit"]


def metadata(venue, asset, start, end):
    """One predecessor snapshot plus snapshots within the requested window."""
    s3 = boto3.client("s3", region_name="ap-northeast-1", endpoint_url=S3_ENDPOINT)
    prefix = f"meta/instruments/exchange={venue}/currency={asset.lower()}/"
    objects = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket="dt-exchange-venue-data", Prefix=prefix):
        for item in page.get("Contents", []):
            key = item["Key"]
            # A marker, manifest or interrupted .tmp write is not a snapshot.
            # Letting it raise here would cost the venue its unit metadata, and
            # every one of its trades a provable USD premium.
            try:
                stamp = datetime.strptime(key.rsplit("__", 1)[1], "%Y%m%dT%H%M%SZ.parquet").replace(tzinfo=timezone.utc)
            except (ValueError, IndexError):
                continue
            if stamp <= end:
                objects.append((stamp, key))
    before = [item for item in objects if item[0] <= start]
    chosen = ([max(before)] if before else []) + [item for item in objects if start < item[0] <= end]
    if not chosen:
        raise ValueError(f"no event-applicable instrument metadata for {venue}")
    def snapshot(key):
        obj = s3.get_object(Bucket="dt-exchange-venue-data", Key=key)
        return pl.read_parquet(BytesIO(obj["Body"].read()), columns=SPEC_COLUMNS)

    # A 30-day window names ~700 snapshots per venue. Fetched one at a time they
    # cost minutes, and concatenated whole they are millions of rows of a chain
    # that barely changes — enough to take the container down.
    with ThreadPoolExecutor(max_workers=16) as pool:
        frames = list(pool.map(snapshot, [key for _, key in sorted(chosen)]))
    spec = SPEC_COLUMNS[2:]
    return (pl.concat(frames)
            .with_columns(pl.col("captured_at").str.to_datetime(time_zone="UTC"))
            .sort("symbol", "captured_at")
            # The as-of join only needs the instant a spec CHANGED, so drop the
            # repeats between changes. Consecutive, not distinct: a symbol that
            # goes A -> B -> A must keep both A rows or the second one resolves
            # back to B.
            .filter(pl.any_horizontal(
                [pl.col(column).ne_missing(pl.col(column).shift(1).over("symbol"))
                 for column in spec]).fill_null(True))
            .sort("captured_at"))


UNIT_COLUMNS = {"oi_unit": pl.String, "price_unit": pl.String,
                "iv_unit": pl.String, "contract_size": pl.Float64}


def event_at(dtype):
    """`timestamp` arrives as a string from some venues, a datetime from others."""
    column = pl.col("timestamp")
    if dtype == pl.String:
        return column.str.to_datetime(time_zone="UTC")
    if isinstance(dtype, pl.Datetime) and dtype.time_zone is None:
        return column.dt.replace_time_zone("UTC")
    return column.dt.convert_time_zone("UTC")


def with_units(frame, specs):
    """An as-of join never applies a future instrument spec to a past trade."""
    return (frame.lazy()
            .with_columns(event_at(frame.schema["timestamp"]).alias("event_at"))
            .sort("event_at")
            .join_asof(specs.lazy().sort("captured_at"), left_on="event_at", right_on="captured_at",
                       by="symbol", strategy="backward", check_sortedness=False)
            .collect())


def priced(frame):
    """Coin size and USD premium per trade, from event-applicable units.

    Expression-for-expression with the row loop this replaced, falsy checks
    included: a zero contract_size or index_price leaves the value unproven
    rather than quietly producing a zero.
    """
    amount, oi, size = pl.col("amount"), pl.col("oi_unit"), pl.col("contract_size")
    frame = frame.with_columns(
        pl.when(oi == "coin").then(amount)
          .when((oi == "contracts") & amount.is_not_null()
                & size.is_not_null() & (size != 0)).then(amount * size)
          .otherwise(None).alias("coin"))
    coin, price, index = pl.col("coin"), pl.col("price"), pl.col("index_price")
    unit = pl.col("price_unit")
    computable = coin.is_not_null() & price.is_not_null()
    return frame.with_columns(pl.coalesce(
        pl.col("turnover_usd"),
        pl.when(computable & (unit == "quote_usd")).then(coin * price),
        pl.when(computable & (unit == "coin") & index.is_not_null() & (index != 0))
          .then(coin * price * index),
    ).alias("premium"))


EMPTY_TOTAL = {"count": 0, "puts": 0, "calls": 0, "turnover": 0.0,
               "missing": 0, "blocks": []}


def aggregate_trades(venue, rows, spec, gaps):
    """Reduce one venue's trades to totals and blocks so the frame can be freed.

    Called as each read lands rather than after all of them: at 30d the five
    venue windows are ~1.9M rows between them, and holding them together while
    the joins run is what exhausted the container.
    """
    total = dict(EMPTY_TOTAL, count=rows.height, blocks=[])
    if not rows.height:
        return total
    symbol = pl.col("symbol")
    total["puts"] = rows.select(symbol.str.ends_with("-P").sum()).item()
    total["calls"] = rows.select(symbol.str.ends_with("-C").sum()).item()
    if spec is not None:
        converted = with_units(rows, spec)
    else:
        # No metadata for this venue: the unit columns stay null so nothing
        # converts, which is what the absent dict keys used to mean.
        converted = rows.with_columns(
            [pl.lit(None, dtype).alias(name) for name, dtype in UNIT_COLUMNS.items()]
        ).with_columns(event_at(rows.schema["timestamp"]).alias("event_at"))
    valued = priced(converted)
    total["missing"] = valued.select(pl.col("premium").is_null().sum()).item()
    total["turnover"] = valued["premium"].sum() or 0.0

    identifier = pl.col("block_id").cast(pl.String)
    identified = valued.filter(pl.col("block_id").is_not_null() & (identifier != ""))
    if identified.height:
        iv, iv_unit = pl.col("iv"), pl.col("iv_unit")
        usable = iv.is_not_null() & iv_unit.is_in(["decimal", "vol_points"])
        points = iv * pl.when(iv_unit == "decimal").then(100).otherwise(1)
        # maintain_order + first() reproduces the setdefault this replaced: the
        # block takes its bucket from the earliest leg, because with_units has
        # already sorted the rows by event time.
        grouped = (identified
                   .group_by(identifier.alias("block_id"), maintain_order=True)
                   .agg(volume_coin=pl.col("coin").fill_null(0).sum(),
                        iv_sum=points.filter(usable).sum(),
                        iv_count=iv.filter(usable).len(),
                        leg_count=pl.len(),
                        bucket_at=pl.col("event_at").dt.timestamp("ms").first(),
                        complete=pl.col("coin").is_not_null().all()))
        total["blocks"] = [{"exchange": venue, **group}
                           for group in grouped.filter(pl.col("complete")).to_dicts()]
        if grouped.select((~pl.col("complete")).any()).item():
            gaps.append(f"{venue}: block notional unavailable for groups lacking event-time unit metadata")
        del grouped, identified
    del converted, valued
    gc.collect()
    return total


def inputs(totals, evidence, specs, gaps):
    snapshot = {"trades_by_venue": {}, "trades_total": 0, "put_trades": 0, "call_trades": 0}
    turnover, missing_values = 0.0, 0
    blocks = []
    for venue in VENUES:
        total = totals.get(venue, EMPTY_TOTAL)
        snapshot["trades_by_venue"][venue] = total["count"]
        snapshot["trades_total"] += total["count"]
        snapshot["put_trades"] += total["puts"]
        snapshot["call_trades"] += total["calls"]
        turnover += total["turnover"]
        missing_values += total["missing"]
        blocks.extend(total["blocks"])
    snapshot.update(turnover_usd=turnover, turnover_complete=missing_values == 0)
    if missing_values:
        gaps.append(f"Volume: {missing_values} trades lack a provable USD premium; shown sum is the valued subset")
    dvol = evidence.get("dvol_window")
    if dvol is not None and dvol.height:
        first = dvol.row(0, named=True)
        snapshot.update(dvol=first["close"], dvol_open=first["open"],
                        dvol_low=first["low"], dvol_high=first["high"])
    surface = evidence.get("option_surface_deribit")
    if surface is not None and surface.height and "deribit" in specs:
        observed = with_units(surface, specs["deribit"]).to_dicts()
        for observation, key in (("window_open", "vs_open"), ("latest", "vs_now")):
            snapshot[key] = {r["symbol"]: {"mark_iv": r["markIV"] * (100 if r["iv_unit"] == "decimal" else 1),
                                           "delta": r["delta"]}
                             for r in observed if r["observation"] == observation
                             and r.get("markIV") is not None and r.get("iv_unit") in ("decimal", "vol_points")}
    return snapshot, blocks, turnover


def run(asset, window, start, end):
    recap.WARNINGS.clear()
    queries = build_queries(asset, start, end, render=True)
    end_ms, start_ms = int(end.timestamp() * 1000), int(start.timestamp() * 1000)
    # Gaps are collected per stage and concatenated in the original order at
    # the end: aggregating early would otherwise interleave a venue's block
    # warning with the read warnings, and the ⚠ lines are part of the output.
    read_gaps, meta_gaps, block_gaps = [], [], []
    gaps, specs, evidence, totals = [], {}, {}, {}
    # Partition reads get their own pool: they are the ones holding a window in
    # memory, so their concurrency is a memory budget, not a latency choice.
    readers = query_workers(end - start)
    set_budget(readers)
    with ThreadPoolExecutor(max_workers=readers) as reader_pool, \
            ThreadPoolExecutor(max_workers=8) as pool:
        reads = [(q, reader_pool.submit(run_query, q)) for q in queries]
        meta = {v: pool.submit(metadata, v, asset, start, end) for v in VENUES}
        # Publication age is a WALL-CLOCK question, so the reader's `now` must
        # not be the window end: collect_recap accepts --now for replays, and
        # passing that historical instant here made every replayed read fail
        # the publication gate as "future-dated" (published > now).
        tape = pool.submit(read_executions, start, end, asset=asset)
        closes = pool.submit(recap.fetch_7d_closes, asset, end_ms)
        # Same Deribit perpetual-price proxy and realized-vol definition as before.
        market = pool.submit(recap._fetch_market_fallback, asset, start_ms, end_ms, want_surface=False)
        def spec_for(venue):
            """Resolve one venue's metadata once, when its trades need it."""
            if venue not in specs and venue in meta:
                try:
                    specs[venue] = meta.pop(venue).result()
                except Exception as exc:
                    meta.pop(venue, None)
                    meta_gaps.append(f"{venue}: unit metadata unavailable — {exc}")
            return specs.get(venue)

        for query, future in reads:
            source, rows = future.result()
            if query.name in ("dvol_window", "option_surface_deribit") and rows.height:
                # Both are small — one row and a snapshot — so reading them back
                # as dicts here costs nothing.
                observed = rows.to_dicts()
                latest = [r for r in observed if r.get("observation", "latest") == "latest"]
                times = [datetime.fromisoformat(str(r["max_event_at"]).replace("Z", "+00:00"))
                         for r in latest if r.get("max_event_at")]
                if not times or not timedelta(0) <= end - max(times) <= timedelta(minutes=45):
                    read_gaps.append(f"{query.name}: latest observation stale or freshness unverified; excluded")
                    rows = rows.clear()
            if source["status"] != "ok":
                read_gaps.append(f"{query.name}: unavailable — {source['error']}")
            elif source["path_plan"].get("missing_pattern_count"):
                read_gaps.append(f"{query.name}: {source['path_plan']['missing_pattern_count']}/{source['path_plan']['pattern_count']} hourly/bucket paths absent; partial coverage")
            elif not rows.height:
                read_gaps.append(f"{query.name}: no usable observations")
            if query.name.startswith("option_trades_"):
                # Reduced here and dropped, so the next venue's read never sits
                # beside this one's window.
                venue = query.name[len("option_trades_"):]
                trades = rows.filter(pl.col("record_type") == "trade") if rows.height else rows
                totals[venue] = aggregate_trades(venue, trades, spec_for(venue), block_gaps)
                del rows, trades
                gc.collect()
            else:
                evidence[query.name] = rows
        for venue in list(meta):
            spec_for(venue)
        try:
            tape_result = tape.result()
            executions = calculation_rows(tape_result["rows"])
            # An uncovered tail is missing evidence, not a quiet tape.
            if not tape_result.get("coverage_complete", False):
                gaps.append(
                    "Paradigm executions: "
                    + tape_result.get("coverage_note", "coverage incomplete")
                )
        except Exception as exc:
            executions = []
            gaps.append(f"Paradigm executions unavailable — {exc}")
        deri = {}
        for key, future in (("closes_7d", closes), ("market", market)):
            try:
                deri[key] = future.result()
            except Exception as exc:
                gaps.append(f"Deribit {key} unavailable — {exc}")
    gaps = read_gaps + meta_gaps + gaps + block_gaps
    snapshot, blocks, known_turnover = inputs(totals, evidence, specs, gaps)
    surface_rows = evidence.get("option_surface_deribit")
    if not snapshot["trades_total"] and not (surface_rows is not None and surface_rows.height):
        raise RuntimeError("recap: no usable core direct-data source; " + "; ".join(gaps))
    result = recap.build(asset, window, start_ms, end_ms, deri, snapshot, executions, blocks)
    # Direct inputs cover the requested window, not the retired 24h rollup.
    result["hot_horizon"] = None
    result["snapshot"]["volume_usd_m"] = round(known_turnover / 1e6, 2)
    # `·` like every other Snapshot separator. The `;` form was relayed as
    # `;;` by the model twice in a row on 2026-09-08; the script never emitted
    # that, but a separator the relay cannot double removes the question.
    result["snapshot"]["volume_scope"] = "observed valued trades · USD premium"
    result["snapshot"]["activity_scope"] = "observed trades · see coverage"
    result["source_gaps"] = gaps
    return recap.render_md(result)
