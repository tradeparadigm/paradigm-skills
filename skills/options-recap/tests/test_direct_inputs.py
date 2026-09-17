"""Direct input adapters preserve the established numerical/output contract."""

from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
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
           "notional_volume_usd": 3200000}
    mapped = calculation_rows([row])[0]
    assert mapped["DESCRIPTION"] == "Put 11 Sep 26 70000"
    assert mapped["SIDE"] == "BUY" and mapped["QTY"] == 40


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
