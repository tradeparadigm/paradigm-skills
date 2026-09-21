"""Direct input adapters preserve the established numerical/output contract."""

from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
import re
import sys
import types

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import direct_inputs as direct
import recap
import collect_recap as collector
from collect_recap import build_queries

UTC = timezone.utc
START = datetime(2026, 9, 8, 8, tzinfo=UTC)
END = datetime(2026, 9, 8, 9, tzinfo=UTC)


def spec():
    return pl.DataFrame({"symbol": ["BTC-11SEP26-70000-P"] * 2,
                         "captured_at": [START, END], "iv_unit": ["decimal"] * 2,
                         "oi_unit": ["contracts"] * 2, "contract_size": [0.01, 0.1],
                         "price_unit": ["coin"] * 2})


TRADE_SCHEMA = {"record_type": pl.String, "exchange": pl.String,
                "timestamp": pl.String, "symbol": pl.String, "amount": pl.Float64,
                "price": pl.Float64, "index_price": pl.Float64,
                "turnover_usd": pl.Float64, "block_id": pl.String, "iv": pl.Float64}


def trade(**updates):
    return {"record_type": "trade", "exchange": "okex-options",
            "timestamp": "2026-09-08T08:30:00Z", "symbol": "BTC-11SEP26-70000-P",
            "amount": 100.0, "price": 0.01, "index_price": 80000.0,
            "turnover_usd": None, "block_id": "block-1", "iv": 0.4, **updates}


def trades(*rows):
    """inputs() reads frames now — a dict per row is what blew the memory budget."""
    return pl.DataFrame(list(rows), schema=TRADE_SCHEMA)


def reduce(frame, spec, gaps, venue="okex-options"):
    """The streaming path: reduce one venue, then assemble from the totals."""
    totals = {venue: direct.aggregate_trades(venue, frame, spec, gaps)}
    return direct.inputs(totals, {}, {venue: spec} if spec is not None else {}, gaps)


def test_event_time_units_not_latest_metadata():
    gaps = []
    snapshot, blocks, turnover = reduce(trades(trade()), spec(), gaps)
    assert turnover == 800.0  # 100 contracts * 0.01 BTC * 0.01 premium * 80k
    assert blocks[0]["volume_coin"] == 1.0
    assert blocks[0]["iv_sum"] == 40.0
    assert snapshot["put_trades"] == 1
    assert not gaps


def test_unavailable_metadata_does_not_default_contract_size():
    gaps = []
    snapshot, blocks, turnover = reduce(trades(trade()), None, gaps)
    assert not snapshot["turnover_complete"]
    assert not blocks and turnover == 0
    assert any("lack a provable USD premium" in gap for gap in gaps)


def test_existing_usd_turnover_is_not_scaled_twice():
    _, _, turnover = reduce(trades(trade(turnover_usd=123.0)), spec(), [])
    assert turnover == 123.0


def test_render_path_reads_complete_not_sampled_inputs():
    queries = build_queries("BTC", START, END, render=True)
    assert len(queries) == 7
    trades = next(q for q in queries if q.name == "option_trades_deribit")
    surface = next(q for q in queries if q.name == "option_surface_deribit")
    assert "LIMIT 25" not in trades.sql
    assert "WHERE evidence_rank=1" not in surface.sql
    assert all("/hot/" not in p and "hot__" not in p for q in queries for p in q.paths)


def test_same_renderer_reconciles_vrp_and_put_call_direction(monkeypatch):
    monkeypatch.setattr(recap, "realized_vs_implied", lambda *_: {"value": 33.3, "vrp": 6.1})
    result = recap.build("BTC", "1h", int(START.timestamp()*1000), int(END.timestamp()*1000), {},
                         {"dvol": 39.4, "put_trades": 3, "call_trades": 4})
    text = recap.render_md(result)
    assert "+6.1v" in text and "RICH" in text
    assert "put-heavy" not in text
    assert [text.index(s) for s in ("**Snapshot**", "**Biggest Print**", "**Block Flow", "**Vol Surface**")] == sorted(
        text.index(s) for s in ("**Snapshot**", "**Biggest Print**", "**Block Flow", "**Vol Surface**"))


