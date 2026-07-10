#!/usr/bin/env python3
"""
Unit tests for recap.py — no network, no S3, no deps.

Run: python3 tests/test_recap.py

Covers the orchestrator's pure logic: window parsing, hot-CSV ingest, the
snapshot/block/surface assembly, markdown rendering, and the run_duckdb
subprocess plumbing. Several tests are *regression* guards for the hot-data
corruption that produced absurd numbers in earlier versions:
  - a per-exchange aggregate row (blank optionType, notional ~$9.8e12) and
    cross-venue unit mixing inflated Volume to ~$9.8T;
  - a single unit-corrupt block row (~$5B) inflated Block Flow.
recap.py defends against both; the tests below pin that behaviour with the
exact rows seen in production.
"""

import os
import stat
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import recap  # noqa: E402
from recap import (  # noqa: E402
    parse_window_ms, load_hot, build, build_block_flow, render_md,
    _leg_phrase, pct, dvol_label, spot_vol_label, fmt_hhmm, run_duckdb,
    _load_surface_tickers, _delta_fmt, deribit_tape_volume, MAX_SURFACE_ROWS,
)

_passed = 0
_failed = 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  ✗ {name}  {detail}")


def _write(d, name, text):
    with open(os.path.join(d, name), "w") as f:
        f.write(text)


# Real production rows (BTC, 8h) — the regression fixtures.
CORRUPT_VOLUME_CSV = (
    "exchange,optionType,volume_sum,notional,buy_volume,sell_volume,trade_count\n"
    "deribit,call,4719.6,78.27,2231.6,2488.0,2652\n"
    "deribit,put,2702.9,88.36,1001.8,1701.1,1844\n"
    "bybit-options,put,4035.26,2094282.15,2565.5,1469.76,7135\n"
    "bullish,,8210.02,487302113.28,4109.48,4100.54,801870\n"
    "okex-options,put,246718.0,3966.56,110024.0,136694.0,2779\n"
    "okex-options,call,188346.0,1841.07,97495.0,90851.0,2138\n"
    "deribit,,163448320.0,9803096239210.0,81069170.0,82379150.0,47759\n"
    "bybit-options,call,3044.33,1413683.95,1670.2,1374.13,8315\n"
)
DVOL_SPOT_CSV = (
    "exchange,metric,open,close,high,low\n"
    "deribit,dvol,45.41,43.34,45.5,43.0\n"
    "deribit,spot,59362,60468,60500,59000\n"
)
SURFACE_CSV = (
    "expiry,strike,optionType,markIV_close,delta,openInterest,underlying_price\n"
    "3JUL26,60000,C,45.0,0.50,100,60468\n"
    "3JUL26,55000,P,52.0,-0.25,80,60468\n"
    "3JUL26,65000,C,48.0,0.25,90,60468\n"
)
# v_vol_surface snapshots (symbol, mark_iv, delta) — the consolidated per-strike
# store the deltas read from. "now" reproduces the SURFACE_CSV grid (ATM 45.0,
# 25d call 48.0, 25d put 52.0 → RR -4.0, fly 5.0); "open" is shifted to give a
# known, non-trivial delta set: ΔATM flat (45→45), ΔRR -1.0 (-3→-4), ΔFly -0.5 (5.5→5).
SURFACE_NOW_CSV = (
    "symbol,mark_iv,delta\n"
    "BTC-3JUL26-60000-C,45.0,0.50\n"
    "BTC-3JUL26-65000-C,48.0,0.25\n"
    "BTC-3JUL26-55000-P,52.0,-0.25\n"
)
SURFACE_OPEN_CSV = (
    "symbol,mark_iv,delta\n"
    "BTC-3JUL26-60000-C,45.0,0.50\n"
    "BTC-3JUL26-65000-C,49.0,0.25\n"
    "BTC-3JUL26-55000-P,52.0,-0.25\n"
)
# A 2-leg block: put buy + call sell (Strangle/RR), 100 BTC per leg @ $60k.
TRADES = [
    {"instrument_name": "BTC-26JUN26-55000-P", "index_price": 60000, "iv": 72.0,
     "timestamp": 1780000000000, "direction": "buy", "amount": 100, "block_trade_id": "B1"},
    {"instrument_name": "BTC-26JUN26-65000-C", "index_price": 60000, "iv": 60.0,
     "timestamp": 1780000000000, "direction": "sell", "amount": 100, "block_trade_id": "B1"},
]
CLOSES_7D = [60000 + (i % 5) * 50 for i in range(60)]  # gentle, non-flat


