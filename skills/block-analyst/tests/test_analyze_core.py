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

# net_cash sign: BUY positive, SELL negative, ×qty
ok(ac.net_cash([{"SIDE": "BUY", "PRICE": 2.9, "QTY": 10}]) == 29.0, "net_cash BUY debit")
ok(ac.net_cash([{"SIDE": "SELL", "PRICE": 2.9, "QTY": 10}]) == -29.0, "net_cash SELL credit")

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

print(f"\n{_p} passed, {_f} failed")
sys.exit(1 if _f else 0)
