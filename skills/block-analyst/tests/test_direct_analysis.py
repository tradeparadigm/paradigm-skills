"""Partitioned legs enter the existing deterministic analyst unchanged."""

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import analyze
import collect_analysis


def leg(qty, side, strike, price, mark):
    return {"PRODUCT": "BTC OPTION - DBT", "DESCRIPTION": f"Put 11 Sep 26 {strike}",
            "SIDE": side, "QTY": qty, "PRICE": price, "REF_PRICE": mark,
            "QUOTE_CURRENCY": "BTC", "RFQ_ID": "DRFQv2-r_test", "BLOCK_TRADE_ID": "b_test",
            "NOTIONAL_VOLUME_USD": qty * 80000}


def test_ratio_sizing_fill_and_greeks_share_one_package_count(monkeypatch):
    monkeypatch.setattr(analyze, "fetch_ticker", lambda sym: (sym, {
        "delta": 0.5, "vega": 1, "gamma": 1, "theta": 1, "mark": 80000, "index": 80000}))
    monkeypatch.setattr(analyze, "fetch_trades_bucket", lambda *args: (args[0], None))
    result = analyze.analyze_rows([
        leg(40, "BUY", 59000, .0023, .0021), leg(20, "SELL", 65000, .0210, .0212)], [], 1)
    assert result["qty"] == 20
    assert result["fill_net"] == pytest.approx(-.0164)
    assert result["ref_net"] == pytest.approx(-.0170)
    assert (result["fill_net"] - result["ref_net"]) * result["qty"] == pytest.approx(.012)
    assert result["net_greeks"]["delta"] == 10


def test_multiple_clips_do_not_double_per_unit_premium():
    rows = collect_analysis.combine_fills([
        leg(10, "BUY", 59000, .002, .003), leg(30, "BUY", 59000, .004, .005)])
    assert len(rows) == 1
    assert rows[0]["QTY"] == 40
    assert rows[0]["PRICE"] == pytest.approx(.0035)
    assert rows[0]["REF_PRICE"] == pytest.approx(.0045)


def test_stale_execution_rejection_never_falls_back(monkeypatch, capsys):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "data-discovery" / "scripts"))
    import execution_tape

    def stale(*args, **kwargs):
        raise RuntimeError("stale execution partition")

    monkeypatch.setattr(execution_tape, "read_executions", stale)
    monkeypatch.setattr(collect_analysis, "run_sql", lambda *_: pytest.fail("freshness gate bypassed"))
    assert collect_analysis.render_current_analysis("r_test") == 1
    assert "stale execution partition" in capsys.readouterr().out
