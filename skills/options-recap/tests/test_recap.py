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
    parse_window_ms, load_hot, load_blocks, build, render_md,
    pct, pc_descriptor, dvol_label, spot_vol_label, fmt_hhmm,
    run_duckdb, _load_surface_tickers, _delta_fmt, _venue_label,
    MAX_SURFACE_ROWS,
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
    # deribit-usdc: a real 5th production venue (USDC-linear). volume_sum is in a
    # DIFFERENT contract unit than deribit's BTC-inverse rows, so it MUST NOT enter
    # the Deribit dollar-volume sum; its trades DO count toward activity, folded
    # into the single "Deribit" display label. Placed apart from the deribit rows
    # on purpose — the label-collapse must not depend on row order.
    "deribit-usdc,call,15.3,14740.6,11.61,3.69,83\n"
    "deribit-usdc,put,51.0,25581.55,8.01,42.99,50\n"
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
# A 2-leg block: put buy + call sell (Risk Reversal), 100 BTC per leg @ $60k.
TRADES = [
    {"instrument_name": "BTC-26JUN26-55000-P", "index_price": 60000, "iv": 72.0,
     "timestamp": 1780000000000, "direction": "buy", "amount": 100, "block_trade_id": "B1"},
    {"instrument_name": "BTC-26JUN26-65000-C", "index_price": 60000, "iv": 60.0,
     "timestamp": 1780000000000, "direction": "sell", "amount": 100, "block_trade_id": "B1"},
]
CLOSES_7D = [60000 + (i % 5) * 50 for i in range(60)]  # gentle, non-flat

# Block tape (paradigm_trade_tape_slim) rows — the source for Biggest Print +
# Block Flow now. A Risk Reversal booked as two per-leg rows (put buy + call
# sell) on Deribit, one BLOCK_TRADE_ID, $6M/leg → $12M block, Mixed side.


def _blk(desc, side, notl, bid="B1", rfq="R1", prod="BTC OPTION - DBT",
         qty=100, date="2026-06-01", time="12:00:00", tid=None):
    return {"DATE": date, "TIME": time, "PRODUCT": prod, "DESCRIPTION": desc,
            "QTY": qty, "PRICE": 0.01, "REF_PRICE": 0.01, "SIDE": side,
            "QUOTE_CURRENCY": "BTC", "NOTIONAL_VOLUME_USD": notl, "RFQ_ID": rfq,
            "TRADE_ID": tid or (bid + side[:1]), "BLOCK_TRADE_ID": bid}


BLOCKS_RR = [
    _blk("Put 26 Jun 26 55000", "BUY", 6_000_000, tid="t1"),
    _blk("Call 26 Jun 26 65000", "SELL", 6_000_000, tid="t2"),
]


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
    # Dollar volume stays Deribit-scoped: ONLY the two exact-"deribit" rows, 4719.6 +
    # 2702.9. deribit-usdc (USDC-linear, different unit) must NOT contaminate it —
    # its 15.3 call + 51.0 put volume_sum are excluded despite the "deribit" prefix.
    check("call_vol = deribit calls (excl deribit-usdc)", hot["call_vol"] == 4719.6, hot["call_vol"])
    check("put_vol = deribit puts (excl deribit-usdc)", hot["put_vol"] == 2702.9, hot["put_vol"])
    check("volume_btc = deribit only", abs(hot["volume_btc"] - 7422.5) < 1e-6, hot["volume_btc"])
    check("deribit-usdc volume excluded (would be 7488.8 if summed in)",
          abs(hot["volume_btc"] - (7422.5 + 15.3 + 51.0)) > 1.0, hot["volume_btc"])
    check("aggregate row (163M) excluded", hot["volume_btc"] < 1000 * 1000, hot["volume_btc"])
    check("no legacy volume_usd key", "volume_usd" not in hot)
    # Activity/P-C: trade_count across ALL venues, blank-optionType aggregates dropped.
    # Kept rows: deribit 2652+1844, bybit 8315+7135, okex 2138+2779, deribit-usdc
    # 83+50 = 24996 trades. deribit-usdc trades DO count (unit-free).
    check("trades_total all venues", hot["trades_total"] == 24996, hot["trades_total"])
    check("blank-optionType aggregates excluded (bullish 801870, deribit 47759)",
          hot["trades_total"] < 100000, hot["trades_total"])
    check("put_trades all venues", hot["put_trades"] == 1844 + 7135 + 2779 + 50, hot["put_trades"])
    check("call_trades all venues", hot["call_trades"] == 2652 + 8315 + 2138 + 83, hot["call_trades"])
    # trades_by_venue is keyed by RAW venue id, so deribit and deribit-usdc are two
    # separate entries here (4 total); they only collapse to one "Deribit" label in
    # build()'s activity_split (see test_activity_split_collapses_deribit_venues).
    check("trades_by_venue has 4 raw venues", len(hot["trades_by_venue"]) == 4, hot["trades_by_venue"])
    check("deribit-usdc present as its own raw venue",
          hot["trades_by_venue"].get("deribit-usdc") == 133, hot["trades_by_venue"])
    check("bybit leads by trades",
          max(hot["trades_by_venue"], key=hot["trades_by_venue"].get) == "bybit-options",
          hot["trades_by_venue"])


