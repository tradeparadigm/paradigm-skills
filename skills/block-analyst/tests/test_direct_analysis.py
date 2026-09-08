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


def _tape(rows, complete):
    return {"rows": rows, "sources": [], "build_window_end_ms": 1, "units": {},
            "coverage_complete": complete,
            "coverage_note": "coverage ends 40 min before the requested end"}


def test_unresolved_rfq_with_incomplete_coverage_is_not_a_negative_result(monkeypatch, capsys):
    """Searching data that stops short of now is missing evidence, not 'did not trade'."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "data-discovery" / "scripts"))
    import execution_tape

    other_rfq = [{"rfq_id": "DRFQv2-r_other", "quantity": 1.0, "trade_price": 1.0, "mark_price": 1.0}]
    monkeypatch.setattr(execution_tape, "read_executions",
                        lambda *a, **k: _tape(other_rfq, complete=False))
    assert collect_analysis.render_current_analysis("r_test") == 1
    out = capsys.readouterr().out
    assert "coverage is incomplete" in out and "missing evidence" in out

    # Same miss against COMPLETE coverage is a genuine negative result.
    monkeypatch.setattr(execution_tape, "read_executions",
                        lambda *a, **k: _tape(other_rfq, complete=True))
    assert collect_analysis.render_current_analysis("r_test") == 0
    assert "no authoritative asset" in capsys.readouterr().out


# ── Mixed-direction GRFQ (2026-09-08, GRFQ-50128348) ─────────────────────────

def tape_leg(block, instrument, side, qty, price, mark, rfq="GRFQ-50128348"):
    """A published execution leg in the partitioned-tape shape calculation_rows reads."""
    expiry = "2026-09-11" if "11SEP26" in instrument else "2026-09-12"
    return {"rfq_id": rfq, "block_trade_id": block, "venue_block_trade_id": f"BLOCK-{block[-3:]}",
            "venue": "DBT", "product": "BTC OPTION - DBT", "asset": "BTC",
            "instrument_kind": "OPTION", "option_kind": "CALL", "strike_price": 79000.0,
            "expiry_date": expiry, "instrument_name": instrument,
            "description": "CCal  11 Sep 26 79000 / 12 Sep 26 79000",
            "taker_side": side, "quantity": qty, "trade_price": price, "mark_price": mark,
            "notional_volume_usd": qty * 78500.0, "traded_at": 1788856000000}


FRONT, BACK = "BTC-11SEP26-79000-C", "BTC-12SEP26-79000-C"
# Real shape of the incident: one block took the calendar SHORT (desk 1184),
# two blocks took it LONG (desk 4671). Same rfq_id, opposite taker sides.
MIXED = [
    tape_leg("GRFQ-50094844", FRONT, "BUY", 12.5, 0.0100, 0.0104),
    tape_leg("GRFQ-50094844", BACK, "SELL", 12.5, 0.0128, 0.0132),
    tape_leg("GRFQ-50094847", FRONT, "SELL", 15.0, 0.0104, 0.0112),
    tape_leg("GRFQ-50094847", BACK, "BUY", 15.0, 0.0136, 0.0141),
    tape_leg("GRFQ-50094848", FRONT, "SELL", 12.5, 0.0110, 0.0112),
    tape_leg("GRFQ-50094848", BACK, "BUY", 12.5, 0.0142, 0.0141),
]


def _stub_market(monkeypatch):
    monkeypatch.setattr(analyze, "fetch_ticker", lambda sym: (sym, {
        "delta": 0.5, "vega": 1, "gamma": 0.001, "theta": -1, "mark": 0.012, "index": 78300}))
    monkeypatch.setattr(analyze, "fetch_trades_bucket", lambda *args: (args[0], None))


def _render(monkeypatch, capsys, rows, rfq):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "data-discovery" / "scripts"))
    import execution_tape
    monkeypatch.setattr(execution_tape, "read_executions", lambda *a, **k: {
        "rows": rows, "sources": [], "build_window_end_ms": 1, "units": {},
        "coverage_complete": True, "coverage_note": "test"})
    _stub_market(monkeypatch)
    assert collect_analysis.render_current_analysis(rfq) == 0
    return capsys.readouterr().out


def test_mixed_direction_grfq_is_analysed_per_direction(monkeypatch, capsys):
    out = _render(monkeypatch, capsys, MIXED, "GRFQ-50128348")
    headers = [ln for ln in out.splitlines() if ln.startswith("**BTC ")]
    assert len(headers) == 2, out
    # Dominant direction first: the two LONG blocks merge to 27.5 (15 + 12.5)...
    assert "×27.5" in headers[0] and "+3 bps above mark" in headers[0], headers[0]
    # ...the lone SHORT block stands alone at its own size.
    assert "×12.5" in headers[1], headers[1]
    # The incident output — a four-"leg" Combo netting opposite fills — is gone.
    assert "79k/79k/79k/79k" not in out and "Combo" not in out
    assert "Call Calendar" in headers[0] and "Call Calendar" in headers[1]
    assert "filled as 3 blocks in 2 directions" in out
    assert "**2 block(s): GRFQ-50094847, GRFQ-50094848**" in out
    assert "**1 block(s): GRFQ-50094844**" in out


def test_same_direction_clips_still_merge_into_one_package(monkeypatch, capsys):
    same = [r for r in MIXED if r["block_trade_id"] != "GRFQ-50094844"]
    out = _render(monkeypatch, capsys, same, "GRFQ-50128348")
    headers = [ln for ln in out.splitlines() if ln.startswith("**BTC ")]
    assert len(headers) == 1 and "×27.5" in headers[0], out
    assert "directions" not in out and "block(s):" not in out


def test_calendar_is_named_and_shows_both_expiries(monkeypatch, capsys):
    single = [tape_leg("DRFQv2-bt_1", BACK, "BUY", 25, 0.0140, 0.0139, rfq="DRFQv2-r_1"),
              tape_leg("DRFQv2-bt_1", FRONT, "SELL", 25, 0.0111, 0.0111, rfq="DRFQv2-r_1")]
    out = _render(monkeypatch, capsys, single, "DRFQv2-r_1")
    header = next(ln for ln in out.splitlines() if ln.startswith("**BTC "))
    assert "Call Calendar" in header and "Combo" not in out, header
    assert "11SEP26" in header and "12SEP26" in header, header
    assert "×25" in header and "+1 bps above mark" in header, header


def test_struct_name_reads_result_leg_expiry_key():
    """render() hands _struct_name legs keyed `expiry`, not `expiry_c`."""
    def leg(cp, strike, sign, expiry):
        return {"cp": cp, "strike": strike, "sign": sign, "expiry": expiry}
    calendar = [leg("C", 79000, +1, "12SEP26"), leg("C", 79000, -1, "11SEP26")]
    diagonal = [leg("P", 70000, +1, "12SEP26"), leg("P", 75000, -1, "11SEP26")]
    spread = [leg("C", 79000, +1, "12SEP26"), leg("C", 82000, -1, "12SEP26")]
    straddle = [leg("C", 79000, +1, "12SEP26"), leg("P", 79000, +1, "12SEP26")]
    assert analyze._struct_name("combo", calendar) == "Call Calendar"
    assert analyze._struct_name("combo", diagonal) == "Put Diagonal"
    assert analyze._struct_name("combo", spread) == "Call Spread"
    assert analyze._struct_name("combo", straddle) == "Straddle"