def _full_hot(d):
    _write(d, "volume.csv", CORRUPT_VOLUME_CSV)
    _write(d, "dvol_spot.csv", DVOL_SPOT_CSV)
    _write(d, "surface.csv", SURFACE_CSV)
    return load_hot(d, "BTC")


def _full_hot_deltas(d):
    """Full hot set plus the v_vol_surface now/open snapshots that drive deltas."""
    _write(d, "volume.csv", CORRUPT_VOLUME_CSV)
    _write(d, "dvol_spot.csv", DVOL_SPOT_CSV)
    _write(d, "surface.csv", SURFACE_CSV)
    _write(d, "surface_now.csv", SURFACE_NOW_CSV)
    _write(d, "surface_open.csv", SURFACE_OPEN_CSV)
    return load_hot(d, "BTC")


# ── parse_window_ms ─────────────────────────────────────────────────────────

def test_parse_window():
    check("8h", parse_window_ms("8h") == 8 * 3600_000)
    check("1h", parse_window_ms("1h") == 3600_000)
    check("24h", parse_window_ms("24h") == 24 * 3600_000)
    check("1d == 24h", parse_window_ms("1d") == 24 * 3600_000)
    check("5m", parse_window_ms("5m") == 5 * 60_000)
    check("case-insensitive", parse_window_ms("8H") == 8 * 3600_000)
    try:
        parse_window_ms("8x"); check("bad unit raises", False)
    except ValueError:
        check("bad unit raises", True)


# ── load_hot: volume corruption fix ─────────────────────────────────────────

def test_volume_excludes_aggregate_and_other_venues():
    recap.WARNINGS.clear()
    with tempfile.TemporaryDirectory() as d:
        hot = _full_hot(d)
    # Only the two Deribit per-optionType rows count: 4719.6 + 2702.9.
    check("call_vol = deribit calls", hot["call_vol"] == 4719.6, hot["call_vol"])
    check("put_vol = deribit puts", hot["put_vol"] == 2702.9, hot["put_vol"])
    check("volume_btc = deribit only", abs(hot["volume_btc"] - 7422.5) < 1e-6, hot["volume_btc"])
    check("aggregate row (163M) excluded", hot["volume_btc"] < 1000 * 1000, hot["volume_btc"])
    check("primary_venue Deribit", hot["primary_venue"] == "Deribit", hot["primary_venue"])
    check("no legacy volume_usd key", "volume_usd" not in hot)


def test_volume_to_usd_is_sane():
    # Empty tape → volume falls back to the hot rows (the fallback path).
    with tempfile.TemporaryDirectory() as d:
        hot = _full_hot(d)
        res = build("btc", "8h", 0, 8 * 3600_000,
                    {"closes_7d": CLOSES_7D, "trades": [], "market": None}, hot)
    s = res["snapshot"]
    # 7422.5 BTC × $60,468 ≈ $448.9M — NOT the old $9.8T.
    check("volume_usd_m ~ 449", 440 <= s["volume_usd_m"] <= 460, s["volume_usd_m"])
    check("volume not trillions", s["volume_usd_m"] < 100_000, s["volume_usd_m"])
    check("pc ratio 0.57", s["pc_ratio"] == 0.57, s["pc_ratio"])
    check("calls dominant", s["pc_dominant"] == "calls", s["pc_dominant"])


def test_volume_prefers_tape_over_hot():
    # When BOTH the hot volume rows and a live tape are present, the tape wins —
    # it's the authoritative screen+block figure (hot undercounts ~25%). This is
    # what keeps volume consistent/monotonic across the preset/dynamic boundary.
    with tempfile.TemporaryDirectory() as d:
        hot = _full_hot(d)  # hot: 4719.6 calls / 2702.9 puts
    trades = ([{"instrument_name": "BTC-25JUL26-60000-C", "amount": 100.0}] * 1 +
              [{"instrument_name": "BTC-25JUL26-55000-P", "amount": 40.0}] * 1)
    res = build("btc", "8h", 0, 8 * 3600_000,
                {"closes_7d": CLOSES_7D, "trades": trades, "market": None}, hot)
    s = res["snapshot"]
    spot = s["spot"]
    # Volume must reflect the tape (140 BTC), NOT the hot rows (7422.5 BTC).
    check("volume from tape not hot", round(140 * spot / 1e6) == s["volume_usd_m"],
          (s["volume_usd_m"], spot))
    check("pc ratio from tape (40/100=0.4)", s["pc_ratio"] == 0.4, s["pc_ratio"])
    check("primary venue Deribit", s["primary_venue"] == "Deribit", s["primary_venue"])