def test_volume_hot_only_deribit_scoped():
    # Volume is now hot-only (Deribit-scoped volume_btc × spot) — the tape-primary
    # path is gone, so any block tape passed alongside must NOT change Volume.
    with tempfile.TemporaryDirectory() as d:
        hot = _full_hot(d)
        res = build("btc", "8h", 0, 8 * 3600_000,
                    {"closes_7d": CLOSES_7D, "market": None}, hot, BLOCKS_RR)
    s = res["snapshot"]
    # Deribit-scoped: 7422.5 BTC × $60,468 ≈ $448.9M — NOT the old $9.8T, and NOT
    # the $12M block-tape figure (blocks are a different, multi-venue universe).
    check("volume_usd_m ~ 449 (hot, Deribit-scoped)", 440 <= s["volume_usd_m"] <= 460, s["volume_usd_m"])
    check("volume unaffected by block tape", s["volume_usd_m"] != 12, s["volume_usd_m"])
    check("volume not trillions", s["volume_usd_m"] < 100_000, s["volume_usd_m"])
    # P/C trade-count-based across all venues: 11808 puts / 13188 calls = 0.90.
    check("pc ratio 0.90 (by trades, all venues)", s["pc_ratio"] == 0.90, s["pc_ratio"])
    check("call-tilt (0.90 is a lean, not dominance)", s["pc_descriptor"] == "call-tilt",
          s["pc_descriptor"])
    # Multi-venue activity present; split sums ~100%, Bybit leads by trade count.
    check("activity_trades 24996", s["activity_trades"] == 24996, s["activity_trades"])
    check("activity split ~100%", abs(sum(v["pct"] for v in s["activity_split"]) - 100) <= 2,
          s["activity_split"])
    check("bybit leads activity", s["activity_split"][0]["venue"] == "Bybit", s["activity_split"])


def test_volume_na_without_hot():
    # No hot CSVs → no volume_btc → Volume reads n/a (it's S3/hot-only now; there is
    # no live-API fallback for the $ figure anymore).
    recap.WARNINGS.clear()
    with tempfile.TemporaryDirectory() as d:
        hot = load_hot(d, "BTC")  # empty dir → no hot volume_btc
    res = build("btc", "8h", 0, 8 * 3600_000,
                {"closes_7d": CLOSES_7D, "market": None}, hot, BLOCKS_RR)
    check("volume_usd_m None without hot", res["snapshot"]["volume_usd_m"] is None,
          res["snapshot"]["volume_usd_m"])
    md = render_md(res)
    vol_line = next(ln for ln in md.splitlines() if ln.strip().startswith("Volume"))
    check("Volume line reads n/a", "n/a" in vol_line, vol_line)