def test_leg_adapter_uses_typed_geometry_not_package_description():
    from execution_tape import calculation_rows
    row = {"traded_at": int(START.timestamp()*1000), "description": "incorrect package shorthand",
           "instrument_kind": "OPTION", "expiry_date": "2026-09-11", "option_kind": "PUT",
           "strike_price": 70000.0, "rfq_id": "r_test", "block_trade_id": "bt_test",
           "venue_block_trade_id": "block-1", "product": "BTC OPTION - DBT", "asset": "BTC",
           "quantity": 40, "trade_price": 0.01, "mark_price": 0.02, "taker_side": "BUY",
           "notional_volume_usd": 3200000, "trade_id": "t_test"}
    mapped = calculation_rows([row])[0]
    assert mapped["DESCRIPTION"] == "Put 11 Sep 26 70000"
    assert mapped["SIDE"] == "BUY" and mapped["QTY"] == 40
    # read_executions guarantees trade_id is present, non-null and unique, and
    # tape_block_key falls back to it when a leg carries no block id.
    assert mapped["TRADE_ID"] == "t_test"


def instrument_object(captured_at, contract_size=0.1):
    """captured_at rides as a String — metadata() parses it with the .str namespace."""
    frame = pl.DataFrame({"symbol": ["BTC-11SEP26-70000-P"], "captured_at": [captured_at],
                          "iv_unit": ["decimal"], "oi_unit": ["contracts"],
                          "contract_size": [contract_size], "price_unit": ["coin"]})
    buffer = BytesIO()
    frame.write_parquet(buffer)
    return {"Body": BytesIO(buffer.getvalue())}


def test_metadata_keeps_every_spec_change_not_every_snapshot(monkeypatch):
    """Repeat snapshots collapse, but a spec that reverts keeps both of its rows.

    Deduplicating on distinct specs rather than consecutive ones would drop the
    second A of an A -> B -> A history, and every trade after it would then
    resolve back to B."""
    base = "meta/instruments/exchange=deribit/currency=btc/"
    sizes = {1: 0.1, 2: 0.1, 3: 0.5, 4: 0.5, 5: 0.1}
    snapshots = {f"{base}instruments__deribit__btc__2026090{day}T000000Z.parquet":
                 (f"2026-09-0{day}T00:00:00Z", size) for day, size in sizes.items()}

    class Paginator:
        def paginate(self, **_):
            return [{"Contents": [{"Key": key} for key in snapshots]}]

    class Client:
        def get_paginator(self, _):
            return Paginator()

        def get_object(self, Bucket, Key):
            captured_at, size = snapshots[Key]
            return instrument_object(captured_at, size)

    monkeypatch.setattr(direct, "boto3",
                        type("Stub", (), {"client": staticmethod(lambda *a, **k: Client())}))
    specs = direct.metadata("deribit", "BTC",
                            datetime(2026, 8, 31, tzinfo=timezone.utc),
                            datetime(2026, 9, 6, tzinfo=timezone.utc))
    assert specs["contract_size"].to_list() == [0.1, 0.5, 0.1]


def test_one_stray_object_does_not_cost_a_venue_its_units(monkeypatch):
    """A marker or interrupted write under the metadata prefix used to raise,
    which the caller turns into 'unit metadata unavailable' for the whole venue
    — so every trade on it loses its provable USD premium."""
    base = "meta/instruments/exchange=deribit/currency=btc/"
    snapshots = {
        base + "instruments__deribit__btc__20260908T070000Z.parquet": "2026-09-08T07:00:00Z",
        base + "instruments__deribit__btc__20260908T083000Z.parquet": "2026-09-08T08:30:00Z",
    }
    keys = [*snapshots, base + "_SUCCESS", base + "instruments__deribit__btc__partial.tmp"]

    class Paginator:
        def paginate(self, **_):
            return [{"Contents": [{"Key": key} for key in keys]}]

    class Client:
        def get_paginator(self, _):
            return Paginator()

        def get_object(self, Bucket, Key):
            return instrument_object(snapshots[Key])

    # Patch the module under test, not the real boto3 every other module shares.
    monkeypatch.setattr(direct, "boto3", type("Stub", (), {"client": staticmethod(lambda *a, **k: Client())}))
    specs = direct.metadata("deribit", "BTC", START, END)
    # Both snapshots carry the same spec, so one row covers the window; what
    # matters is that the stray objects did not take the venue's units with them.
    assert specs["symbol"].to_list() == ["BTC-11SEP26-70000-P"]
    assert specs["contract_size"].to_list() == [0.1]
    assert specs["captured_at"].dt.strftime("%Y-%m-%dT%H:%M:%SZ").to_list() == [
        "2026-09-08T07:00:00Z"]


