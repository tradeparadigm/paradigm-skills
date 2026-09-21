#!/usr/bin/env python3
"""
Unit tests for analyze_core.py — no network, no deps.  Run: python3 tests/test_analyze_core.py

These pin the parsing + sign/orientation conventions against REAL resolved trades
captured from the tape, so the script can never ship a wrong [Greeks] sign or a
mis-parsed structure. If a convention here is wrong, this fails before it ships.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import analyze_core as ac  # noqa: E402
import analyze as az  # noqa: E402 — stdlib-only; imported for _struct_name (display names)

_p = _f = 0


def ok(cond, msg):
    global _p, _f
    if cond:
        _p += 1
    else:
        _f += 1
        print(f"  FAIL: {msg}")


# ── ids / product / naming ─────────────────────────────────────────────────────
ok(ac.normalize_core_id("DRFQv2-r_3FvzABC") == "r_3FvzABC", "strip DRFQv2- prefix")
ok(ac.normalize_core_id("r_abc") == "r_abc", "bare id unchanged")
ok(ac.parse_product("SOL OPTION - DBT") == {"asset": "SOL", "kind": "OPTION", "venue": "DBT"},
   "parse SOL product")
ok(ac.parse_product("BTC PERPETUAL - PRDX")["kind"] == "PERPETUAL", "parse perp kind")
ok(ac.deribit_symbol("BTC", "31JUL26", 66000, "C") == "BTC-31JUL26-66000-C", "BTC symbol")
ok(ac.deribit_symbol("SOL", "31JUL26", 88, "c") == "SOL_USDC-31JUL26-88-C", "SOL_USDC symbol")

# ── offset: coin→bps, USD→percent ──────────────────────────────────────────────
ok(ac.offset(0.0131, 0.0135, "BTC")["txt"] == "-4 bps", "coin offset in bps (−4 not −40)")
ok(ac.offset(2.90, 2.7494, "USD")["txt"] == "+5.5%", "USD offset in percent")
ok(ac.offset(2.90, 2.7494, "USDC")["unit"] == "%", "USDC → percent")
# dollar-magnitude premium quoted 'BTC' (Paradex) must be % not a giant bps (the −324953 bug)
ok(ac.offset(140.87, 173.37, "BTC")["txt"] == "-18.7%", "dollar premium → percent, not bps")
# sub-$1 USD/USDC premium is still DOLLARS → percent, never ×10000 bps (cheap alt options)
ok(ac.offset(0.53, 0.50, "USDC")["txt"] == "+6%", "sub-$1 USDC premium → percent, not +300 bps")
ok(ac.offset(0.53, 0.50, "USD")["unit"] == "%", "sub-$1 USD premium → percent unit")

# ── single-leg SOL call (real: /analyze … Call 31 Jul 26 88) ───────────────────
p = ac.parse_description("Call 31 Jul 26 88")
ok(p["code"] == "CL" and len(p["legs"]) == 1, "single call parsed")
lg = p["legs"][0]
ok(lg["cp"] == "C" and lg["strike"] == 88 and lg["expiry_c"] == "31JUL26", "call leg fields")
legs, side, reliable = ac.apply_orientation(p, [{"SIDE": "BUY", "PRICE": 2.90, "QTY": 10000}])
ok(side == "Buyer" and reliable and legs[0]["sign"] == 1, "single call: Buyer, long, reliable")

# net greeks: long 1 call, delta 0.204 → +0.204 * qty
ng = ac.net_greeks(legs, {ac.leg_key(legs[0]): {"delta": 0.204, "vega": 0.8, "gamma": 0.0, "theta": -0.1}}, 10000)
ok(abs(ng["delta"] - 2040) < 1, "single call net delta scaled by qty")

# net greeks: a leg with MISSING greeks → {} (per-leg ⚠ display), never a partial net
p2 = ac.parse_description("ICondor  10 Jul 26  54000/56000/66000/67000")
legs2, _, _ = ac.apply_orientation(p2, [{"SIDE": "SELL", "PRICE": 264.07, "QTY": 5}])
gk = {ac.leg_key(l): {"delta": 0.1, "vega": 1.0, "gamma": 0.0, "theta": -0.1} for l in legs2[:3]}
ok(ac.net_greeks(legs2, gk, 5) == {}, "missing one leg's greeks → {} not a 3-leg partial sum")

# ── iron condor (real: /analyze … ICondor 10 Jul 26 54000/56000/66000/67000) ───
p = ac.parse_description("ICondor  10 Jul 26  54000/56000/66000/67000")
ok(p["code"] == "CO" and len(p["legs"]) == 4, "iron condor parsed to 4 legs")
rows = [{"SIDE": "BUY", "PRICE": 65.56, "QTY": 5}, {"SIDE": "SELL", "PRICE": 81.48, "QTY": 5},
        {"SIDE": "SELL", "PRICE": 264.07, "QTY": 5}, {"SIDE": "BUY", "PRICE": 139.12, "QTY": 5}]
legs, side, reliable = ac.apply_orientation(p, rows)
ok(side == "Seller" and reliable, "IC: net credit → Seller, reliable")
by = {(l["cp"], int(l["strike"])): l["sign"] for l in legs}
ok(by[("P", 54000)] == 1 and by[("C", 67000)] == 1, "IC long the wings")
ok(by[("P", 56000)] == -1 and by[("C", 66000)] == -1, "IC short the body")

# ── call condor (real: CCondor 17 Jul 26 64000/66000/68000/70000) — long=debit ─
p = ac.parse_description("CCondor 17 Jul 26 64000/66000/68000/70000")
ok(p["code"] == "CO" and len(p["legs"]) == 4 and all(l["cp"] == "C" for l in p["legs"]),
   "CCondor → 4 call legs")
legs, side, reliable = ac.apply_orientation(p, [{"SIDE": "BUY", "PRICE": 0.0058, "QTY": 1000}])
ok(side == "Buyer" and reliable, "call condor debit → Buyer, reliable")
byc = {int(l["strike"]): l["sign"] for l in legs}
ok(byc[64000] == 1 and byc[70000] == 1 and byc[66000] == -1 and byc[68000] == -1,
   "long call condor: long wings / short body")

# ── call fly (real: CFly 3 Jul 26 58000/60000/62000) ───────────────────────────
p = ac.parse_description("CFly 3 Jul 26 58000/60000/62000")
ok(p["code"] == "BF" and len(p["legs"]) == 3 and all(l["cp"] == "C" for l in p["legs"]),
   "CFly → 3 call legs")
mid = [l for l in p["legs"] if int(l["strike"]) == 60000][0]
ok(mid["ratio"] == 2, "fly body ratio 2")

# iron condor stays a credit structure (long wings / short body when Seller)
p = ac.parse_description("ICondor 10 Jul 26 54000/56000/66000/67000")
legs, side, _ = ac.apply_orientation(p, [{"SIDE": "SELL", "PRICE": 264.07, "QTY": 5}])
ok(side == "Seller", "iron condor net credit → Seller")

# ── iron fly — 4 legs (2P+2C), credit reference, NOT a 3-leg put fly ───────────
p = ac.parse_description("IFly 3 Jul 26 58000/60000/62000")
ok(p["code"] == "BF" and len(p["legs"]) == 4, "IFly → 4 legs")
ok(sorted((l["cp"], int(l["strike"])) for l in p["legs"]) ==
   [("C", 60000), ("C", 62000), ("P", 58000), ("P", 60000)], "IFly legs: put wing/body + call body/wing")
legs, side, reliable = ac.apply_orientation(p, [{"SIDE": "SELL", "PRICE": 0.02, "QTY": 10}])
ok(side == "Seller" and reliable, "IFly net credit → Seller, reliable")
byf = {(l["cp"], int(l["strike"])): l["sign"] for l in legs}
ok(byf[("P", 58000)] == 1 and byf[("C", 62000)] == 1, "long IFly: long the wings")
ok(byf[("P", 60000)] == -1 and byf[("C", 60000)] == -1, "long IFly: short the straddle body")

# ── verticals — call spread ref is a debit, PUT spread ref is a CREDIT ─────────
# call spread bought for a debit → long lo call / short hi call
p = ac.parse_description("CSpread 31 Jul 26 60000/65000")
ok(p["classified"] and len(p["legs"]) == 2, "call spread parsed to 2 legs")
legs, side, reliable = ac.apply_orientation(p, [{"SIDE": "BUY", "PRICE": 0.02, "QTY": 100}])
ok(side == "Buyer" and reliable, "call spread debit → Buyer, reliable")
byv = {int(l["strike"]): l["sign"] for l in legs}
ok(byv[60000] == 1 and byv[65000] == -1, "long call spread: +lo / -hi")
# bear put spread bought for a DEBIT → long HI put / short LO put (the flip case
# that was inverted when ref_is_debit was hardcoded True for puts)
p = ac.parse_description("PSpread 31 Jul 26 60000/65000")
legs, side, reliable = ac.apply_orientation(p, [{"SIDE": "BUY", "PRICE": 0.02, "QTY": 100}])
ok(side == "Buyer" and reliable, "put spread debit → Buyer, reliable")
byv = {int(l["strike"]): l["sign"] for l in legs}
ok(byv[65000] == 1 and byv[60000] == -1, "bear put spread (debit): +hi / -lo")
# bull put spread sold for a CREDIT → long lo put / short hi put (no flip)
p = ac.parse_description("PSpread 31 Jul 26 60000/65000")
legs, side, reliable = ac.apply_orientation(p, [{"SIDE": "SELL", "PRICE": 0.02, "QTY": 100}])
ok(side == "Seller" and reliable, "put spread credit → Seller, reliable")
byv = {int(l["strike"]): l["sign"] for l in legs}
ok(byv[60000] == 1 and byv[65000] == -1, "bull put spread (credit): +lo / -hi")
# the TAPE writes verticals as the abbreviation "PSpd"/"CSpd" (not the spelled-out
# "Spread") — these must classify identically or they fall to the slow model
# fallback. Regression guard for that exact gap.
p = ac.parse_description("CSpd 31 Jul 26 60000/70000")
ok(p["code"] == "CS" and p["classified"] and len(p["legs"]) == 2, "CSpd abbrev parses to a call spread")
legs, side, reliable = ac.apply_orientation(p, [{"SIDE": "BUY", "PRICE": 0.02, "QTY": 100}])
byv = {int(l["strike"]): l["sign"] for l in legs}
ok(side == "Buyer" and reliable and byv[60000] == 1 and byv[70000] == -1,
   "CSpd debit → Buyer, reliable, +lo / -hi")
p = ac.parse_description("PSpd 25 Sep 26 52000/35000")
ok(p["code"] == "PS" and p["classified"] and len(p["legs"]) == 2, "PSpd abbrev parses to a put spread")
legs, side, reliable = ac.apply_orientation(p, [{"SIDE": "BUY", "PRICE": 0.0225, "QTY": 100}])
byv = {int(l["strike"]): l["sign"] for l in legs}
ok(side == "Buyer" and reliable and byv[52000] == 1 and byv[35000] == -1,
   "PSpd bought (debit) → long hi put / short lo put")

# ── ratio spreads are NOT 1:1 verticals → defer to the unmapped fallback ──────
ok(ac.parse_description("CRatioSpread 27 Jun 26 60000/62000")["classified"] is False,
   "ratio spread name → not classified (never netted 1:1)")

# ── struct_net: per-structure premium must QTY-weight unequal legs ─────────────
# Real 1×2×1 call fly (tape stores 3 per-leg rows; body QTY is 2× the wings). The net
# debit is +wing − 2×body + wing, NOT a flat per-row sum (that over-states it and can
# exceed the fly's max payoff — the bug this guards).
_fly = [{"PRODUCT": "BTC OPTION - DBT", "SIDE": "BUY",  "QTY": 25, "PRICE": 0.0131, "REF_PRICE": 0.013},
        {"PRODUCT": "BTC OPTION - DBT", "SIDE": "SELL", "QTY": 50, "PRICE": 0.0078, "REF_PRICE": 0.0081},
        {"PRODUCT": "BTC OPTION - DBT", "SIDE": "BUY",  "QTY": 25, "PRICE": 0.0045, "REF_PRICE": 0.0043}]
ok(abs(ac.struct_net(_fly, "PRICE") - 0.0020) < 1e-9, "fly net PRICE = +wing -2*body +wing = 0.0020")
ok(abs(ac.struct_net(_fly, "REF_PRICE") - 0.0011) < 1e-9, "fly net REF = 0.0011 (body weighted 2x)")
ok(ac.offset(abs(ac.struct_net(_fly, "PRICE")), abs(ac.struct_net(_fly, "REF_PRICE")), "BTC")["val"] == 9.0,
   "fly offset = +9 bps (not the unweighted +6)")
# perp/future legs are excluded from premium (a hedge is not premium)
_combo = [{"PRODUCT": "BTC OPTION - DBT", "SIDE": "BUY", "QTY": 25, "PRICE": 0.10, "REF_PRICE": 0.10},
          {"PRODUCT": "BTC PERPETUAL - DBT", "SIDE": "SELL", "QTY": 9000, "PRICE": 61000, "REF_PRICE": 61000}]
ok(abs(ac.struct_net(_combo, "PRICE") - 0.10) < 1e-9, "perp leg excluded from option premium")
# equal-quantity structures are unchanged (every weight = 1)
_vert = [{"PRODUCT": "BTC OPTION - DBT", "SIDE": "BUY",  "QTY": 100, "PRICE": 0.02, "REF_PRICE": 0.02},
         {"PRODUCT": "BTC OPTION - DBT", "SIDE": "SELL", "QTY": 100, "PRICE": 0.01, "REF_PRICE": 0.01}]
ok(abs(ac.struct_net(_vert, "PRICE") - 0.01) < 1e-9, "equal-qty vertical net unchanged (+0.02 -0.01)")

# ── 2-digit alt strikes parse; the date's YY is never swallowed as a strike ────
p = ac.parse_description("Strangle 28 Aug 26 88/95")
ok(p["classified"] and sorted(int(l["strike"]) for l in p["legs"]) == [88, 95],
   "2-digit SOL strikes parse as a strangle")
ok(ac.parse_description("Straddle 19 Nov 25")["classified"] is False,
   "strike-less description: year not misread as a strike")

# ── RRPut — signs NOT reliably derivable from tape → defer to model ────────────
p = ac.parse_description("RRPut 31 Jul 26 50000/70000")
ok(p["code"] == "RR" and len(p["legs"]) == 2, "RRPut parsed to 2 legs")
_, _, reliable = ac.apply_orientation(p, [{"SIDE": "BUY", "PRICE": 0.0091, "QTY": 200},
                                          {"SIDE": "SELL", "PRICE": 0.0037, "QTY": 200}])
ok(reliable is False, "RR: reliable is False (model nets the greeks)")

# ── calendar — two expiries, direction deferred ────────────────────────────────
p = ac.parse_description("CCal 10 Jul 26 63000 / 31 Jul 26 63000")
ok(p["code"] == "CA" and len(p["legs"]) == 2, "calendar parsed to 2 legs")
ok({l["expiry_c"] for l in p["legs"]} == {"10JUL26", "31JUL26"}, "calendar two expiries")
_, _, reliable = ac.apply_orientation(p, [{"SIDE": "BUY", "PRICE": 0.0065, "QTY": 200},
                                          {"SIDE": "SELL", "PRICE": 0.0251, "QTY": 200}])
ok(reliable is False, "calendar: reliable is False")

# ── Cstm — explicit per-leg signs (real 4-leg custom) → reliable ───────────────
p = ac.parse_description(
    "Cstm -1.00 Put 25 Sep 26 45000 +1.50 Put 25 Sep 26 60000 -1.00 Call 25 Sep 26 75000")
ok(p["code"] == "CM" and len(p["legs"]) == 3, "Cstm parsed to 3 option legs")
sg = {(l["cp"], int(l["strike"])): (l["sign"], l["ratio"]) for l in p["legs"]}
ok(sg[("P", 45000)] == (-1, 1.0), "Cstm 45kP short x1")
ok(sg[("P", 60000)] == (1, 1.5), "Cstm 60kP long x1.5")
ok(sg[("C", 75000)] == (-1, 1.0), "Cstm 75kC short x1")
_, _, reliable = ac.apply_orientation(p, [{"SIDE": "SELL", "PRICE": 0.069, "QTY": 50}])
ok(reliable, "Cstm: explicit signs → reliable")

# ── per-leg rows (RRPut stored as separate rows: a put, a call, a perp) ────────
rr_rows = [
    {"PRODUCT": "BTC OPTION - DBT", "DESCRIPTION": "Put 31 Jul 26 50000", "SIDE": "BUY", "PRICE": 0.0091, "QTY": 200},
    {"PRODUCT": "BTC OPTION - DBT", "DESCRIPTION": "Call 31 Jul 26 70000", "SIDE": "SELL", "PRICE": 0.0037, "QTY": 200},
    {"PRODUCT": "BTC PERPETUAL - DBT", "DESCRIPTION": "Perpetual", "SIDE": "BUY", "PRICE": 59324, "QTY": 232398},
]
lr = ac.legs_from_rows(rr_rows)
ok(lr is not None and len(lr) == 3, "per-leg rows → 3 legs built")
bykey = {(l["cp"], l["strike"]): l["sign"] for l in lr}
ok(bykey[("P", 50000)] == 1, "per-leg: long 50k put (row SIDE BUY)")
ok(bykey[("C", 70000)] == -1, "per-leg: short 70k call (row SIDE SELL)")
ok(any(l["cp"] == "FUT" and l["sign"] == 1 for l in lr), "per-leg: long perp leg from perp row")
# combined-description block (ICondor: same desc on every row) is NOT per-leg mode
ic_rows = [{"PRODUCT": "BTC OPTION - PRDX", "DESCRIPTION": "ICondor 10 Jul 26 54000/56000/66000/67000",
            "SIDE": "BUY", "PRICE": 65.56, "QTY": 5}] * 4
ok(ac.legs_from_rows(ic_rows) is None, "combined-desc block → not per-leg (parse the structure)")

# ── package net offset (multi-leg): (|net_fill| − |net_mark|) in the displayed ──
# Paid/Recd orientation — the ONE convention. Real RRPut (Seller, per-leg option
# rows + a perp hedge): Recd 0.0009 net credit vs mark 0.0015 → −6 bps BELOW mark
# (received less than mark; against the taker). The five near-identical fills of
# this structure must all land on this same sign — never the +1/+7 per-leg split.
_rr_off = [{"PRODUCT": "BTC OPTION - DBT", "SIDE": "SELL", "QTY": 200, "PRICE": 0.0177, "REF_PRICE": 0.0176},
           {"PRODUCT": "BTC OPTION - DBT", "SIDE": "BUY",  "QTY": 200, "PRICE": 0.0168, "REF_PRICE": 0.0161},
           {"PRODUCT": "BTC PERPETUAL - DBT", "SIDE": "BUY", "QTY": 300000, "PRICE": 61000, "REF_PRICE": 61000}]
ok(abs(ac.struct_net(_rr_off, "PRICE") - (-0.0009)) < 1e-9, "RRPut net credit executed = -0.0009 (perp excluded)")
ok(abs(ac.struct_net(_rr_off, "REF_PRICE") - (-0.0015)) < 1e-9, "RRPut net credit at mark = -0.0015")
_rroff = ac.offset(abs(ac.struct_net(_rr_off, "PRICE")), abs(ac.struct_net(_rr_off, "REF_PRICE")), "BTC")
ok(_rroff["val"] == -6.0 and _rroff["sign"] == -1, "RRPut package offset = -6 bps, below mark (not +1/+7 per-leg)")

# net_cash sign: BUY positive, SELL negative, ×qty
ok(ac.net_cash([{"SIDE": "BUY", "PRICE": 2.9, "QTY": 10}]) == 29.0, "net_cash BUY debit")
ok(ac.net_cash([{"SIDE": "SELL", "PRICE": 2.9, "QTY": 10}]) == -29.0, "net_cash SELL credit")

# ── display names — DRFQ vocabulary (Butterfly family, typed calendars) ────────
# Names must match the DRFQ StrategyCodeEnum table (rfq-trader references/
# instruments.md): "Call/Put/Iron Butterfly" (never "Fly"), calendars typed C/P.
ok(az._struct_name("BF", ac.parse_description("CFly 3 Jul 26 58000/60000/62000")["legs"])
   == "Call Butterfly", "CFly → Call Butterfly (not Call Fly)")
ok(az._struct_name("BF", ac.parse_description("PFly 27 Mar 26 40000/50000/60000")["legs"])
   == "Put Butterfly", "PFly → Put Butterfly (not Put Fly)")
ok(az._struct_name("BF", ac.parse_description("IFly 3 Jul 26 58000/60000/62000")["legs"])
   == "Iron Butterfly", "IFly (2P+2C legs) → Iron Butterfly (not Iron Fly)")
ok(az._struct_name("CA", ac.parse_description("CCal 10 Jul 26 63000 / 31 Jul 26 63000")["legs"])
   == "Call Calendar", "CCal → typed Call Calendar (not bare Calendar)")
ok(az._struct_name("CA", ac.parse_description("PCal 26 Dec 25 86000 / 27 Mar 26 86000")["legs"])
   == "Put Calendar", "PCal → typed Put Calendar")
# cross-expiry pair with DIFFERENT strikes is a diagonal, not a calendar
ok(az._struct_name("CA", ac.parse_description("PCal 26 Dec 25 86000 / 27 Mar 26 85000")["legs"])
   == "Put Diagonal", "PCal with two strikes → Put Diagonal")
ok(az._struct_name("CM", []) == "Custom", "CM → Custom (the unnamed-package bucket)")
# per-leg-rows ("combo") path — signs from row SIDE, names match options-recap:
# same-strike C&P traded opposite ways is a synthetic forward → "Combo", not RR
syn = ac.legs_from_rows([
    {"PRODUCT": "BTC OPTION - DBT", "DESCRIPTION": "Call 31 Jul 26 60000", "SIDE": "BUY", "PRICE": 0.05, "QTY": 100},
    {"PRODUCT": "BTC OPTION - DBT", "DESCRIPTION": "Put 31 Jul 26 60000", "SIDE": "SELL", "PRICE": 0.04, "QTY": 100}])
ok(az._struct_name("combo", syn) == "Combo", "per-leg same-strike C&P opposite ways → Combo (synthetic)")
# C&P at different strikes traded opposite ways stays a Risk Reversal
rrl = ac.legs_from_rows([
    {"PRODUCT": "BTC OPTION - DBT", "DESCRIPTION": "Put 31 Jul 26 50000", "SIDE": "BUY", "PRICE": 0.0091, "QTY": 200},
    {"PRODUCT": "BTC OPTION - DBT", "DESCRIPTION": "Call 31 Jul 26 70000", "SIDE": "SELL", "PRICE": 0.0037, "QTY": 200}])
ok(az._struct_name("combo", rrl) == "Risk Reversal", "per-leg C&P diff strikes opposite ways → Risk Reversal")
# a calendar stored as per-leg rows gets its typed name, not the Combo bucket
cal = ac.legs_from_rows([
    {"PRODUCT": "BTC OPTION - DBT", "DESCRIPTION": "Call 10 Jul 26 63000", "SIDE": "SELL", "PRICE": 0.0065, "QTY": 200},
    {"PRODUCT": "BTC OPTION - DBT", "DESCRIPTION": "Call 31 Jul 26 63000", "SIDE": "BUY", "PRICE": 0.0251, "QTY": 200}])
ok(az._struct_name("combo", cal) == "Call Calendar", "per-leg cross-expiry same-strike calls → Call Calendar")

# ── fallbacks for unmapped structures ──────────────────────────────────────────
# a description that lists explicit legs (even under an unknown name) → extractable
gl = ac.extract_legs_generic("Seagull -1 Put 31 Jul 26 55000 +1 Call 31 Jul 26 70000")
ok(len(gl) == 2 and {l["cp"] for l in gl} == {"P", "C"}, "generic extract pulls explicit legs")
ok(gl[0]["sign"] == -1 and gl[1]["sign"] == 1, "generic extract keeps explicit signs")
# a named structure that lists only strikes (no per-leg types) → nothing to extract
ok(ac.extract_legs_generic("Strangle 28 Aug 26 57000/68000") == [], "no explicit legs → empty (raw-rows fallback)")
# an unmapped structure name → parse_description does NOT classify (→ fallback ladder)
up = ac.parse_description("Seagull 31 Jul 26 55000/60000/70000")
ok(up["classified"] is False, "unmapped name → not classified")

# ── structure unit: the displayed size must be the base the premium nets against ──
ratio_rows = [
    {"PRODUCT": "BTC OPTION - DBT", "DESCRIPTION": "Cstm  -2.00  Put  24 Jul 26  59000       +1.00  Put  24 Jul 26  65000",
     "QTY": 40, "PRICE": 0.0023, "REF_PRICE": 0.0021, "SIDE": "BUY"},
    {"PRODUCT": "BTC OPTION - DBT", "DESCRIPTION": "Cstm  -2.00  Put  24 Jul 26  59000       +1.00  Put  24 Jul 26  65000",
     "QTY": 20, "PRICE": 0.0210, "REF_PRICE": 0.0212, "SIDE": "SELL"},
]
# 40/20 is 20 packages of (buy 2, sell 1) — taking the first row's QTY said 40,
# which contradicted the premium struct_net nets against the same base.
ok(ac.structure_unit(ratio_rows) == 20.0, "ratio package unit is the base leg, not the first row")
ok(abs(ac.struct_net(ratio_rows, "PRICE") + 0.0164) < 1e-9, "2:1 weighted fill nets to 0.0164 credit")
ok(abs(ac.struct_net(ratio_rows, "REF_PRICE") + 0.0170) < 1e-9, "2:1 weighted mark nets to 0.0170 credit")
equal_rows = [dict(r, QTY=100) for r in ratio_rows]
ok(ac.structure_unit(equal_rows) == 100.0, "equal-size legs are unaffected")
hedged = ratio_rows + [{"PRODUCT": "BTC PERPETUAL - DBT", "DESCRIPTION": "Perpetual 65,000",
                        "QTY": 5, "PRICE": 65000, "REF_PRICE": 64955.57, "SIDE": "SELL"}]
ok(ac.structure_unit(hedged) == 20.0, "a smaller perp hedge row does not become the structure unit")

# The same trade in the tape's other shapes. Smallest row QTY is the base only
# when every row is one distinct leg; these two are where it is not.
one_row = [dict(ratio_rows[0], QTY=40)]
ok(ac.structure_unit(one_row) == 20.0, "a single row STATING -2.00/+1.00 divides by its widest ratio")
ok(abs(ac.struct_net(one_row, "PRICE") - 0.0023) < 1e-9,
   "that row's PRICE is already the package price, so it still weights as 1")
# A named structure's ratios are OUR canonical geometry, not something the tape
# wrote: CFly parses to 1/2/1, and dividing by that made a 100-lot fly ×50 and
# halved its greeks with it.
fly_row = [{"PRODUCT": "BTC OPTION - DBT", "DESCRIPTION": "CFly 24 Jul 26 59000/62000/65000",
            "QTY": 100, "PRICE": 0.0023, "REF_PRICE": 0.0021, "SIDE": "BUY"}]
ok([l["ratio"] for l in ac.parse_description(fly_row[0]["DESCRIPTION"])["legs"]] == [1.0, 2.0, 1.0],
   "CFly's 1/2/1 comes from the structure map, not the DESCRIPTION text")
ok(ac.structure_unit(fly_row) == 100.0, "a 100-lot fly is 100 flies, not 50")
# The sold leg clipped across two makers, every row repeating the package string.
# Nothing in that string says which row is which leg, so this is NOT recovered —
# the smallest row wins and the caller is told the size is inferred.
clipped = [dict(ratio_rows[0], QTY=40), dict(ratio_rows[1], QTY=10), dict(ratio_rows[1], QTY=10)]
ok(ac.structure_unit(clipped) == 10.0, "a clipped leg under a combined DESCRIPTION falls back to the smallest row")
ok(not ac.package_size_certain(clipped), "and the caller is told so")
per_leg = [{"PRODUCT": "BTC OPTION - DBT", "DESCRIPTION": "Put 24 Jul 26 59000",
            "QTY": 40, "PRICE": 0.0023, "REF_PRICE": 0.0021, "SIDE": "BUY"},
           {"PRODUCT": "BTC OPTION - DBT", "DESCRIPTION": "Put 24 Jul 26 65000",
            "QTY": 20, "PRICE": 0.0210, "REF_PRICE": 0.0212, "SIDE": "SELL"}]
ok(ac.structure_unit(per_leg) == 20.0, "per-leg rows give the same unit as the combined form")
# One leg, several makers: the clips ADD. Taking the smallest called a 50-lot
# call x20 and counted its premium 2.5 times.
clips = [{"PRODUCT": "BTC OPTION - DBT", "DESCRIPTION": "Call 7 May 26 84000",
          "QTY": q, "PRICE": 0.0122, "REF_PRICE": 0.0118, "SIDE": "BUY"} for q in (30, 20)]
ok(ac.structure_unit(clips) == 50.0, "clips of one instrument add rather than compete for the minimum")
ok(abs(ac.struct_net(clips, "PRICE") - 0.0122) < 1e-9,
   "and the premium counts that leg once, not 2.5 times")
# A DESCRIPTION that does not resolve to ONE leg carries no identity, so rows
# under it are not grouped at all: the smallest row wins, as it did before this
# PR, and package_size_certain reports that the answer is inferred. Inferring
# clips from side and/or price was tried and mis-sized a different family of
# equal-size structures each time — see the PR body.
unkeyed = [dict(r, DESCRIPTION="C 7 May 26 84000") for r in clips]
ok(ac.structure_unit(unkeyed) == 20.0, "an unresolvable DESCRIPTION falls back to the smallest row")
ok(not ac.package_size_certain(unkeyed), "and says the size is inferred")
# Put-call parity makes an at-the-forward straddle's two legs print the SAME
# price, and its two rows the same side. Grouping on either merged them into one
# leg of double the size — ×200 with the premium AND the bps offset halved.
straddle = [{"PRODUCT": "BTC OPTION - DBT", "DESCRIPTION": "Straddle 25 Sep 26 62000",
             "QTY": 100, "PRICE": 0.0410, "REF_PRICE": 0.0405, "SIDE": "SELL"}] * 2
ok(ac.structure_unit(straddle) == 100.0, "a straddle's two same-priced legs are legs, not clips")
ok(abs(ac.struct_net(straddle, "PRICE") + 0.0820) < 1e-9,
   "so its package premium is both legs, not one")
ok(ac.package_size_certain(straddle), "equal legs make the base a fact whatever the structure")
# The same shape with a leg clipped is indistinguishable from it, which is the
# whole reason nothing is inferred here.
clipped_straddle = [straddle[0], dict(straddle[1], QTY=50), dict(straddle[1], QTY=50)]
ok(not ac.package_size_certain(clipped_straddle), "a clipped leg cannot be told from a third leg")
# Certainty holds where identity is real, or where no two rows share a side.
ok(ac.package_size_certain(clips), "per-instrument rows carry real identity")
ok(ac.package_size_certain(ratio_rows), "distinct sides under one description are unambiguous")
ok(ac.package_size_certain(fly_row), "a single row is never ambiguous")
# The base must not depend on the order rows arrive in. struct_net was always
# order-independent; keying the shared base on the FIRST row made the premium
# swing 4x on the same block depending on CSV order.
for _perm in ([0, 1, 2], [1, 0, 2], [2, 1, 0]):
    _shuffled = [clipped[i] for i in _perm]
    ok(ac.structure_unit(_shuffled) == ac.structure_unit(clipped),
       f"row order {_perm} does not move the size")
    ok(abs(ac.struct_net(_shuffled, "PRICE") - ac.struct_net(clipped, "PRICE")) < 1e-12,
       f"row order {_perm} does not move the premium")
# Unequal legs with nothing stating their ratios: the smallest row is a guess,
# and the guess must be declared. A 100/100/10 spread-with-a-tail and a 10:10:1
# ratio are the same three numbers.
for _rows, _name in ((clipped, "a clipped ratio leg"),
                     (clipped_straddle, "a clipped straddle"), (unkeyed, "bare clips")):
    ok(not ac.package_size_certain(_rows), f"{_name} is declared inferred")
tail = [{"PRODUCT": "BTC OPTION - DBT", "DESCRIPTION": d, "QTY": q, "PRICE": p,
         "REF_PRICE": p, "SIDE": sd} for d, q, p, sd in (
    ("Call 24 Jul 26 60000", 100, 0.0100, "BUY"),
    ("Call 24 Jul 26 70000", 100, 0.0090, "SELL"),
    ("Call 24 Jul 26 90000", 10, 0.0010, "BUY"))]
ok(not ac.package_size_certain(tail),
   "a spread with a small tail cannot know its own base, and says so")

# The per-leg `ratio` legs_from_rows sets is consumed by net_greeks, and nothing
# held it: dropping it gave net delta 0.0, and passing the raw QTY gave 400.0,
# against a correct 20.0 — both invisible to every other check here.
_ratio_rows = [{"PRODUCT": "BTC OPTION - DBT", "DESCRIPTION": "Put 24 Jul 26 59000",
                "QTY": 40, "PRICE": 0.0023, "REF_PRICE": 0.0021, "SIDE": "BUY"},
               {"PRODUCT": "BTC OPTION - DBT", "DESCRIPTION": "Put 24 Jul 26 65000",
                "QTY": 20, "PRICE": 0.0210, "REF_PRICE": 0.0212, "SIDE": "SELL"}]
_legs = ac.legs_from_rows(_ratio_rows)
ok([l["ratio"] for l in _legs] == [2.0, 1.0],
   f"a 40/20 fill carries leg ratios 2:1 against the base {[l[chr(39)+chr(39)] if False else l['ratio'] for l in _legs]}")
_greeks = {ac.leg_key(l): {"delta": 0.5, "vega": 1.0, "gamma": 0.0, "theta": 0.0} for l in _legs}
_net = ac.net_greeks(_legs, _greeks, ac.structure_unit(_ratio_rows))
# +2 x 0.5 - 1 x 0.5 = 0.5 per package, x20 packages = 10.
ok(abs(_net["delta"] - 10.0) < 1e-9,
   f"and the greeks are qty-weighted by them, not by a flat 1 {_net}")


# ── the header itself: structure_unit reaching the rendered ×N ──────────────────
# analyze.py:139 is the user-visible half of the ratio fix, and reverting it to
# fill[0]["QTY"] left every check above green. _run is exercised with the network
# stubbed so the assertion is on the rendered line, not on the core function.
def _rendered(rows):
    import csv as _csv, io, tempfile, types
    from contextlib import redirect_stdout
    saved = (az._get, az.fetch_ticker, az.fetch_trades_bucket)
    az._get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no network in tests"))
    az.fetch_ticker = lambda sym: (sym, None)
    az.fetch_trades_bucket = lambda sym, now_ms: (sym, None)
    directory = tempfile.mkdtemp()
    try:
        with open(os.path.join(directory, "fill.csv"), "w", newline="") as handle:
            writer = _csv.DictWriter(handle, fieldnames=sorted(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        out = io.StringIO()
        with redirect_stdout(out):
            az._run(types.SimpleNamespace(csv_dir=directory, now_ms=1_752_000_000_000,
                                          render=True))
        return out.getvalue()
    finally:
        az._get, az.fetch_ticker, az.fetch_trades_bucket = saved


# Every shape through the renderer, not just the one that was already right:
# structure_unit/struct_net stayed green through the whole round-1 bug, so the
# level that matters is the printed line.
for _rows, _want, _never, _label in (
        (ratio_rows, "×20", "×40", "ratio rows"),
        (one_row, "×20", "×40", "a single row stating its ratios"),
        (fly_row, "×100", "×50", "a named fly, whose ratios the tape never wrote"),
        (clips, "×50", "×20", "one leg filled by two makers"),
        (straddle, "×100", "×200", "an at-the-forward straddle, both legs one price")):
    _out = _rendered(_rows)
    ok(_want in _out, f"header sizes {_label} {_want} [{_out[:110]}]")
    ok(_never not in _out, f"header never sizes {_label} {_never}")

# An inferred size says so where the reader sees it, not in a trailing comment.
_amb = _rendered(clipped)
ok("⚠ ×N INFERRED" in _amb, f"an inferred size is declared in the body [{_amb[:110]}]")
# The doubt covers the premium and the offset too, not just the size: they are
# netted against the same base, so they are wrong by the same factor.
ok("bps offset are unconfirmed" in _amb, "and the warning scopes the doubt to all three")

# A Cstm whose second leg omits its ratio: the CSTM pattern requires one, so
# that leg was dropped and the block sized off its own QTY as if it were a
# single outright, reporting the base as certain. It must not classify.
_half = [{"PRODUCT": "BTC OPTION - DBT", "QTY": 40, "PRICE": 0.01, "REF_PRICE": 0.01,
          "SIDE": "BUY",
          "DESCRIPTION": "Cstm  +2.00  Call  24 Jul 26  60000       Call  24 Jul 26  70000"}]
ok(ac.parse_description(_half[0]["DESCRIPTION"])["classified"] is False,
   "a Cstm that drops a leg does not classify")
_out = _rendered(_half)
ok("⚠ unmapped structure" in _out,
   f"and the reader is told the structure was not mapped [{_out[:100]}]")
ok(_rendered(ratio_rows).count("⚠ ×N INFERRED") == 0,
   "and an unambiguous one says nothing")

print(f"\n{_p} passed, {_f} failed")
sys.exit(1 if _f else 0)