def test_activity_split_collapses_deribit_venues():
    # deribit + deribit-usdc share the "Deribit" display label, so the Activity line
    # must show ONE "Deribit" entry whose share includes BOTH venues' trades.
    with tempfile.TemporaryDirectory() as d:
        hot = _full_hot(d)
        res = build("btc", "8h", 0, 8 * 3600_000,
                    {"closes_7d": CLOSES_7D, "trades": [], "market": None}, hot)
    s = res["snapshot"]
    split = s["activity_split"]
    deribit_entries = [v for v in split if v["venue"] == "Deribit"]
    check("exactly one Deribit entry in split", len(deribit_entries) == 1, split)
    # Combined Deribit share = (4496 + 133) / 24996 = 18.5% → 19; NOT 4496/24996 → 18.
    check("deribit-usdc folded into Deribit share (19%, not 18%)",
          deribit_entries[0]["pct"] == round(100 * (4496 + 133) / 24996), deribit_entries[0])
    check("deribit-usdc raises the Deribit share",
          deribit_entries[0]["pct"] > round(100 * 4496 / 24996), deribit_entries[0])
    # And in the rendered line the token "Deribit" appears exactly once.
    md = render_md(res)
    activity_line = next(ln for ln in md.splitlines() if ln.strip().startswith("Activity"))
    check("rendered Activity line has one 'Deribit'", activity_line.count("Deribit") == 1, activity_line)
    check("no raw 'deribit-usdc' leaks into render", "deribit-usdc" not in md, activity_line)


def test_asset_guard_drops_foreign_rows():
    # Regression: a shared-tmp race once put an ETH Snapshot slice inside a BTC
    # recap (exit 0, no warning). The CSVs now echo `asset`; rows for a
    # different asset must be dropped with a loud warning so the field degrades
    # to null → Deribit fallback instead of rendering the wrong market.
    recap.WARNINGS.clear()
    eth_dvol_spot = (
        "asset,exchange,metric,open,close,high,low\n"
        "ETH,deribit,dvol,49.4,48.1,49.5,48.0\n"
        "ETH,deribit,spot,1877,1874,1880,1864\n"
    )
    eth_volume = (
        "asset,exchange,optionType,volume_sum,notional,buy_volume,sell_volume,trade_count\n"
        "ETH,deribit,call,10000,1,5000,5000,200\n"
    )
    with tempfile.TemporaryDirectory() as d:
        _write(d, "dvol_spot.csv", eth_dvol_spot)
        _write(d, "volume.csv", eth_volume)
        hot = load_hot(d, "BTC")
    check("foreign dvol dropped", hot["dvol"] is None, hot["dvol"])
    check("foreign spot dropped", hot["spot_close"] is None, hot["spot_close"])
    check("foreign volume dropped", hot["volume_btc"] is None, hot["volume_btc"])
    check("contamination warned", any("contamination" in w for w in recap.WARNINGS),
          recap.WARNINGS)
    # Correct-asset rows with the new column still load normally.
    recap.WARNINGS.clear()
    btc_rows = eth_dvol_spot.replace("ETH,", "BTC,")
    with tempfile.TemporaryDirectory() as d:
        _write(d, "dvol_spot.csv", btc_rows)
        hot2 = load_hot(d, "BTC")
    check("own-asset rows kept", hot2["dvol"] == 48.1, hot2["dvol"])
    # Asset-column-free CSVs (older fixtures) pass through untouched.
    with tempfile.TemporaryDirectory() as d:
        hot3 = _full_hot(d)
    check("legacy CSVs without asset column still load", hot3["dvol"] == 43.34, hot3["dvol"])


def test_surface_tickers_asset_guard():
    recap.WARNINGS.clear()
    mixed = (
        "symbol,mark_iv,delta\n"
        "BTC-3JUL26-60000-C,45.0,0.50\n"
        "ETH-3JUL26-1900-C,52.0,0.50\n"
    )
    with tempfile.TemporaryDirectory() as d:
        _write(d, "surface_now.csv", mixed)
        t = _load_surface_tickers(d, "surface_now.csv", "BTC")
    check("own-asset symbol kept", "BTC-3JUL26-60000-C" in t, t)
    check("foreign symbol dropped", "ETH-3JUL26-1900-C" not in t, t)
    check("surface contamination warned",
          any("contamination" in w for w in recap.WARNINGS), recap.WARNINGS)
    # Without an asset arg (legacy callers) nothing is filtered.
    with tempfile.TemporaryDirectory() as d:
        _write(d, "surface_now.csv", mixed)
        t2 = _load_surface_tickers(d, "surface_now.csv")
    check("no filter without asset arg", len(t2) == 2, t2)