def fake_connection(glob_files):
    """A duckdb.connect stand-in whose glob() returns exactly `glob_files`.

    Lives in this lane rather than the stdlib one because run_query now returns
    a polars frame, so the double has to produce one.
    """

    class Connection:
        description = [("max_event_at",), ("price",)]

        def execute(self, sql):
            self._glob = sql.lstrip().startswith("SELECT file FROM glob(")
            return self

        def fetchall(self):
            return [(name,) for name in glob_files] if self._glob else [("x", 1)]

        def pl(self):
            return pl.DataFrame({"max_event_at": ["2026-08-30T12:00:00Z"], "price": [1]})

        def cursor(self):
            return Connection()

        def close(self):
            pass

    return Connection


def run_query_with(glob_files, query):
    """Drive run_query against a fake DuckDB, with no network for either reader.

    A streaming query would otherwise fetch from S3, so s3_async is stubbed; the
    fake connection supplies the result frame in both cases.
    """
    original = collector.duckdb.connect
    previous = sys.modules.get("s3_async")
    collector.duckdb.connect = fake_connection(glob_files)
    sys.modules["s3_async"] = types.SimpleNamespace(
        read_objects=lambda paths, columns=None: pl.DataFrame())
    try:
        return collector.run_query(query)
    finally:
        collector.duckdb.connect = original
        if previous is None:
            sys.modules.pop("s3_async", None)
        else:
            sys.modules["s3_async"] = previous


def test_evidence_contract_names_provenance_and_freshness():
    query = collector.Query("x", ["s3://direct"], "SELECT 1", {"price": "USD"}, True)
    metadata, rows = run_query_with(["s3://direct/a.parquet"], query)
    assert metadata["path_plan"] == {
        "pattern_count": 1, "first_pattern": "s3://direct", "last_pattern": "s3://direct",
        "resolved_file_count": 1, "missing_pattern_count": 0}
    assert metadata["units"] == {"price": "USD"}
    assert metadata["max_event_at"] == "2026-08-30T12:00:00Z"
    assert rows["price"][0] == 1


def test_absent_partition_does_not_erase_the_rest_of_the_window():
    """One unwritten hour must not take the whole window's evidence down."""
    patterns = [f"s3://bucket/hour={hour:02d}/**/*.parquet" for hour in (17, 18, 19)]
    query = collector.Query("trades", patterns, "SELECT * FROM read_parquet(__PATHS__)", {}, True)
    # Hours 17 and 18 landed; the current hour 19 has not been written yet.
    metadata, rows = run_query_with(
        ["s3://bucket/hour=17/a.parquet", "s3://bucket/hour=18/b.parquet"], query)
    assert metadata["status"] == "ok" and rows.height
    assert metadata["path_plan"]["resolved_file_count"] == 2
    assert metadata["path_plan"]["missing_pattern_count"] == 1
    assert metadata["path_plan"]["missing_patterns"] == [patterns[2]]


def test_missing_hours_are_read_off_the_returned_keys():
    """Day-level globs cannot probe per hour, so an absent hour is found by its
    absence from the resolved file names — same warning, 1/24 the listings."""
    start = datetime(2026, 8, 30, 10, tzinfo=UTC)
    end = datetime(2026, 8, 30, 13, tzinfo=UTC)
    trades = next(q for q in collector.build_queries("BTC", start, end)
                  if q.name == "option_trades_deribit")
    assert trades.expected_hours == ("20260830T10", "20260830T11", "20260830T12")
    # Hours 10 and 12 landed; hour 11 never wrote a file.
    metadata, _ = run_query_with([
        "s3://b/year=2026/month=08/day=30/hour=10/x__rows__20260830T100000Z.parquet",
        "s3://b/year=2026/month=08/day=30/hour=12/x__rows__20260830T121500Z.parquet"], trades)
    assert metadata["path_plan"]["missing_pattern_count"] == 1
    assert metadata["path_plan"]["missing_patterns"] == ["20260830T11"]
    assert metadata["path_plan"]["pattern_count"] == 3


