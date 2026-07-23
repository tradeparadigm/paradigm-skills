#!/usr/bin/env python3
"""
Unit tests for vol_math.py — no network, no auth, no deps.

Run: python3 tests/test_vol_math.py
These pin the formulas so the production CLI and the eval fixture generator
can't drift, and so a human can verify the math once by inspection.
"""

import math
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
from vol_math import (
    compute_realized_vol,
    realized_vs_implied,
    black76_greeks,
    expiry_ms_from_instrument,
    compute_flow_greeks,
    cluster_blocks,
    compute_vol_surface,
    classify_structure,
    dominant_side,
    summarize_blocks,
    aggregate_clips,
    clip_signature,
    HOURS_PER_YEAR,
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


def approx(a, b, tol=1e-6):
    return a is not None and b is not None and abs(a - b) <= tol


# ── Realized vol ───────────────────────────────────────────────────────────

def test_rv_flat_series_is_zero():
    rv = compute_realized_vol([100.0] * 10)
    check("flat series → 0 vol", approx(rv["annualized_vol"], 0.0, 1e-9),
          f"got {rv['annualized_vol']}")


def test_rv_too_few_points():
    rv = compute_realized_vol([100.0, 101.0])
    check("under 3 points → None", rv["annualized_vol"] is None, f"got {rv}")
    check("empty → None", compute_realized_vol([])["annualized_vol"] is None)


def test_rv_known_value():
    # Alternating +1%/-1% each hour: every log-return has equal magnitude.
    closes = [100.0]
    for i in range(1, 50):
        closes.append(closes[-1] * (1.01 if i % 2 else 1 / 1.01))
    rv = compute_realized_vol(closes)
    # Reconstruct expected: sample stdev of the log returns × √8760 × 100.
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    n = len(rets)
    mean = sum(rets) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (n - 1))
    expected = sd * math.sqrt(HOURS_PER_YEAR) * 100
    check("known series matches formula", approx(rv["annualized_vol"], round(expected, 1), 0.05),
          f"got {rv['annualized_vol']} vs {round(expected, 1)}")


def test_rv_annualization_factor():
    check("8760 hours/year (24/7)", HOURS_PER_YEAR == 8760)


def test_vrp_labels():
    # rv ~0 vs dvol 50 → implied very rich
    rich = realized_vs_implied([100.0] * 10, 50.0)
    check("VRP rich label", "rich" in (rich["vrp_label"] or ""), rich)
    # Build a high-realized series, low implied → cheap
    closes = [100.0]
    for i in range(1, 200):
        closes.append(closes[-1] * (1.02 if i % 2 else 1 / 1.02))
    cheap = realized_vs_implied(closes, 5.0)
    check("VRP cheap label", "cheap" in (cheap["vrp_label"] or ""), cheap)
    check("VRP sign = dvol - rv", approx(cheap["vrp"], round(5.0 - cheap["value"], 1), 0.11),
          f"vrp {cheap['vrp']} value {cheap['value']}")


# ── Expiry parsing ─────────────────────────────────────────────────────────

def test_expiry_parsing():
    from datetime import datetime, timezone
    one = expiry_ms_from_instrument("BTC-26JUN26-55000-P")
    expect = int(datetime(2026, 6, 26, 8, 0, tzinfo=timezone.utc).timestamp() * 1000)
    check("26JUN26 parses to 08:00 UTC", one == expect, f"got {one} vs {expect}")
    # single-digit day
    short = expiry_ms_from_instrument("BTC-5JUN26-60000-C")
    expect2 = int(datetime(2026, 6, 5, 8, 0, tzinfo=timezone.utc).timestamp() * 1000)
    check("5JUN26 (single-digit day) parses", short == expect2, f"got {short} vs {expect2}")
    check("garbage → None", expiry_ms_from_instrument("not-an-instrument") is None)


# ── Black-76 greeks ────────────────────────────────────────────────────────