def test_strike_label_precision():
    from vol_math import _tape_strike_label as _strike_label
    check("10K+ clean thousands abbreviate", _strike_label("68000") == "68K")
    check("10K+ half-thousands keep precision", _strike_label("62500") == "62.5K")
    # Regression: ETH strikes 1825/1875/1925 all rendered "1K", so an iron
    # fly read as buying and selling the same strike.
    check("sub-10K strikes stay raw", _strike_label("1875") == "1875")
    check("1825 != 1875 != 1925 labels",
          len({_strike_label(s) for s in ("1825", "1875", "1925")}) == 3)
    # Regression: 2000 rendered "2K" beside raw 1875/2100 in one ETH table —
    # the threshold is magnitude (>=10K), not clean divisibility.
    check("sub-10K clean thousands stay raw too", _strike_label("2000") == "2000")
    check("boundary: 10000 abbreviates", _strike_label("10000") == "10K")
    check("non-numeric passes through", _strike_label("X") == "X")


def test_venue_label_degrades_for_unknown_venue():
    # A future venue absent from _VENUE_LABELS must degrade to a readable, non-empty
    # label without crashing and without colliding with a mapped label.
    check("known: okex-options → OKX", _venue_label("okex-options") == "OKX")
    check("known: deribit-usdc → Deribit", _venue_label("deribit-usdc") == "Deribit")
    check("unknown: cme-options → Cme", _venue_label("cme-options") == "Cme")
    check("unknown single token: kraken → Kraken", _venue_label("kraken") == "Kraken")
    check("empty/None degrades to '?' not crash", _venue_label("") == "?" and _venue_label(None) == "?")
    check("unknown label != any mapped label",
          _venue_label("cme-options") not in ("Deribit", "OKX", "Bybit", "Bullish"))


# ── load_hot: dvol/spot + surface parsing ───────────────────────────────────

def test_dvol_spot_parsing():
    with tempfile.TemporaryDirectory() as d:
        hot = _full_hot(d)
    check("dvol close", hot["dvol"] == 43.34, hot["dvol"])
    check("dvol open", hot["dvol_open"] == 45.41, hot["dvol_open"])
    check("spot close", hot["spot_close"] == 60468, hot["spot_close"])
    check("spot open", hot["spot_open"] == 59362, hot["spot_open"])


def test_dvol_spot_prefers_deribit_over_future_venue():
    # Hardening: DVOL/spot are Deribit-only today. If a future venue ever emits
    # dvol/spot rows too, the Deribit row must still win deterministically — a naive
    # last-row-wins loop would let whichever row sorts last (here deribit-usdc,
    # listed AFTER deribit) silently override the canonical Deribit figure.
    recap.WARNINGS.clear()
    contaminated = (
        "exchange,metric,open,close,high,low\n"
        "deribit,dvol,45.41,43.34,45.5,43.0\n"
        "deribit,spot,59362,60468,60500,59000\n"
        "deribit-usdc,dvol,90.0,91.0,92.0,89.0\n"      # bogus alt-venue rows, listed last
        "deribit-usdc,spot,1.0,1.01,1.02,0.99\n"
    )
    with tempfile.TemporaryDirectory() as d:
        _write(d, "volume.csv", CORRUPT_VOLUME_CSV)
        _write(d, "dvol_spot.csv", contaminated)
        hot = load_hot(d, "BTC")
    check("dvol from deribit, not deribit-usdc", hot["dvol"] == 43.34, hot["dvol"])
    check("dvol_open from deribit", hot["dvol_open"] == 45.41, hot["dvol_open"])
    check("spot from deribit, not deribit-usdc", hot["spot_close"] == 60468, hot["spot_close"])
    check("spot_open from deribit", hot["spot_open"] == 59362, hot["spot_open"])
    # Degrade gracefully: with ONLY a non-deribit row present, still read it (no crash/blank).
    only_alt = ("exchange,metric,open,close,high,low\n"
                "okex-options,spot,100,110,120,90\n")
    with tempfile.TemporaryDirectory() as d:
        _write(d, "dvol_spot.csv", only_alt)
        hot2 = load_hot(d, "BTC")
    check("falls back to sole non-deribit spot row", hot2["spot_close"] == 110, hot2["spot_close"])


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