def test_fully_absent_window_reports_unavailable_not_a_quiet_market():
    patterns = ["s3://bucket/hour=17/**/*.parquet", "s3://bucket/hour=18/**/*.parquet"]
    query = collector.Query("trades", patterns, "SELECT * FROM read_parquet(__PATHS__)", {}, True)
    metadata, rows = run_query_with([], query)
    assert metadata["status"] == "unavailable"
    assert rows.height == 0 and metadata["row_count"] == 0
    assert "2 partition patterns" in metadata["error"]


def test_a_streaming_failure_is_a_gap_not_a_traceback(monkeypatch):
    """The async reader raises obstore/pyarrow/botocore errors, none of them
    duckdb.Error. Uncaught they killed the whole recap — every trade query and
    dvol stream, so it is the common path — instead of naming one bad source."""
    query = collector.Query("option_trades_deribit", ["s3://b/**/*.parquet"],
                            "SELECT * FROM read_parquet(__PATHS__)", {}, True,
                            stream=True, columns=("timestamp",))
    original = collector.duckdb.connect
    previous = sys.modules.get("s3_async")
    collector.duckdb.connect = fake_connection(["s3://b/a.parquet"])

    def boom(paths, columns=None):
        raise pl.exceptions.ComputeError("schema drift across objects")

    sys.modules["s3_async"] = types.SimpleNamespace(read_objects=boom)
    try:
        metadata, rows = collector.run_query(query)
    finally:
        collector.duckdb.connect = original
        if previous is None:
            sys.modules.pop("s3_async", None)
        else:
            sys.modules["s3_async"] = previous
    assert metadata["status"] == "unavailable"
    assert "schema drift" in metadata["error"]
    assert rows.height == 0


def test_schema_drift_across_objects_falls_back_to_string():
    """union_by_name=true reads a column that is int in one object and string in
    another as VARCHAR; permissive Arrow promotion raises instead."""
    import pyarrow as pa
    from importlib import util as _util
    spec = _util.spec_from_file_location(
        "s3_async", Path(__file__).resolve().parents[2] / "data-discovery" / "scripts" / "s3_async.py")
    mod = _util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    merged = mod._concat([pa.table({"id": pa.array([1, 2], pa.int64())}),
                          pa.table({"id": pa.array(["x"], pa.string())})])
    assert merged.num_rows == 3
    assert merged.schema.field("id").type == pa.string()
    assert merged.column("id").to_pylist() == ["1", "2", "x"]


HOURS = ("20260916T09", "20260916T10", "20260916T11", "20260916T12")
COV_END = datetime(2026, 9, 16, 12, 30, tzinfo=UTC)
COV_START = COV_END - timedelta(hours=3)


def _verdict(monkeypatch, present, missing_trade_hours, now=COV_END):
    monkeypatch.setattr(direct, "hours_present", lambda *a, **k: (set(present), HOURS))
    monkeypatch.setattr(direct, "connect", lambda: types.SimpleNamespace(close=lambda: None))
    return direct.coverage_verdict("deribit", "BTC", COV_START, COV_END, missing_trade_hours, now=now)


def test_a_trade_hour_the_companion_feed_kept_is_a_quiet_market(monkeypatch):
    """THE headline claim: missing from both feeds is lost data, missing only
    from the intermittent trade tape is a quiet market. Neither earlier fixture
    reached the intersection — in one the missing hour was the in-progress one,
    in the other it was companion-missing too, so `lost` was identical with the
    companion check removed."""
    state, detail = _verdict(monkeypatch, HOURS, ["20260916T10"])
    assert state == "quiet", (state, detail)
    assert detail["quiet_hours"] == ["20260916T10"]


def test_the_hour_still_being_written_is_not_a_feed_gap(monkeypatch):
    """A live window ends inside the current hour, which no producer has
    finished writing. Counting it made all five venues report a feed gap."""
    state, _ = _verdict(monkeypatch, HOURS[:3], ["20260916T12"])
    assert state == "complete"


def test_a_replay_window_does_not_excuse_its_final_hour(monkeypatch):
    """The in-progress exclusion is about NOW, not about `end`. Deriving it from
    `end` alone reported a genuinely dead final hour as fully covered."""
    later = COV_END + timedelta(hours=6)
    state, detail = _verdict(monkeypatch, HOURS[:3], ["20260916T12"], now=later)
    assert state == "feed_gap", (state, detail)
    assert detail["lost_hours"] == ["20260916T12"]