def test_black76_positivity():
    g = black76_greeks(F=60000, K=60000, T_years=0.05, iv_pct=70)
    check("vega positive", g["vega"] > 0, g)
    check("dollar_gamma positive", g["dollar_gamma"] > 0, g)


def test_black76_degenerate():
    check("T=0 → zero greeks", black76_greeks(60000, 60000, 0, 70)["vega"] == 0)
    check("sigma=0 → zero greeks", black76_greeks(60000, 60000, 0.1, 0)["vega"] == 0)


def test_black76_vega_increases_with_tenor():
    near = black76_greeks(60000, 60000, 0.02, 70)["vega"]
    far = black76_greeks(60000, 60000, 0.50, 70)["vega"]
    check("longer tenor → more vega", far > near, f"near {near:.1f} far {far:.1f}")


def test_black76_atm_has_most_gamma():
    atm = black76_greeks(60000, 60000, 0.1, 70)["dollar_gamma"]
    otm = black76_greeks(60000, 80000, 0.1, 70)["dollar_gamma"]
    check("ATM gamma > far-OTM gamma", atm > otm, f"atm {atm:.0f} otm {otm:.0f}")


# ── Flow greeks / dealer positioning ───────────────────────────────────────

def _leg(inst, direction, amount, F=62000, iv=70.0, ts=1748000000000, bid="B1"):
    return {"instrument_name": inst, "index_price": F, "iv": iv,
            "timestamp": ts, "direction": direction, "amount": amount,
            "block_trade_id": bid}


def test_customer_buying_makes_dealers_short():
    # Customers buy a put → long vega/gamma → dealers short both.
    trades = [_leg("BTC-26JUN26-60000-P", "buy", 100)]
    fg = compute_flow_greeks(cluster_blocks(trades))
    check("net customer vega > 0 (bought)", fg["net_customer_vega"] > 0, fg)
    check("dealer vega < 0 (short)", fg["dealer_vega"] < 0, fg)
    check("dealer short gamma label", "short gamma" in fg["positioning_label"], fg)


def test_customer_selling_makes_dealers_long():
    trades = [_leg("BTC-26JUN26-60000-C", "sell", 100)]
    fg = compute_flow_greeks(cluster_blocks(trades))
    check("net customer vega < 0 (sold)", fg["net_customer_vega"] < 0, fg)
    check("dealer long gamma label", "long gamma" in fg["positioning_label"], fg)


def test_dealer_is_opposite_of_customer():
    trades = [_leg("BTC-26JUN26-60000-P", "buy", 100)]
    fg = compute_flow_greeks(cluster_blocks(trades))
    check("dealer vega = -customer vega",
          fg["dealer_vega"] == -fg["net_customer_vega"], fg)
    check("dealer gamma = -customer gamma",
          fg["dealer_dollar_gamma"] == -fg["net_customer_dollar_gamma"], fg)


def test_balanced_two_way():
    # Same instrument bought and sold in equal size → net ≈ 0 vs gross → balanced.
    trades = [_leg("BTC-26JUN26-60000-C", "buy", 100, bid="B1"),
              _leg("BTC-26JUN26-60000-C", "sell", 100, bid="B2")]
    fg = compute_flow_greeks(cluster_blocks(trades))
    check("offsetting flow → balanced", fg["balanced"], fg)
    check("balanced label", "two-way" in fg["positioning_label"], fg)


def test_cluster_blocks_filters_screen():
    trades = [
        _leg("BTC-26JUN26-60000-P", "buy", 100, bid="B1"),
        {"instrument_name": "BTC-26JUN26-60000-C", "direction": "buy", "amount": 1,
         "index_price": 62000, "iv": 70, "timestamp": 1748000000000},  # no block_trade_id
    ]
    clusters = cluster_blocks(trades)
    check("screen trade excluded from clusters", len(clusters) == 1, clusters)


# ── Block structures / summary (#2b) ────────────────────────────────────────