# ── Block flow: derived from the multi-venue Paradigm tape (blocks.csv) ─────

def _block_flow(block_rows):
    """Build via the live path (build → build_tape_blocks) with empty hot, so the
    block section is exercised end-to-end. Returns (block_flow, biggest_print)."""
    res = build("btc", "8h", 0, 8 * 3600_000,
                {"closes_7d": CLOSES_7D, "market": None}, {}, block_rows)
    return res["block_flow"], res["biggest_print"]


def test_block_flow_from_tape():
    bf, bp = _block_flow(BLOCKS_RR)
    check("n_blocks from tape = 1", bf["n_blocks"] == 1, bf["n_blocks"])
    # Σ per-leg NOTIONAL_VOLUME_USD = $6M + $6M = $12M.
    check("total_m = 12.0 (Σ per-leg notional)", bf["total_m"] == 12.0, bf["total_m"])
    check("biggest expiry", bp["expiry"] == "26JUN26", bp)
    check("biggest is Risk Reversal (classified from per-leg rows)",
          bp["structure"] == "Risk Reversal", bp)
    # Size is the structure UNIT (min per-leg QTY = 100), not a leg-sum.
    check("biggest size 100 (unit, not leg-sum)", bp["size"] == 100, bp)
    check("biggest notional 12.0M", bp["notional_m"] == 12.0, bp)
    check("biggest is mixed-direction (buy + sell legs)", bp["side"] == "Mixed", bp)
    check("biggest venue Deribit", bp["venue"] == "Deribit", bp)


def test_block_flow_multi_venue_and_notional_floor():
    # Blocks span venues (Deribit/Paradex/Bullish); a sub-floor dust block is dropped.
    rows = [
        _blk("Straddle 19 Nov 25 90000", "BUY", 8_000_000, bid="bST", rfq="rST",
             prod="BTC OPTION - BLSH"),
        _blk("RRCall 30 Jan 26 70000/108000", "BUY", 3_000_000, bid="bRR", rfq="rRR",
             prod="BTC OPTION - PRDX"),
        _blk("Call 26 Dec 25 100000", "BUY", 50_000, bid="bDust", rfq="rDust"),  # < floor
    ]
    bf, bp = _block_flow(rows)
    check("dust block below $250k floor dropped", bf["n_blocks"] == 2, bf["n_blocks"])
    check("biggest is the $8M Bullish straddle", bp["notional_m"] == 8.0 and bp["venue"] == "Bullish", bp)
    venues = {r["venue"] for r in bf["rows"]}
    check("row venues include Paradex + Bullish", {"Paradex", "Bullish"} <= venues, venues)
    md = render_md({"header": {"asset": "BTC", "window": "8h", "start_utc": "01:00",
                               "end_utc": "09:00"},
                    "snapshot": {}, "biggest_print": bp, "block_flow": bf,
                    "vol_surface": None, "hot_horizon": None, "warnings": []})
    check("Venue column rendered", "Venue" in md and "Paradex" in md and "Bullish" in md, md)
    check("biggest print names the venue", "via Paradigm/Bullish" in md, md)


def test_pc_descriptor_bands():
    # Reciprocal-symmetric bands: 1.05x is near-neutral, not "puts dominant".
    check("1.05 → balanced", pc_descriptor(1.05) == "balanced", pc_descriptor(1.05))
    check("0.95 → balanced", pc_descriptor(0.95) == "balanced", pc_descriptor(0.95))
    check("1.15 → put-tilt", pc_descriptor(1.15) == "put-tilt", pc_descriptor(1.15))
    check("1.30 → puts dominant", pc_descriptor(1.30) == "puts dominant",
          pc_descriptor(1.30))
    check("0.90 → call-tilt", pc_descriptor(0.90) == "call-tilt", pc_descriptor(0.90))
    check("0.57 → calls dominant", pc_descriptor(0.57) == "calls dominant",
          pc_descriptor(0.57))
    check("None → None", pc_descriptor(None) is None)


