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

# net_cash sign: BUY positive, SELL negative, ×qty
ok(ac.net_cash([{"SIDE": "BUY", "PRICE": 2.9, "QTY": 10}]) == 29.0, "net_cash BUY debit")
ok(ac.net_cash([{"SIDE": "SELL", "PRICE": 2.9, "QTY": 10}]) == -29.0, "net_cash SELL credit")

print(f"\n{_p} passed, {_f} failed")
sys.exit(1 if _f else 0)