def test_classify_structure():
    put = [_leg("BTC-26JUN26-60000-P", "buy", 10)]
    check("single put → Put", classify_structure(put) == "Put", classify_structure(put))
    rr = [_leg("BTC-26JUN26-55000-P", "buy", 100),
          _leg("BTC-26JUN26-68000-C", "sell", 100)]
    check("P+C diff strikes → Strangle/RR", classify_structure(rr) == "Strangle/RR",
          classify_structure(rr))
    straddle = [_leg("BTC-26JUN26-60000-P", "buy", 10),
                _leg("BTC-26JUN26-60000-C", "buy", 10)]
    check("P+C same strike → Straddle", classify_structure(straddle) == "Straddle",
          classify_structure(straddle))
    spread = [_leg("BTC-26JUN26-60000-P", "buy", 10),
              _leg("BTC-26JUN26-55000-P", "sell", 10)]
    check("both puts diff strikes → Put Spread",
          classify_structure(spread) == "Put Spread", classify_structure(spread))
    call_spread = [_leg("BTC-26JUN26-60000-C", "buy", 10),
                   _leg("BTC-26JUN26-65000-C", "sell", 10)]
    check("both calls diff strikes → Call Spread",
          classify_structure(call_spread) == "Call Spread",
          classify_structure(call_spread))
    cal = [_leg("BTC-26JUN26-60000-C", "buy", 10),
           _leg("BTC-3JUL26-60000-C", "sell", 10)]
    check("diff expiries same strike → Calendar", classify_structure(cal) == "Calendar",
          classify_structure(cal))
    # Short straddle + long wings = iron fly. The ≥3-leg check must beat the
    # C&P branch — this printed as "Strangle/RR" in a live ETH recap.
    ironfly = [_leg("ETH-16JUL26-1875-C", "sell", 63),
               _leg("ETH-16JUL26-1875-P", "sell", 63),
               _leg("ETH-16JUL26-1925-C", "buy", 63),
               _leg("ETH-16JUL26-1825-P", "buy", 63)]
    check("4-leg iron fly → Butterfly/Condor",
          classify_structure(ironfly) == "Butterfly/Condor", classify_structure(ironfly))
    diag = [_leg("ETH-24JUL26-1900-C", "buy", 6400),
            _leg("ETH-28AUG26-2100-C", "sell", 6400)]
    check("2 legs diff expiry+strike → Diagonal", classify_structure(diag) == "Diagonal",
          classify_structure(diag))


def test_dominant_side():
    buys = [_leg("BTC-26JUN26-60000-P", "buy", 10),
            _leg("BTC-26JUN26-55000-P", "buy", 10)]
    check("all buys → Buy", dominant_side(buys) == "Buy", dominant_side(buys))
    sells = [_leg("BTC-26JUN26-60000-C", "sell", 10)]
    check("all sells → Sell", dominant_side(sells) == "Sell", dominant_side(sells))
    mixed = [_leg("BTC-26JUN26-55000-P", "buy", 100),
             _leg("BTC-26JUN26-68000-C", "sell", 100)]
    check("buy + sell → Mixed", dominant_side(mixed) == "Mixed", dominant_side(mixed))


def test_summarize_blocks_ranks_and_describes():
    trades = [
        # big RR: 200 BTC @ ~62k → ~$12.4M notional
        _leg("BTC-26JUN26-55000-P", "buy", 100, F=62000, bid="RR"),
        _leg("BTC-26JUN26-68000-C", "sell", 100, F=62000, bid="RR"),
        # small outright: 20 BTC → ~$1.24M
        _leg("BTC-12JUN26-59000-P", "buy", 20, F=62000, bid="P1"),
        # below the 10-BTC floor → filtered out
        _leg("BTC-5JUN26-70000-C", "buy", 1, F=62000, bid="TINY"),
    ]
    blocks = summarize_blocks(cluster_blocks(trades))
    check("two blocks survive the 10-BTC floor", len(blocks) == 2, blocks)
    top = blocks[0]
    check("largest by notional first (the RR)", top["block_trade_id"] == "RR", top)
    check("largest size is 200 BTC", top["size_btc"] == 200.0, top)
    check("unit size is 100 (per-leg, not leg-sum)", top["unit_size"] == 100.0, top)
    check("largest classified Strangle/RR", top["structure"] == "Strangle/RR", top)
    check("largest is mixed-direction", top["side"] == "Mixed", top)
    check("largest expiry 26JUN26", top["expiry"] == "26JUN26", top)
    check("notional ranks RR above outright", top["notional_usd"] > blocks[1]["notional_usd"], blocks)