def test_block_flow_caps_rows_at_top_n():
    # 10 distinct straddles (unique strikes/expiries → distinct worked orders, no
    # clip merging) at descending notional: rows cap at 8; header counts all 10
    # blocks and 10 structures, and the render discloses the truncation.
    rows = []
    for i in range(10):
        rows.append(_blk(f"Straddle 26 Jun 26 {50000 + i * 1000}", "BUY",
                         (10 - i) * 1_000_000, bid=f"B{i}", rfq=f"R{i}"))
    bf, bp = _block_flow(rows)
    check("8 rows shown", len(bf["rows"]) == 8, len(bf["rows"]))
    check("header counts all 10 blocks", bf["n_blocks"] == 10, bf["n_blocks"])
    check("10 structures", bf["n_structures"] == 10, bf["n_structures"])
    md = render_md({"header": {"asset": "BTC", "window": "1h", "start_utc": "01:00",
                               "end_utc": "02:00"},
                    "snapshot": {}, "biggest_print": bp,
                    "block_flow": bf, "vol_surface": None, "hot_horizon": None,
                    "warnings": []})
    check("header shows both granularities", "10 blocks / 10 structures" in md, md)
    check("truncation disclosed in header", "(top 8 by notional)" in md, md)


def test_block_flow_aggregates_clips_by_rfq():
    # 5 clips of one worked order (same RFQ_ID, distinct BLOCK_TRADE_IDs) → one
    # structure row whose Blocks count carries the 5 prints; header states both
    # granularities. The biggest print is still ONE block, not the rolled-up order.
    rows = []
    for i in range(5):
        rows.append(_blk("PSpd 31 Jul 26 64000/60000", "BUY", 2_000_000 + i,
                         bid=f"K{i}", rfq="RWORK"))
    bf, bp = _block_flow(rows)
    check("one structure row", len(bf["rows"]) == 1, bf["rows"])
    check("header counts 5 raw blocks", bf["n_blocks"] == 5, bf["n_blocks"])
    check("one structure", bf["n_structures"] == 1, bf["n_structures"])
    row = bf["rows"][0]
    check("blocks count on row (RFQ rollup)", row["blocks"] == 5, row)
    check("row is the put spread", "Put Spread" in row["structure"], row["structure"])
    # Biggest print = the single largest block (~$2M), NOT the $10M rolled-up order.
    check("biggest print is one block, not the RFQ aggregate", bp["notional_m"] == 2.0, bp)
    md = render_md({"header": {"asset": "BTC", "window": "1h", "start_utc": "01:00",
                               "end_utc": "02:00"},
                    "snapshot": {}, "biggest_print": bp,
                    "block_flow": bf, "vol_surface": None, "hot_horizon": None,
                    "warnings": []})
    check("header: 5 blocks / 1 structure", "5 blocks / 1 structure" in md, md)
    check("no truncation suffix when all shown", "top 8 by notional" not in md, md)


def test_block_flow_column_stretches_for_long_labels():
    # Regression: typed cross-expiry labels ("24JUL26/31JUL26 Call Diagonal")
    # overflowed a fixed Structure column. The column stretches to the longest
    # label and the Notl column header stays aligned with the rows — now with a
    # Venue column between Structure and Notl.
    rows = [
        _blk("Call 24 Jul 26 68000", "BUY", 21_000_000, bid="D1", rfq="RD", tid="t1"),
        _blk("Call 31 Jul 26 71000", "SELL", 21_000_000, bid="D1", rfq="RD", tid="t2"),
    ]
    bf, bp = _block_flow(rows)
    md = render_md({"header": {"asset": "BTC", "window": "1h", "start_utc": "01:00",
                               "end_utc": "02:00"},
                    "snapshot": {}, "biggest_print": bp,
                    "block_flow": bf, "vol_surface": None, "hot_horizon": None,
                    "warnings": []})
    lines = md.splitlines()
    header = next(l for l in lines if l.startswith("#") and "Structure" in l)
    row = next(l for l in lines if "Call Diagonal" in l and l.split()[0].isdigit())
    check("Call Diagonal label present", "Call Diagonal" in row, row)
    check("Notl column aligned with row", row.index("$") == header.index("Notl"),
          (header, row))


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
                 {"closes_7d": CLOSES_7D, "market": None}, hot, BLOCKS_RR)