# ── load_hot: dvol/spot + surface parsing ───────────────────────────────────

def test_dvol_spot_parsing():
    with tempfile.TemporaryDirectory() as d:
        hot = _full_hot(d)
    check("dvol close", hot["dvol"] == 43.34, hot["dvol"])
    check("dvol open", hot["dvol_open"] == 45.41, hot["dvol_open"])
    check("spot close", hot["spot_close"] == 60468, hot["spot_close"])
    check("spot open", hot["spot_open"] == 59362, hot["spot_open"])


def test_surface_sym_construction():
    with tempfile.TemporaryDirectory() as d:
        hot = _full_hot(d)
    t = hot["tickers"]
    check("call sym built", t.get("BTC-3JUL26-60000-C", {}).get("mark_iv") == 45.0, t)
    check("put sym built", t.get("BTC-3JUL26-55000-P", {}).get("delta") == -0.25, t)
    check("surface_spot from dvol_spot", hot["surface_spot"] == 60468, hot["surface_spot"])


def test_surface_metrics_flow_through_build():
    with tempfile.TemporaryDirectory() as d:
        hot = _full_hot(d)
        res = build("btc", "8h", 0, 8 * 3600_000,
                    {"closes_7d": CLOSES_7D, "trades": [], "market": None}, hot)
    vs = res["vol_surface"]
    check("surface present", vs is not None and len(vs["rows"]) == 1, vs)
    row = vs["rows"][0]
    check("ATM 45", row["atm"] == 45.0, row)
    check("25d RR -4 (calls 48 - puts 52)", row["rr_25d"] == -4.0, row)
    check("skew line present", bool(vs["skew_line"]), vs["skew_line"])
    # No v_vol_surface CSVs in _full_hot → deltas are absent.
    check("no-open → d_atm None", row.get("d_atm") is None, row)
    check("no-open → d_rr None", row.get("d_rr") is None, row)


# ── Vol-surface deltas (v_vol_surface now/open) ──────────────────────────────

def test_load_surface_tickers():
    with tempfile.TemporaryDirectory() as d:
        _write(d, "surface_now.csv", SURFACE_NOW_CSV)
        t = _load_surface_tickers(d, "surface_now.csv")
    check("now sym count", len(t) == 3, t)
    check("now call iv", t.get("BTC-3JUL26-60000-C", {}).get("mark_iv") == 45.0, t)
    check("now put delta", t.get("BTC-3JUL26-55000-P", {}).get("delta") == -0.25, t)
    check("missing CSV → empty", _load_surface_tickers(d, "nope.csv") == {})


def test_load_hot_populates_vs_maps():
    with tempfile.TemporaryDirectory() as d:
        hot = _full_hot_deltas(d)
    check("vs_now populated", len(hot["vs_now"]) == 3, hot["vs_now"])
    check("vs_open populated", len(hot["vs_open"]) == 3, hot["vs_open"])
    # _full_hot (no vs CSVs) leaves them empty.
    with tempfile.TemporaryDirectory() as d2:
        hot2 = _full_hot(d2)
    check("vs_now empty without CSV", hot2["vs_now"] == {}, hot2["vs_now"])


def test_surface_deltas_flow_through_build():
    with tempfile.TemporaryDirectory() as d:
        hot = _full_hot_deltas(d)
        res = build("btc", "8h", 0, 8 * 3600_000,
                    {"closes_7d": CLOSES_7D, "trades": [], "market": None}, hot)
    row = res["vol_surface"]["rows"][0]
    # Displayed (now) values come from v_vol_surface.
    check("now ATM 45", row["atm"] == 45.0, row)
    check("now RR -4", row["rr_25d"] == -4.0, row)
    check("now Fly 5", row["fly"] == 5.0, row)
    # Deltas vs the open snapshot.
    check("ΔATM flat (0.0)", row["d_atm"] == 0.0, row)
    check("ΔRR -1.0", row["d_rr"] == -1.0, row)
    check("ΔFly -0.5", row["d_fly"] == -0.5, row)