def test_summarize_blocks_multi_expiry_label():
    # A cross-expiry structure names its near AND far tenor, chronologically —
    # a legs[0]-based label let identical structures render under different
    # expiries depending on tape order.
    a = [_leg("BTC-26MAR27-4200-C", "buy", 270, bid="D1"),
         _leg("BTC-25JUN27-2000-C", "buy", 270, bid="D1")]
    b = list(reversed([_leg("BTC-26MAR27-4200-C", "buy", 250, bid="D2"),
                       _leg("BTC-25JUN27-2000-C", "buy", 250, bid="D2")]))
    rows = summarize_blocks(cluster_blocks(a + b), top_n=10, min_btc=5.0)
    labels = {r["expiry"] for r in rows}
    check("2-expiry label near/far with '/'", labels == {"26MAR27/25JUN27"}, labels)
    check("same structure → same label regardless of leg order", len(labels) == 1, labels)
    # 3+ expiries: "/" would read as a complete enumeration and contradict the
    # Detail legs (a live 3-expiry package labeled "31JUL26/25SEP26" hid its
    # 28AUG26 leg) — the label becomes a "→" range instead.
    tri = [_leg("BTC-31JUL26-66000-C", "sell", 300, bid="T1"),
           _leg("BTC-28AUG26-66000-C", "buy", 300, bid="T1"),
           _leg("BTC-25SEP26-65000-C", "sell", 300, bid="T1")]
    tri_rows = summarize_blocks(cluster_blocks(tri), top_n=10, min_btc=5.0)
    check("3-expiry label is a '→' range", tri_rows[0]["expiry"] == "31JUL26→25SEP26",
          tri_rows[0]["expiry"])


def test_vrp_reconciles_with_displayed_lines():
    # VRP must equal the difference of the DISPLAY-rounded DVOL and RV, so the
    # rendered Snapshot reconciles line-to-line — live recaps showed VRP 0.1v
    # off the visible DVOL−RV arithmetic. Boundary-ish dvol values included.
    closes = [60000 + (i % 5) * 50 for i in range(60)]
    for dvol in (36.38, 48.1499, 33.2501, 51.05):
        r = realized_vs_implied(closes, dvol)
        check(f"VRP({dvol}) = displayed DVOL − displayed RV",
              r["vrp"] == round(round(dvol, 1) - r["value"], 1), r)


def test_summarize_blocks_empty():
    check("no clusters → empty list", summarize_blocks({}) == [], "expected []")


# ── Vol surface ────────────────────────────────────────────────────────────

def _surface_tickers():
    """Front expiry with a downside skew (puts richer than calls) and wide
    enough strikes to bracket the 25Δ wings; a back expiry at lower IV."""
    return {
        # 5JUN26 — ATM ~82v, 25Δ put rich (downside skew). call_delta spans 0.10–0.90.
        "BTC-5JUN26-60000-C": {"mark_iv": 96.0, "delta": 0.90},
        "BTC-5JUN26-61000-C": {"mark_iv": 90.0, "delta": 0.75},   # ~25Δ put strike (cd 0.75)
        "BTC-5JUN26-62000-C": {"mark_iv": 82.0, "delta": 0.50},   # ATM
        "BTC-5JUN26-63000-C": {"mark_iv": 80.0, "delta": 0.25},   # 25Δ call
        "BTC-5JUN26-64000-C": {"mark_iv": 84.0, "delta": 0.10},
        # 6JUN26 — lower ATM (contango if it were the back of a normal curve)
        "BTC-6JUN26-62000-C": {"mark_iv": 70.0, "delta": 0.50},
        "BTC-6JUN26-61000-C": {"mark_iv": 74.0, "delta": 0.75},
        "BTC-6JUN26-63000-C": {"mark_iv": 68.0, "delta": 0.25},
    }