def test_render_four_sections():
    md = render_md(_full_result())
    for h in ("**Snapshot**", "**Biggest Print", "**Block Flow", "**Vol Surface**"):
        check(f"render has {h}", h in md, md[:80])
    check("render title", md.startswith("**BTC Options · 8h Recap"), md[:40])
    # Volume is hot-only and Deribit-scoped: 7422.5 BTC × $60,468 ≈ $449M. The block
    # tape ($12M, a different multi-venue universe) must NOT drive this line.
    check("render volume line (hot, Deribit-scoped)",
          "Volume" in md and "$449M" in md and "$12M" not in md and "Deribit only" in md,
          "volume render")
    check("render multi-venue Activity line", "Activity" in md and "Bybit" in md, "activity render")
    check("render P/C 0.9x", "0.9x" in md)
    check("render biggest Risk Reversal (from tape)", "26JUN26 Risk Reversal" in md)
    check("biggest print via Paradigm/Deribit", "via Paradigm/Deribit" in md, md)
    # Vol-surface delta columns are always present in the header; with no
    # window-open surface (this fixture has none) the delta cells read n/a.
    check("delta columns present", "ΔATM" in md and "ΔRR" in md and "ΔFly" in md, md)
    check("deltas n/a without open", "45.0v" in md and "n/a" in md, "expected n/a deltas")
    # Dropped/forbidden output must not reappear.
    check("no Themes", "Themes" not in md)
    check("no Dealer positioning", "Dealer positioning" not in md)
    check("four yaml fences", md.count("```yaml") == 4, md.count("```yaml"))


def test_render_vrp_deadband_matches_rv_line():
    # A small positive VRP (0.5v) is inside the ±1v dead-band: the RV line must
    # read "IN LINE" and the VRP line "roughly fair" — never "IN LINE" beside
    # "overpriced" on adjacent lines (the contradiction this fix removes).
    md = render_md({"header": {"asset": "BTC", "window": "1h", "start_utc": "01:00",
                               "end_utc": "02:00"},
                    "snapshot": {"vrp": 0.5, "rv_7d": 45.0, "dvol": 45.5},
                    "biggest_print": None,
                    "block_flow": {"rows": [], "n_blocks": 0, "n_structures": 0,
                                   "total_m": 0, "truncated": False},
                    "vol_surface": None, "hot_horizon": None, "warnings": []})
    rv_line = next(l for l in md.splitlines() if l.startswith("RV 7d"))
    vrp_line = next(l for l in md.splitlines() if l.startswith("VRP"))
    check("small +VRP → RV line IN LINE", "IN LINE" in rv_line, rv_line)
    check("small +VRP → VRP line roughly fair", "roughly fair" in vrp_line, vrp_line)
    check("small +VRP not called overpriced", "overpriced" not in vrp_line, vrp_line)
    # Outside the band the words still flip.
    md2 = render_md({"header": {"asset": "BTC", "window": "1h", "start_utc": "01:00",
                                "end_utc": "02:00"},
                     "snapshot": {"vrp": 3.0, "rv_7d": 42.0, "dvol": 45.0},
                     "biggest_print": None,
                     "block_flow": {"rows": [], "n_blocks": 0, "n_structures": 0,
                                    "total_m": 0, "truncated": False},
                     "vol_surface": None, "hot_horizon": None, "warnings": []})
    vrp_line2 = next(l for l in md2.splitlines() if l.startswith("VRP"))
    check("VRP > 1 → overpriced", "overpriced" in vrp_line2, vrp_line2)


def test_render_deterministic():
    r = _full_result()
    check("render is deterministic", render_md(r) == render_md(r))


def test_render_activity_na_when_missing():
    # Thin/empty windows must still render the Activity line (as n/a), not drop
    # it — a live 30m recap omitted the row while Volume/P-C showed n/a.
    recap.WARNINGS.clear()
    with tempfile.TemporaryDirectory() as d:
        hot = load_hot(d, "BTC")  # empty dir → no activity
    res = build("btc", "30m", 0, 1800_000,
                {"closes_7d": [], "trades": [], "market": None}, hot)
    md = render_md(res)
    activity_lines = [ln for ln in md.splitlines() if ln.strip().startswith("Activity")]
    check("Activity line present when empty", len(activity_lines) == 1, md[:400])
    check("Activity reads n/a", "n/a" in activity_lines[0], activity_lines)