def test_render_delta_columns_present_and_formatted():
    with tempfile.TemporaryDirectory() as d:
        hot = _full_hot_deltas(d)
        res = build("btc", "8h", 0, 8 * 3600_000,
                    {"closes_7d": CLOSES_7D, "trades": TRADES, "market": None}, hot)
    md = render_md(res)
    check("header has ΔATM", "ΔATM" in md, md)
    check("header has ΔRR", "ΔRR" in md, md)
    check("header has ΔFly", "ΔFly" in md, md)
    check("ΔATM flat rendered", "flat" in md, md)
    check("ΔRR -1.0v rendered", "-1.0v" in md, md)
    check("ΔFly -0.5v rendered", "-0.5v" in md, md)


def test_delta_fmt():
    check("positive signed", _delta_fmt(1.2) == "+1.2v", _delta_fmt(1.2))
    check("negative signed", _delta_fmt(-0.5) == "-0.5v", _delta_fmt(-0.5))
    check("zero → flat", _delta_fmt(0.0) == "flat", _delta_fmt(0.0))
    check("tiny → flat", _delta_fmt(0.04) == "flat", _delta_fmt(0.04))
    check("None → n/a", _delta_fmt(None) == "n/a", _delta_fmt(None))
    check("star carried on value", _delta_fmt(-0.5, "*") == "-0.5v*", _delta_fmt(-0.5, "*"))
    check("star dropped on n/a", _delta_fmt(None, "*") == "n/a", _delta_fmt(None, "*"))


def test_surface_caps_to_max_rows():
    # 6 expiries in the now snapshot → table caps to MAX_SURFACE_ROWS (front).
    exps = ["3JUL26", "10JUL26", "17JUL26", "24JUL26", "31JUL26", "28AUG26"]
    lines = ["symbol,mark_iv,delta"]
    for e in exps:
        lines += [f"BTC-{e}-60000-C,45.0,0.50",
                  f"BTC-{e}-65000-C,48.0,0.25",
                  f"BTC-{e}-55000-P,52.0,-0.25"]
    with tempfile.TemporaryDirectory() as d:
        _write(d, "dvol_spot.csv", DVOL_SPOT_CSV)
        _write(d, "surface_now.csv", "\n".join(lines) + "\n")
        hot = load_hot(d, "BTC")
        res = build("btc", "8h", 0, 8 * 3600_000,
                    {"closes_7d": CLOSES_7D, "trades": [], "market": None}, hot)
    n = len(res["vol_surface"]["rows"])
    check(f"rows capped to {MAX_SURFACE_ROWS}", n == MAX_SURFACE_ROWS, n)


# ── load_hot: missing files degrade, don't crash ────────────────────────────

def test_missing_hot_files():
    recap.WARNINGS.clear()
    with tempfile.TemporaryDirectory() as d:
        hot = load_hot(d, "BTC")  # empty dir
    check("dvol None", hot["dvol"] is None)
    check("volume_btc None", hot["volume_btc"] is None)
    check("no tickers", hot["tickers"] == {})
    check("warnings recorded", len(recap.WARNINGS) >= 2, recap.WARNINGS)


# ── Block flow: derived from trades, ignores corrupt hot block.csv ──────────

def test_block_flow_from_trades_not_hot():
    recap.WARNINGS.clear()
    with tempfile.TemporaryDirectory() as d:
        # A corrupt block.csv must NOT affect output — recap.py derives from the tape.
        _write(d, "block.csv",
               "block_id,notional,volume_sum,leg_count,avg_iv\nB-X,5119223491.1,85615.2,3,44.5\n")
        hot = load_hot(d, "BTC")
    bf = build_block_flow(TRADES, hot, spot=60000)
    check("n_blocks from tape = 1", bf["n_blocks"] == 1, bf["n_blocks"])
    # 200 BTC × $60k = $12M — NOT the $5.1B corrupt hot row.
    check("total_m = 12.0 (from tape)", bf["total_m"] == 12.0, bf["total_m"])
    check("total not billions", bf["total_m"] < 1000, bf["total_m"])
    bp = bf["biggest_print"]
    check("biggest expiry", bp["expiry"] == "26JUN26", bp)
    check("biggest is Strangle/RR", bp["structure"] == "Strangle/RR", bp)
    check("biggest size 200", bp["size"] == 200, bp)
    check("biggest notional 12.0M", bp["notional_m"] == 12.0, bp)
    check("biggest two-way", bp["side"] == "Two-way", bp)