def test_surface_atm_and_skew():
    s = compute_vol_surface(_surface_tickers(), spot=62000)
    front = s["expiries"][0]
    check("front expiry is 5JUN26", front["expiry"] == "5JUN26", front)
    check("front ATM ≈ 82v", approx(front["atm_iv"], 82.0, 0.6), front)
    # 25Δ call IV (80) − 25Δ put IV (90) = −10 → puts bid
    check("25Δ RR negative (puts bid)", front["rr_25d"] < 0, front)
    check("skew label says puts bid", "puts bid" in (s["skew_label"] or ""), s)
    check("front wings not extrapolated", front["wings_extrapolated"] is False, front)


def test_surface_butterfly_positive_when_wings_bid():
    # wings (80,90) average 85 > ATM 82 → fly positive
    s = compute_vol_surface(_surface_tickers(), spot=62000)
    check("fly positive (wings bid)", s["expiries"][0]["fly_25d"] > 0, s["expiries"][0])


def test_surface_term_structure_backwardation():
    s = compute_vol_surface(_surface_tickers(), spot=62000)
    check("front ATM > back ATM", s["front_atm"] > s["back_atm"], s)
    check("term = backwardation", "backwardation" in (s["term_structure"] or ""), s)


def _atm_only_tickers(exp_atms):
    """One 0.50Δ strike per expiry — enough for ATM/term, no wings."""
    return {f"BTC-{exp}-60000-C": {"mark_iv": iv, "delta": 0.50}
            for exp, iv in exp_atms}


def test_surface_term_structure_reads_whole_curve():
    # The reported bug: 33.3 → 35.6 → 35.1 → 35.2 → 33.7 rises then falls.
    # A front-vs-next comparison called this "contango"; it is humped.
    s = compute_vol_surface(_atm_only_tickers([
        ("15JUL26", 33.3), ("16JUL26", 35.6), ("17JUL26", 35.1),
        ("18JUL26", 35.2), ("24JUL26", 33.7)]), spot=60000)
    check("humped, not contango", "humped" in (s["term_structure"] or ""), s)
    check("names the peak expiry", "16JUL26" in s["term_structure"], s)
    check("back_atm is the LAST expiry", s["back_atm"] == 33.7, s)

    up = compute_vol_surface(_atm_only_tickers([
        ("15JUL26", 30.0), ("16JUL26", 31.5), ("18JUL26", 33.0)]), spot=60000)
    check("monotonic up → contango", "contango" in (up["term_structure"] or ""), up)

    # A 0.1v counter-dip is within tolerance — still contango, not humped.
    wiggle = compute_vol_surface(_atm_only_tickers([
        ("15JUL26", 30.0), ("16JUL26", 31.5), ("17JUL26", 31.4),
        ("18JUL26", 33.0)]), spot=60000)
    check("≤0.2v dip tolerated as contango",
          "contango" in (wiggle["term_structure"] or ""), wiggle)

    shallow = compute_vol_surface(_atm_only_tickers([
        ("15JUL26", 30.0), ("16JUL26", 30.4), ("18JUL26", 30.8)]), spot=60000)
    check("monotonic but <1v span → flat",
          "flat" in (shallow["term_structure"] or ""), shallow)