def test_a_genuinely_missing_mid_window_hour_still_reports(monkeypatch):
    state, detail = _verdict(monkeypatch, {"20260916T09", "20260916T11"},
                             ["20260916T10", "20260916T12"])
    assert state == "feed_gap"
    assert detail["lost_hours"] == ["20260916T10"]


def test_a_companion_only_gap_is_not_a_feed_gap(monkeypatch):
    """The trade tape covered these hours; only the quote feed lost them. Calling
    that `feed_gap` made the caller say the trades below were understated, which
    is false for exactly the hours that triggered it."""
    state, detail = _verdict(monkeypatch, {"20260916T09", "20260916T11"}, [])
    assert state == "companion_gap", (state, detail)
    assert detail["lost_hours"] == ["20260916T10"]


def test_an_empty_companion_listing_is_unknown_not_a_total_outage(monkeypatch):
    """An empty LIST is indistinguishable from a wrong prefix, so it cannot be
    read as every hour lost on a venue whose trade tape is 100% complete."""
    state, _ = _verdict(monkeypatch, set(), [])
    assert state == "unknown"


def test_a_failed_coverage_read_does_not_abort_the_recap(monkeypatch):
    """Advisory: the trade rows are already in memory, so a 403 on the companion
    LIST must cost the coverage note and nothing else."""
    def boom():
        raise RuntimeError("AccessDenied on LIST")
    monkeypatch.setattr(direct, "connect", boom)
    state, detail = direct.coverage_verdict("deribit", "BTC", COV_START, COV_END, [])
    assert state == "unknown"
    assert "AccessDenied" in detail["error"]