def test_leg_phrase():
    legs = [t for t in TRADES]
    phrase = _leg_phrase(legs)
    check("leg phrase has put buy", "bought 55KP" in phrase, phrase)
    check("leg phrase has call sell", "sold 65KC" in phrase, phrase)
    check("leg phrase size", "x200" in phrase, phrase)
    check("leg phrase two-way", "two-way" in phrase, phrase)
    check("leg phrase avg iv 66.0v", "66.0v" in phrase, phrase)


# ── Snapshot helper labels ──────────────────────────────────────────────────

def test_helpers():
    check("pct up", pct(110, 100) == 10.0)
    check("pct down", pct(99, 100) == -1.0)
    check("pct None on zero", pct(100, 0) is None)
    check("dvol rising", dvol_label(40, 42) == "rising")
    check("dvol falling", dvol_label(42, 40) == "falling")
    check("dvol flat (small move)", dvol_label(40.0, 40.3) == "flat")
    check("dvol None", dvol_label(None, 40) is None)
    check("spot up vol down → sold rally",
          spot_vol_label(100, 110, 50, 48) == "vol sold through rally")
    check("spot down vol up → bid weakness",
          spot_vol_label(110, 100, 48, 50) == "vol bid into weakness")
    check("spot up vol up → bought rally",
          spot_vol_label(100, 110, 48, 50) == "vol bought through rally")
    check("missing input → None", spot_vol_label(None, 110, 48, 50) is None)
    check("fmt_hhmm", fmt_hhmm(0) == "00:00")


# ── render_md ───────────────────────────────────────────────────────────────

def _full_result():
    with tempfile.TemporaryDirectory() as d:
        hot = _full_hot(d)
    return build("btc", "8h", 0, 8 * 3600_000,
                 {"closes_7d": CLOSES_7D, "trades": TRADES, "market": None}, hot)


def test_render_four_sections():
    md = render_md(_full_result())
    for h in ("**Snapshot**", "**Biggest Print**", "**Block Flow", "**Vol Surface**"):
        check(f"render has {h}", h in md, md[:80])
    check("render title", md.startswith("**BTC Options · 8h Recap"), md[:40])
    # Volume/P-C now come from the tape (TRADES: 100C + 100P = 200 BTC × $60,468
    # ≈ $12M, P/C 1.0), not the hot rows — one authoritative source for all windows.
    check("render volume line", "Volume" in md and "$12M" in md, "volume render")
    check("render P/C 1.0x", "1.0x" in md)
    check("render biggest Strangle/RR", "26JUN26 Strangle/RR" in md)
    # Vol-surface delta columns are always present in the header; with no
    # window-open surface (this fixture has none) the delta cells read n/a.
    check("delta columns present", "ΔATM" in md and "ΔRR" in md and "ΔFly" in md, md)
    check("deltas n/a without open", "45.0v" in md and "n/a" in md, "expected n/a deltas")
    # Dropped/forbidden output must not reappear.
    check("no Themes", "Themes" not in md)
    check("no Dealer positioning", "Dealer positioning" not in md)
    check("four yaml fences", md.count("```yaml") == 4, md.count("```yaml"))


def test_render_deterministic():
    r = _full_result()
    check("render is deterministic", render_md(r) == render_md(r))


def test_render_degraded_banner():
    recap.WARNINGS.clear()
    with tempfile.TemporaryDirectory() as d:
        hot = load_hot(d, "BTC")  # empty → no volume, no surface, warnings
    res = build("btc", "8h", 0, 8 * 3600_000,
                {"closes_7d": [], "trades": [], "market": None}, hot)
    md = render_md(res)
    check("degraded banner prepended", md.startswith("⚠ hot surface unavailable"), md[:60])
    check("volume reads n/a", "Volume" in md and "n/a" in md)
    check("surface reads No data", "No data" in md)