def test_labels_are_contract_tokens_only():
    # The skew/term labels render verbatim inside a FIXED template
    # (references/output-format.md) — no explanatory suffixes. Live recaps
    # leaked "(35.2v), non-monotonic" and ", downside skew" into the output.
    humped = compute_vol_surface(_atm_only_tickers([
        ("15JUL26", 33.3), ("16JUL26", 35.6), ("24JUL26", 33.7)]), spot=60000)
    check("humped label is exactly the contract token",
          humped["term_structure"] == "humped — peak at 16JUL26", humped["term_structure"])
    up = compute_vol_surface(_atm_only_tickers([
        ("15JUL26", 30.0), ("16JUL26", 31.5), ("18JUL26", 33.0)]), spot=60000)
    check("contango label is bare", up["term_structure"] == "contango", up["term_structure"])
    s = compute_vol_surface(_surface_tickers(), spot=62000)
    check("skew label matches template (no prose suffix)",
          s["skew_label"] == f"front 25Δ RR {s['expiries'][0]['rr_25d']:+}v → puts bid",
          s["skew_label"])
    check("no 'downside skew' suffix", "downside" not in s["skew_label"], s["skew_label"])


def test_aggregate_clips_merges_worked_order():
    # Three clips of the same 2:1 put spread (differing only in size) + one
    # distinct straddle. The clips collapse to one entry; the straddle stays.
    trades = (
        [_leg("BTC-31JUL26-60000-P", "buy", 100, iv=36.5, bid="C1"),
         _leg("BTC-31JUL26-64000-P", "sell", 50, iv=36.5, bid="C1")] +
        [_leg("BTC-31JUL26-60000-P", "buy", 20, iv=36.6, bid="C2"),
         _leg("BTC-31JUL26-64000-P", "sell", 10, iv=36.6, bid="C2")] +
        [_leg("BTC-31JUL26-60000-P", "buy", 20, iv=37.0, bid="C3"),
         _leg("BTC-31JUL26-64000-P", "sell", 10, iv=37.0, bid="C3")] +
        [_leg("BTC-26JUN26-62000-C", "buy", 40, bid="S1"),
         _leg("BTC-26JUN26-62000-P", "buy", 40, bid="S1")]
    )
    clusters = cluster_blocks(trades)
    ranked = summarize_blocks(clusters, top_n=10**9, min_btc=5.0)
    grouped = aggregate_clips(ranked, clusters)
    check("4 blocks → 2 grouped rows", len(grouped) == 2, grouped)
    spread = next(g for g in grouped if g["structure"] == "Put Spread")
    check("clip_count 3", spread["clip_count"] == 3, spread)
    check("sizes summed (150+30+30)", spread["size_btc"] == 210.0, spread)
    check("unit sizes summed (50+10+10)", spread["unit_size"] == 70.0, spread)
    check("keeps largest clip's id", spread["block_trade_id"] == "C1", spread)
    check("iv size-weighted toward big clip", 36.5 <= spread["avg_iv"] <= 36.7, spread)
    straddle = next(g for g in grouped if g["structure"] == "Straddle")
    check("distinct structure not merged", straddle["clip_count"] == 1, straddle)
    # Signature is ratio-based: 100/50 and 20/10 match; a 1:1 print would not.
    even = [_leg("BTC-31JUL26-60000-P", "buy", 10, bid="E1"),
            _leg("BTC-31JUL26-64000-P", "sell", 10, bid="E1")]
    check("different leg ratio → different signature",
          clip_signature(even) != clip_signature(clusters["C1"]), None)


def test_surface_extrapolation_flag():
    # Narrow strikes: call_delta only spans 0.40–0.60, so 25Δ wings are extrapolated.
    narrow = {
        "BTC-5JUN26-62000-C": {"mark_iv": 82.0, "delta": 0.50},
        "BTC-5JUN26-61500-C": {"mark_iv": 84.0, "delta": 0.60},
        "BTC-5JUN26-62500-C": {"mark_iv": 81.0, "delta": 0.40},
    }
    s = compute_vol_surface(narrow, spot=62000)
    check("narrow strikes → wings extrapolated", s["expiries"][0]["wings_extrapolated"] is True, s)


def test_surface_empty():
    s = compute_vol_surface({}, spot=62000)
    check("empty tickers → no expiries", s["expiries"] == [], s)
    check("empty → term None", s["term_structure"] is None, s)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"Running {len(tests)} test functions...")
    for t in tests:
        t()
    print(f"\n{_passed} checks passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