def _run_once(monkeypatch, *, coverage=("feed_gap", {"lost_hours": ["20260916T10"],
                                                    "expected": 3, "quiet_hours": []}),
              missing_hours=("20260916T10",), status="ok",
              query_name="option_trades_deribit", end=None, pattern_count=4,
              coverage_patch=True, now=None):
    """Drive `run()` through ONE real venue read, so the loop body executes.

    Stubbing build_queries to [] emptied the loop, which left coverage_verdict,
    both read-gap branches and every Block Flow line unexecuted while the test
    that named them still passed.
    """
    calls = {}
    query = collector.Query(name=query_name, paths=["s3://x"], sql="SELECT 1",
                            units={}, stream=True, columns=("timestamp",))
    rows = pl.DataFrame({"record_type": ["trade"], "timestamp": ["2026-09-16T10:00:00Z"]})
    source = {"status": status, "error": "boom",
              "path_plan": {"missing_hours": list(missing_hours),
                            "pattern_count": pattern_count,
                            "missing_pattern_count": len(missing_hours)}}

    class _Future:
        def result(self):
            return source, rows

    def fake_build(asset, window, start_ms, end_ms, deri, snapshot, executions,
                   blocks, **kwargs):
        calls["tape_available"] = kwargs.get("tape_available")
        calls["venue_coverage"] = snapshot.get("venue_coverage")
        return {"snapshot": snapshot, "source_gaps": [], "hot_horizon": None}

    monkeypatch.setattr(recap, "build", fake_build)
    monkeypatch.setattr(recap, "render_md", lambda result: result)
    monkeypatch.setattr(direct, "build_queries", lambda *a, **k: [query])
    monkeypatch.setattr(direct, "run_query", lambda *a, **k: (source, rows))
    monkeypatch.setattr(direct, "metadata", lambda *a, **k: pl.DataFrame())
    if coverage_patch:
        monkeypatch.setattr(direct, "coverage_verdict", lambda *a, **k: coverage)
    monkeypatch.setattr(direct, "aggregate_trades", lambda *a, **k: dict(direct.EMPTY_TOTAL))
    monkeypatch.setattr(direct, "read_executions",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no partition")))
    monkeypatch.setattr(recap, "fetch_7d_closes", lambda *a, **k: [])
    monkeypatch.setattr(recap, "_fetch_market_fallback", lambda *a, **k: None)
    captured = {}

    def fake_inputs(totals, evidence, specs, gaps, coverage=None):
        captured["gaps"] = list(gaps)
        captured["coverage"] = coverage
        return {"trades_total": 1, "venue_coverage": coverage}, [], 0.0

    monkeypatch.setattr(direct, "inputs", fake_inputs)
    window_end = end or COV_END
    # Pin the clock to the window these fixtures describe. Letting run() read the
    # wall clock made every live-window assertion pass on the day it was written
    # and fail on every day after it.
    result = direct.run("BTC", "24h", window_end - timedelta(hours=3), window_end,
                        now=now or window_end)
    return calls, captured, result


def test_run_reaches_the_read_loop_and_wires_coverage_through(monkeypatch):
    calls, captured, _ = _run_once(monkeypatch)
    assert captured["coverage"]["deribit"][0] == "feed_gap", captured
    assert calls["venue_coverage"] == captured["coverage"]
    # read_executions raised, so the tape is genuinely absent — the signal that
    # stops _dedupe_venue_blocks deleting every brokered block.
    assert calls["tape_available"] is False, calls


def test_a_feed_gap_says_the_figures_below_are_understated(monkeypatch):
    _, captured, _ = _run_once(monkeypatch)
    gap = [g for g in captured["gaps"] if g.startswith("Deribit:")]
    assert gap and "understated" in gap[0], captured["gaps"]


def test_a_companion_gap_does_not_claim_the_trades_are_understated(monkeypatch):
    _, captured, _ = _run_once(
        monkeypatch, coverage=("companion_gap", {"lost_hours": ["20260916T10"],
                                                 "expected": 3, "quiet_hours": []}))
    gap = [g for g in captured["gaps"] if g.startswith("Deribit:")]
    assert gap, captured["gaps"]
    assert "quote feed" in gap[0] and "understated" not in gap[0], gap


def test_an_unverifiable_venue_still_says_so(monkeypatch):
    """Returning `unknown` for an empty listing closed a false alarm and opened a
    silence: a genuinely dead companion feed produced no line at all."""
    _, captured, _ = _run_once(monkeypatch, coverage=("unknown", {}))
    gap = [g for g in captured["gaps"] if g.startswith("Deribit:")]
    assert gap and "could not be verified" in gap[0], captured["gaps"]


def test_a_block_missing_one_legs_index_is_not_half_priced():
    """polars skips nulls in the numerator; dividing by the full coin sum then
    halved a block whose legs were mixed. Weight only over legs that have one."""
    rows = trades(
        trade(block_id="b1", amount=100.0, index_price=80000.0, turnover_usd=1.0),
        trade(block_id="b1", amount=100.0, index_price=None, turnover_usd=1.0))
    _, blocks, _ = reduce(rows, spec(), [])
    assert len(blocks) == 1
    # Both legs are 1 coin after the contract-size conversion, so the weighted
    # index over the priced leg alone is 80000 — not 40000.
    assert abs(blocks[0]["index_px"] - 80000.0) < 1e-6


def test_a_symbol_winning_both_delta_targets_is_not_a_dropped_strike():
    """The surface query ranks per (observation, expiry, type, target_delta) for
    targets 0.25 and 0.50, so one symbol can appear twice. Counting rows against
    a symbol-keyed dict reported the duplicate as a strike dropped for want of
    units — a false gap, in the phase whose point is not raising false gaps."""
    row = {"observation": "latest", "symbol": "BTC-11SEP26-70000-P",
           "timestamp": "2026-09-08T08:30:00Z", "markIV": 0.5, "delta": 0.5}
    surface = pl.DataFrame([row, dict(row)], schema_overrides={"markIV": pl.Float64})
    gaps = []
    direct.inputs({}, {"option_surface_deribit": surface},
                  {"deribit": spec()}, gaps)
    assert not any("dropped for want of IV units" in g for g in gaps), gaps


def test_every_venue_symbol_format_is_classified_put_or_call():
    """Bybit suffixes its settlement currency, so `ends_with("-P")` matched none
    of its 571k trades and P/C was silently computed from 28% of the tape."""
    formats = {"BTC-25SEP26-150000-P": "P", "BTC_USDC-25DEC26-86000-P": "P",
               "BTC-USD-260904-78000-C": "C", "BTC-26MAR27-130000-P-USDT": "P",
               "BTC-USDC-20261127-80000-P": "P", "BTC-9SEP26-82500-C-USDT": "C"}
    frame = trades(*(trade(symbol=s) for s in formats))
    total = direct.aggregate_trades("bybit-options", frame, None, [])
    assert total["puts"] == sum(1 for v in formats.values() if v == "P")
    assert total["calls"] == sum(1 for v in formats.values() if v == "C")
    assert total["unclassified"] == 0


def test_a_symbol_with_no_option_type_is_excluded_and_declared():
    gaps = []
    frame = trades(trade(symbol="BTC-PERPETUAL"), trade(symbol="BTC-25SEP26-150000-C"))
    totals = {"deribit": direct.aggregate_trades("deribit", frame, None, gaps)}
    direct.inputs(totals, {}, {}, gaps)
    assert totals["deribit"]["unclassified"] == 1
    assert any("no recognisable option type" in g and "deribit" in g for g in gaps)


def test_unvalued_trades_name_the_venue_whose_metadata_is_short():
    """A bare total reads as diffuse noise; the real 14d case is one venue's
    instrument snapshots not covering the symbols its own tape traded."""
    gaps = []
    frame = trades(trade(symbol="BTC-26MAR27-130000-P-USDT", turnover_usd=None),
                   trade(symbol="BTC-26MAR27-140000-C-USDT", turnover_usd=None))
    totals = {"bybit-options": direct.aggregate_trades("bybit-options", frame, None, gaps)}
    direct.inputs(totals, {}, {}, gaps)
    volume = [g for g in gaps if g.startswith("Volume:")]
    assert volume, gaps
    assert "Bybit 2 of 2 (100%) across 2 symbols" in volume[0]


def test_an_hour_aligned_window_does_not_discount_a_bucket_it_never_had():
    """hour_patterns steps `while cursor < end`, so an hour-aligned end never
    includes its own hour. Discounting it anyway printed "24 of 23" for a window
    where 24 hours really were expected."""
    end = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    _, aligned = collector.hour_patterns("normalized", "deribit", "option_summary",
                                         "btc", end - timedelta(hours=24), end)
    assert len(aligned) == 24 and f"{end:%Y%m%dT%H}" not in aligned
    mid = end.replace(minute=30)
    _, unaligned = collector.hour_patterns("normalized", "deribit", "option_summary",
                                           "btc", mid - timedelta(hours=24), mid)
    assert f"{mid:%Y%m%dT%H}" in unaligned, unaligned[-1]


def test_the_read_gap_denominator_matches_the_plan(monkeypatch):
    """The `N of M` on a partial-coverage line: M must be the hours the plan
    actually asked for."""
    for end, expected in ((datetime(2026, 9, 18, 12, 0, tzinfo=UTC), "of 24"),
                          (datetime(2026, 9, 18, 12, 30, tzinfo=UTC), "of 24")):
        _, captured, _ = _run_once(
            monkeypatch, query_name="venue_blocks_deribit", end=end,
            missing_hours=("20260918T03",), pattern_count=(24 if end.minute == 0 else 25))
        line = [g for g in captured["gaps"] if g.startswith("venue_blocks_deribit:")]
        assert line and expected in line[0], (end.isoformat(), captured["gaps"])


def test_the_live_boundary_is_exclusive_at_exactly_one_hour():
    """The threshold itself, which only became testable once `now` was
    injectable: at exactly LIVE_WINDOW the window is no longer live, so its
    final hour is due. `<` vs `<=` is invisible to every other fixture."""
    end = datetime(2026, 9, 18, 12, 30, tzinfo=UTC)
    assert direct.is_live(end + direct.LIVE_WINDOW - timedelta(seconds=1), end)
    assert not direct.is_live(end + direct.LIVE_WINDOW, end)


def test_both_clocks_read_one_definition_of_live(monkeypatch):
    """coverage_verdict and the read-gap denominator each had their own copy of
    `(now - end) < 1h`. Threading one clock through does not stop two thresholds
    drifting, so they now call the same predicate — pinned by flipping it."""
    end = datetime(2026, 9, 18, 12, 30, tzinfo=UTC)
    monkeypatch.setattr(direct, "is_live", lambda now, end: False)
    _, captured, _ = _run_once(
        monkeypatch, query_name="venue_blocks_deribit", end=end,
        missing_hours=("20260918T03",), pattern_count=25, now=end)
    line = [g for g in captured["gaps"] if g.startswith("venue_blocks_deribit:")]
    assert line and "of 25" in line[0], captured["gaps"]
    state, _ = _verdict(monkeypatch, HOURS[:3], ["20260916T12"])
    assert state == "feed_gap", "coverage_verdict must read the same predicate"


def test_a_replayed_window_counts_its_final_hour_in_the_denominator(monkeypatch):
    """The other direction of the same rule, pinned explicitly so it cannot pass
    by accident of today's date: when the window is NOT live, its final hour is
    genuinely due and must stay in the `N of M`."""
    end = datetime(2026, 9, 18, 12, 30, tzinfo=UTC)
    _, captured, _ = _run_once(
        monkeypatch, query_name="venue_blocks_deribit", end=end,
        missing_hours=("20260918T03",), pattern_count=25, now=end + timedelta(days=2))
    line = [g for g in captured["gaps"] if g.startswith("venue_blocks_deribit:")]
    assert line and "of 25" in line[0], captured["gaps"]


def test_the_run_passes_one_clock_to_the_coverage_check(monkeypatch):
    """Two clocks microseconds apart can straddle the hour boundary and disagree
    about whether the final bucket is still open."""
    seen = {}

    def spy(venue, asset, start, end, missing, now=None):
        seen["now"] = now
        return "complete", {"quiet_hours": [], "expected": 3}

    monkeypatch.setattr(direct, "coverage_verdict", spy)
    _run_once(monkeypatch, coverage_patch=False)
    assert seen["now"] is not None, "run() must hand coverage_verdict its own clock"


def test_the_venue_index_comes_from_the_busiest_venue(monkeypatch):
    """Last resort spot. A one-trade venue's index is a worse estimate than the
    venue carrying most of the tape, and picking by VENUES order would take it."""
    totals = {
        "deribit": dict(direct.EMPTY_TOTAL, count=5, index_close=50_000.0),
        "bybit-options": dict(direct.EMPTY_TOTAL, count=900, index_close=100_000.0),
    }
    snapshot, _, _ = direct.inputs(totals, {}, {}, [])
    assert snapshot["venue_index_close"] == 100_000.0, snapshot["venue_index_close"]


def test_an_exclusion_reason_reads_as_a_sentence():
    """`reason.replace("_", " ")` leaked the enum name to the reader as
    "id space unproven"."""
    assert "id_space_unproven" in direct._EXCLUSION_REASONS
    said = direct._EXCLUSION_REASONS["id_space_unproven"]
    assert "_" not in said and said.startswith("the "), said


def test_every_venue_named_in_a_gap_uses_the_snapshot_vocabulary():
    """A ⚠ line read `okex-options` three lines above `OKX 17%` on the Activity
    line for the same venue. One vocabulary, and the two Deribit ids stay apart
    because a gap names ONE venue and two identical labels would be worse."""
    assert recap.venue_name("okex-options") == "OKX"
    assert recap.venue_name("bybit-options") == "Bybit"
    assert recap.venue_name("deribit") == "Deribit"
    assert recap.venue_name("deribit-usdc") == "Deribit USDC"
    # Unknown venues degrade rather than raise, same as the Snapshot label.
    assert recap.venue_name("cme-options") == "Cme"
    assert recap.venue_name(None) == "?"
    # And the two are the same vocabulary: every name is the label, or the label
    # plus a qualifier that keeps two folded ids apart.
    for venue in ("deribit", "deribit-usdc", "okex-options", "bybit-options", "bullish"):
        assert recap.venue_name(venue).startswith(recap._venue_label(venue)), venue


def test_a_block_dropped_for_missing_units_is_counted_not_just_mentioned():
    """Every other exclusion states its size; this one removed blocks before
    build() could see them, so the count has to come from here."""
    gaps = []
    frame = trades(trade(block_id="whole", amount=10.0),
                   trade(block_id="partial", amount=None),
                   trade(block_id="partial", amount=5.0))
    direct.aggregate_trades("okex-options", frame, spec(), gaps)
    dropped = [g for g in gaps if "block(s) excluded" in g]
    assert dropped, gaps
    assert "Block Flow: 1 OKX block(s) excluded" in dropped[0]
    assert "1 of 2 remain" in dropped[0]


def test_the_scope_labels_match_the_documented_template():
    """The Coverage row was removed from the template but both scope labels
    still pointed at it, so the rendered Snapshot named a row that was gone."""
    template = (Path(direct.__file__).resolve().parents[1]
                / "references" / "output-format.md").read_text(encoding="utf-8")
    source = Path(direct.__file__).read_text(encoding="utf-8")
    for label in re.findall(r'\["(?:volume|activity)_scope"\] = "([^"]+)"', source):
        assert label in template, label