# ── run_duckdb subprocess plumbing ──────────────────────────────────────────

def test_run_duckdb_missing_binary():
    recap.WARNINGS.clear()
    saved = os.environ.get("PATH", "")
    with tempfile.TemporaryDirectory() as empty:
        os.environ["PATH"] = empty  # no duckdb anywhere
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as f:
                f.write("SELECT 1;"); sqlp = f.name
            rc = run_duckdb(sqlp)
        finally:
            os.environ["PATH"] = saved
            os.unlink(sqlp)
    check("missing duckdb → rc -1", rc == -1, rc)
    check("missing duckdb warned", any("duckdb" in w for w in recap.WARNINGS), recap.WARNINGS)


def test_run_duckdb_invokes_binary():
    recap.WARNINGS.clear()
    saved = os.environ.get("PATH", "")
    with tempfile.TemporaryDirectory() as bindir:
        marker = os.path.join(bindir, "ran.txt")
        mock = os.path.join(bindir, "duckdb")
        # Mock reads SQL from stdin (like real duckdb) and writes a marker file.
        with open(mock, "w") as f:
            f.write(f"#!/bin/sh\ncat >/dev/null\necho ok > '{marker}'\nexit 0\n")
        os.chmod(mock, os.stat(mock).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        os.environ["PATH"] = bindir + os.pathsep + saved
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as sf:
                sf.write("COPY (SELECT 1) TO 'x';"); sqlp = sf.name
            rc = run_duckdb(sqlp)
        finally:
            os.environ["PATH"] = saved
            os.unlink(sqlp)
        check("mock duckdb → rc 0", rc == 0, rc)
        check("mock duckdb executed (marker written)", os.path.exists(marker))


def test_deribit_tape_volume():
    # Sums option contracts by call/put suffix; ignores non-option instruments.
    trades = [
        {"instrument_name": "BTC-25JUL26-60000-C", "amount": 10.0},
        {"instrument_name": "BTC-25JUL26-60000-C", "amount": 5.0},
        {"instrument_name": "BTC-25JUL26-55000-P", "amount": 8.0},
        {"instrument_name": "BTC-PERPETUAL", "amount": 99.0},   # not an option
        {"instrument_name": None, "amount": 3.0},               # defensive
    ]
    cv, pv = deribit_tape_volume(trades)
    check("call contracts summed", cv == 15.0, cv)
    check("put contracts summed", pv == 8.0, pv)
    check("empty tape → (None, None)", deribit_tape_volume([]) == (None, None))


def test_build_volume_fallback_when_hot_absent():
    # Non-preset window: no hot `volume` row, so build() derives Deribit-only
    # volume from the window tape and labels the venue Deribit.
    recap.WARNINGS.clear()
    deri = {
        "closes_7d": [], "market": None,
        "trades": [
            {"instrument_name": "BTC-25JUL26-60000-C", "amount": 12.0},
            {"instrument_name": "BTC-25JUL26-55000-P", "amount": 8.0},
        ],
    }
    hot = {"tickers": {}, "spot_close": 60000, "dvol": None}  # no volume_btc
    r = build("btc", "3h", 0, 10_800_000, deri, hot)
    s = r["snapshot"]
    # 20 contracts × $60k = $1.2M → 1 (rounded to $M)
    check("volume from tape (Deribit-only)", s["volume_usd_m"] == 1, s["volume_usd_m"])
    check("primary venue labeled Deribit", s["primary_venue"] == "Deribit", s["primary_venue"])
    check("P/C from tape", s["pc_ratio"] == round(8.0 / 12.0, 2), s["pc_ratio"])


def test_header_dates_on_multiday_windows():
    from recap import fmt_stamp
    check("intraday: HH:MM only", fmt_stamp(0, False) == "00:00", fmt_stamp(0, False))
    check("with_date: includes month/day", fmt_stamp(0, True) == "Jan 01 00:00", fmt_stamp(0, True))
    # A 48h window (multiple of 24h) must NOT collapse to identical start==end.
    recap.WARNINGS.clear()
    end = 100 * 24 * 3600_000
    with tempfile.TemporaryDirectory() as d:
        hot = _full_hot(d)
    res = build("btc", "48h", end - 48 * 3600_000, end,
                {"closes_7d": CLOSES_7D, "trades": [], "market": None}, hot)
    h = res["header"]
    check("48h start != end in header", h["start_utc"] != h["end_utc"], h)
    check("48h header carries a date", " " in h["start_utc"], h["start_utc"])
    # Intraday window stays HH:MM only.
    res8 = build("btc", "8h", end - 8 * 3600_000, end,
                 {"closes_7d": CLOSES_7D, "trades": [], "market": None}, hot)
    check("8h header HH:MM only", " " not in res8["header"]["start_utc"], res8["header"])


def test_flow_horizon_banner_beyond_24h():
    # A 72h window whose tape only reaches back ~24h → flow-horizon banner, and the
    # header still says 72h (DVOL/spot/surface are full-window).
    recap.WARNINGS.clear()
    end = 100 * 3600_000
    start = end - 72 * 3600_000          # 72h window
    trades = [{"instrument_name": "BTC-25JUL26-60000-C", "amount": 5.0,
               "timestamp": end - 23 * 3600_000},   # oldest trade ~23h back
              {"instrument_name": "BTC-25JUL26-60000-C", "amount": 5.0, "timestamp": end}]
    with tempfile.TemporaryDirectory() as d:
        hot = _full_hot(d)
    res = build("btc", "72h", start, end,
                {"closes_7d": CLOSES_7D, "trades": trades, "market": None}, hot)
    check("flow_horizon set", res["flow_horizon"] is not None, res["flow_horizon"])
    check("covered ~23h", res["flow_horizon"]["covered_h"] == 23, res["flow_horizon"])
    md = render_md(res)
    check("banner rendered", "Deribit tape retention limit" in md, md[:200])
    check("banner names full window", "full 72h" in md, md[:200])


def test_no_flow_horizon_banner_within_24h():
    # An 8h window whose tape spans it → no banner.
    recap.WARNINGS.clear()
    end = 100 * 3600_000
    trades = [{"instrument_name": "BTC-25JUL26-60000-C", "amount": 5.0,
               "timestamp": end - 7 * 3600_000},
              {"instrument_name": "BTC-25JUL26-60000-P", "amount": 5.0, "timestamp": end}]
    with tempfile.TemporaryDirectory() as d:
        hot = _full_hot(d)
    res = build("btc", "8h", end - 8 * 3600_000, end,
                {"closes_7d": CLOSES_7D, "trades": trades, "market": None}, hot)
    check("no flow_horizon within 24h", res["flow_horizon"] is None, res["flow_horizon"])
    check("no banner in md", "tape retention" not in render_md(res))


def test_market_fallback_skips_surface_when_not_wanted():
    # The dynamic-window optimization: with want_surface=False the fallback must
    # fetch DVOL+spot but make ZERO get_instruments/ticker calls (v_vol_surface
    # supplies the surface). With want_surface=True the ticker calls happen.
    calls = []
    orig = recap._get

    def fake_get(path, params, timeout=15):
        calls.append(path)
        if path == "get_volatility_index_data":
            return {"data": [[0, 40.0, 41.0, 39.0, 40.5]]}
        if path == "get_tradingview_chart_data":
            return {"close": [60000.0], "open": [59000.0], "low": [58000.0]}
        if path == "get_instruments":
            return [{"expiration_timestamp": 1, "instrument_name": "BTC-1JAN27-60000-C"}]
        if path == "ticker":
            return {"mark_iv": 40.0, "greeks": {"delta": 0.5}}
        return {}

    recap._get = fake_get
    try:
        r = recap._fetch_market_fallback("BTC", 0, 1000, want_surface=False)
        check("no ticker/instruments calls when want_surface=False",
              "ticker" not in calls and "get_instruments" not in calls, calls)
        check("DVOL+spot still fetched", bool(r["dvol"]) and bool(r["spot"]), r)
        check("no surface tickers returned", r["tickers"] == {}, r["tickers"])
        calls.clear()
        recap._fetch_market_fallback("BTC", 0, 1000, want_surface=True)
        check("ticker calls happen when want_surface=True", "ticker" in calls, calls)
    finally:
        recap._get = orig


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"Running {len(tests)} test functions...")
    for t in tests:
        t()
    print(f"\n{_passed} checks passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
