"""Direct input adapters preserve the established numerical/output contract."""

from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
import sys

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import direct_inputs as direct
import recap
from collect_recap import build_queries

UTC = timezone.utc
START = datetime(2026, 9, 8, 8, tzinfo=UTC)
END = datetime(2026, 9, 8, 9, tzinfo=UTC)


def spec():
    return pl.DataFrame({"symbol": ["BTC-11SEP26-70000-P"] * 2,
                         "captured_at": [START, END], "iv_unit": ["decimal"] * 2,
                         "oi_unit": ["contracts"] * 2, "contract_size": [0.01, 0.1],
                         "price_unit": ["coin"] * 2})


def trade(**updates):
    return {"record_type": "trade", "exchange": "okex-options",
            "timestamp": "2026-09-08T08:30:00Z", "symbol": "BTC-11SEP26-70000-P",
            "amount": 100.0, "price": 0.01, "index_price": 80000.0,
            "turnover_usd": None, "block_id": "block-1", "iv": 0.4, **updates}


def test_event_time_units_not_latest_metadata():
    gaps = []
    snapshot, blocks, turnover = direct.inputs(
        {"option_trades_okex-options": [trade()]}, {"okex-options": spec()}, gaps)
    assert turnover == 800.0  # 100 contracts * 0.01 BTC * 0.01 premium * 80k
    assert blocks[0]["volume_coin"] == 1.0
    assert blocks[0]["iv_sum"] == 40.0
    assert snapshot["put_trades"] == 1
    assert not gaps


def test_unavailable_metadata_does_not_default_contract_size():
    gaps = []
    snapshot, blocks, turnover = direct.inputs({"option_trades_okex-options": [trade()]}, {}, gaps)
    assert not snapshot["turnover_complete"]
    assert not blocks and turnover == 0
    assert any("lack a provable USD premium" in gap for gap in gaps)


def test_existing_usd_turnover_is_not_scaled_twice():
    _, _, turnover = direct.inputs({"option_trades_okex-options": [trade(turnover_usd=123.0)]},
                                    {"okex-options": spec()}, [])
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


def instrument_object(captured_at):
    """captured_at rides as a String — metadata() parses it with the .str namespace."""
    frame = pl.DataFrame({"symbol": ["BTC-11SEP26-70000-P"], "captured_at": [captured_at],
                          "iv_unit": ["decimal"], "oi_unit": ["contracts"],
                          "contract_size": [0.1], "price_unit": ["coin"]})
    buffer = BytesIO()
    frame.write_parquet(buffer)
    return {"Body": BytesIO(buffer.getvalue())}


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
    assert specs.height == 2
    assert specs["contract_size"].to_list() == [0.1, 0.1]