def test_render_singular_block():
    # "1 block / 1 structure" → both words pluralize independently.
    rows = [_blk("Call 31 Jul 26 68000", "BUY", 3_000_000, bid="B1", rfq="R1")]
    bf, bp = _block_flow(rows)
    md = render_md({"header": {"asset": "BTC", "window": "1h", "start_utc": "01:00",
                               "end_utc": "02:00"},
                    "snapshot": {}, "biggest_print": bp,
                    "block_flow": bf, "vol_surface": None, "hot_horizon": None,
                    "warnings": []})
    check("singular: 1 block / 1 structure", "1 block / 1 structure" in md, md)
    check("no '1 blocks'", "1 blocks" not in md, md)


def test_beyond_24h_prefers_market_ohlc():
    # The rolling hot file retains ~24h, so for a >24h window its OHLC silently
    # under-covers (a live 2d recap quoted the 24h low as the 48h low). With a
    # Deribit market series present, build() must prefer it for DVOL/spot.
    recap.WARNINGS.clear()
    end = 100 * 24 * 3600_000
    mkt = {"dvol": [[0, 40.0, 41.0, 39.0, 40.5], [1, 40.5, 42.0, 38.5, 39.0]],
           "spot": {"open": [61000.0, 62000.0], "close": [62000.0, 63000.0],
                    "low": [60500.0, 61500.0]},
           "spot_now": 63000.0, "tickers": {}}
    with tempfile.TemporaryDirectory() as d:
        hot = _full_hot(d)  # hot says spot low 59000, dvol 43.34 — 24h-scoped
    res = build("btc", "48h", end - 48 * 3600_000, end,
                {"closes_7d": CLOSES_7D, "trades": [], "market": mkt}, hot)
    s = res["snapshot"]
    check("48h spot low from market, not hot", s["spot_low"] == 60500, s["spot_low"])
    check("48h spot from market", s["spot"] == 63000, s["spot"])
    check("48h spot_from = full-window open", s["spot_from"] == 61000, s["spot_from"])
    check("48h dvol from market", s["dvol"] == 39.0, s["dvol"])
    check("48h dvol_open from market", round(s["dvol_open"], 1) == 40.0, s["dvol_open"])
    # Within 24h, hot stays authoritative even when a market series exists.
    res8 = build("btc", "8h", end - 8 * 3600_000, end,
                 {"closes_7d": CLOSES_7D, "trades": [], "market": mkt}, hot)
    check("8h keeps hot spot", res8["snapshot"]["spot"] == 60468, res8["snapshot"]["spot"])
    check("8h keeps hot dvol", res8["snapshot"]["dvol"] == 43.3, res8["snapshot"]["dvol"])


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


def test_hot_horizon_banner_beyond_24h():
    # A >24h window: Volume/Activity/P-C/DVOL/spot come from the ~24h hot rollup, so
    # they under-cover; Block Flow (Paradigm tape) and the surface span the full
    # window. Banner discloses that; header still says 72h.
    recap.WARNINGS.clear()
    end = 100 * 3600_000
    start = end - 72 * 3600_000          # 72h window
    with tempfile.TemporaryDirectory() as d:
        hot = _full_hot(d)
    res = build("btc", "72h", start, end,
                {"closes_7d": CLOSES_7D, "market": None}, hot, BLOCKS_RR)
    check("hot_horizon set to 72", res["hot_horizon"] == 72, res["hot_horizon"])
    md = render_md(res)
    check("banner rendered", "hot-rollup horizon" in md, md[:200])
    check("banner names full window", "full 72h" in md, md[:200])
    check("banner scopes to hot sections, not Block Flow",
          "Block Flow and surface span" in md, md[:200])


def test_no_hot_horizon_banner_within_24h():
    # An 8h window → no banner (hot rollup covers it).
    recap.WARNINGS.clear()
    end = 100 * 3600_000
    with tempfile.TemporaryDirectory() as d:
        hot = _full_hot(d)
    res = build("btc", "8h", end - 8 * 3600_000, end,
                {"closes_7d": CLOSES_7D, "market": None}, hot, BLOCKS_RR)
    check("no hot_horizon within 24h", res["hot_horizon"] is None, res["hot_horizon"])
    check("no banner in md", "hot-rollup horizon" not in render_md(res))


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
