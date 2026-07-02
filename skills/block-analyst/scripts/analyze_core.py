#!/usr/bin/env python3
"""
analyze_core.py — pure, dependency-free logic for the block analyst.

No network, no auth, no deps — so it unit-tests by inspection (see
tests/test_analyze_core.py) and can't drift from the live CLI.

Responsibilities (all deterministic, given the resolved tape rows + live greeks):
  • normalise the rfq_id, parse PRODUCT → asset/kind/venue
  • parse DESCRIPTION → structure code + legs (strike/type/ratio, explicit signs
    for Cstm; canonical signs for named structures)
  • orient the structure (Buyer/Seller, long/short) from the NET CASH of the tape
    rows — deterministic, needs no per-leg→strike mapping
  • net greeks = Σ (taker_leg_sign × ratio × per-leg greek) × qty
  • fill-vs-mark offset, unit picked by quote currency (bps for coin, % for USD/USDC)
  • Deribit instrument naming (incl. USDC-margined alts: SOL_USDC-…)

The orchestrator (analyze.py) supplies live per-strike greeks; this module never
fetches. Anything it can't confidently classify is flagged so the caller can hand
the computed data to the model for the final render.
"""
from __future__ import annotations

import re

MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


# ── ids / product ────────────────────────────────────────────────────────────

def normalize_core_id(raw: str) -> str:
    """Strip a routing prefix (DRFQv2- / GRFQ-) → the stable r_… core."""
    s = (raw or "").strip()
    s = re.sub(r"^(DRFQv2-|GRFQ-)", "", s)
    return s


def parse_product(product: str) -> dict:
    """'BTC OPTION - DBT' → {asset:'BTC', kind:'OPTION', venue:'DBT'}.
    'ETH PERPETUAL - DBT' → kind PERPETUAL. Never assume BTC."""
    p = (product or "").strip()
    left, _, venue = p.partition(" - ")
    toks = left.split()
    asset = toks[0].upper() if toks else ""
    kind = toks[1].upper() if len(toks) > 1 else ""
    return {"asset": asset, "kind": kind, "venue": (venue or "").strip().upper()}


def deribit_symbol(asset: str, expiry_c: str, strike, cp: str) -> str:
    """BTC/ETH → 'BTC-31JUL26-66000-C'; USDC-margined alts → 'SOL_USDC-…'."""
    base = asset.upper()
    if base not in ("BTC", "ETH"):
        base = f"{base}_USDC"
    k = int(round(float(strike)))
    return f"{base}-{expiry_c}-{k}-{cp.upper()}"


def perp_symbol(asset: str) -> str:
    base = asset.upper()
    return f"{base}-PERPETUAL" if base in ("BTC", "ETH") else f"{base}_USDC-PERPETUAL"


# ── description parsing ────────────────────────────────────────────────────────

_DATE = r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{2})"          # DD Mon YY
_TYPE = r"(Call|Put|C|P)"


def compact_expiry(d: int, mon: str, yy: str) -> str:
    return f"{int(d)}{mon.upper()}{yy}"


def _leg(cp: str, strike, ratio=1.0, sign=None, expiry_c=None) -> dict:
    cp = "C" if cp.upper().startswith("C") else "P"
    return {"cp": cp, "strike": float(strike), "ratio": float(ratio),
            "sign": sign, "expiry_c": expiry_c}


def parse_description(desc: str) -> dict:
    """Parse the tape DESCRIPTION into structure code + legs.

    Returns {code, legs, expiries, perp, classified, raw}. `legs[i].sign` is set
    only for Cstm (explicit +/-); named structures get canonical signs later via
    apply_orientation(). `classified` is False when we don't recognise the shape
    (caller then defers the render to the model).
    """
    raw = (desc or "").strip()
    toks = raw.split()
    if not toks:
        return {"code": "?", "legs": [], "expiries": [], "perp": False,
                "classified": False, "raw": raw}
    head = toks[0]
    up = head.upper()

    # Custom: explicit per-leg [+/-]ratio Type DD Mon YY Strike  (any # of legs)
    if up.startswith("CSTM") or up.startswith("CUSTOM"):
        legs, perp = [], False
        # each leg: sign ratio type date strike   e.g. -1.00 Put 25 Sep 26 45000
        for m in re.finditer(
                r"([+-]?\d*\.?\d+)\s+" + _TYPE + r"\s+" + _DATE + r"\s+(\d+)", raw):
            r_, cp, d, mon, yy, k = m.groups()
            sign = -1 if float(r_) < 0 else 1
            lg = _leg(cp, k, abs(float(r_)), sign, compact_expiry(d, mon, yy))
            lg["_explicit"] = True
            legs.append(lg)
        if re.search(r"Perp", raw, re.I):
            perp = True
        return {"code": "CM", "legs": legs, "expiries": _uniq_exp(legs),
                "perp": perp, "classified": bool(legs), "raw": raw}

    # Single outright: "Call 31 Jul 26 88" / "Put 7 May 26 84000"
    if up in ("CALL", "PUT"):
        m = re.search(_DATE + r"\s+(\d+)", raw)
        if m:
            d, mon, yy, k = m.groups()
            ec = compact_expiry(d, mon, yy)
            return {"code": "CL" if up == "CALL" else "PL",
                    "legs": [_leg(up[0], k, 1.0, None, ec)],
                    "expiries": [ec], "perp": False, "classified": True, "raw": raw}

    # Named multi-leg: <NAME> DD Mon YY  K[/K...]   (one expiry, N strikes)
    dm = re.search(_DATE, raw)
    # strikes = the trailing a/b/c/d group
    ks = []
    km = re.search(r"(\d{3,7}(?:\s*/\s*\d{3,7})*)\s*$", raw)
    if km:
        ks = [int(x) for x in re.split(r"\s*/\s*", km.group(1))]
    if dm and ks:
        d, mon, yy = dm.groups()
        ec = compact_expiry(d, mon, yy)
        legs = _named_legs(up, ks, ec)
        if legs is not None:
            return {"code": _code_of(up), "legs": legs, "expiries": _uniq_exp(legs),
                    "perp": bool(re.search(r"Perp", raw, re.I)),
                    "classified": True, "raw": raw}

    # Calendar: same strike, two expiries — "CCal 10 Jul 26 63000 / 31 Jul 26 63000"
    cal = re.findall(_DATE + r"\s+(\d+)", raw)
    if up.endswith("CAL") and len(cal) == 2:
        cp = "C" if up.startswith("C") else "P"
        legs = []
        for i, (d, mon, yy, k) in enumerate(cal):
            legs.append(_leg(cp, k, 1.0, None, compact_expiry(d, mon, yy)))
        return {"code": "CA", "legs": legs, "expiries": _uniq_exp(legs),
                "perp": False, "classified": True, "raw": raw}

    return {"code": up[:4], "legs": [], "expiries": [], "perp": False,
            "classified": False, "raw": raw}


def _uniq_exp(legs):
    out = []
    for l in legs:
        if l["expiry_c"] and l["expiry_c"] not in out:
            out.append(l["expiry_c"])
    return out


def _code_of(name_up: str) -> str:
    if name_up.startswith("STRADDLE"):
        return "ST"
    if name_up.startswith("STRANGLE"):
        return "SN"
    if name_up.startswith("RRCALL") or name_up.startswith("RRPUT") or name_up.startswith("RR"):
        return "RR"
    if name_up.startswith("ICONDOR") or name_up.startswith("CONDOR"):
        return "CO"
    if name_up.startswith("IFLY") or name_up.startswith("FLY") or name_up.startswith("BUTTERFLY"):
        return "BF"
    return name_up[:4]


def _named_legs(name_up: str, ks: list[int], ec: str):
    """Canonical leg geometry (strike + type + ratio) for a named structure.
    Signs are assigned later by apply_orientation() from the net-cash direction —
    here we set the *relative* sign pattern via `sign` = +1 (wing/long-side) or
    -1 (body/short-side) for a reference orientation.
    """
    def L(cp, k, sign, ratio=1.0):
        return _leg(cp, k, ratio, sign, ec)

    if name_up.startswith("STRADDLE") and len(ks) == 1:
        return [L("C", ks[0], +1), L("P", ks[0], +1)]
    if name_up.startswith("STRANGLE") and len(ks) == 2:
        lo, hi = sorted(ks)
        return [L("P", lo, +1), L("C", hi, +1)]
    if (name_up.startswith("RRCALL") or name_up.startswith("RRPUT")
            or name_up.startswith("RR")) and len(ks) == 2:
        lo, hi = sorted(ks)
        # RR: long the higher-strike call, short the lower-strike put (ref orientation)
        return [L("P", lo, -1), L("C", hi, +1)]
    if (name_up.startswith("ICONDOR") or name_up.startswith("CONDOR")) and len(ks) == 4:
        k1, k2, k3, k4 = sorted(ks)
        # iron condor: long wings (k1 put, k4 call), short body (k2 put, k3 call)
        return [L("P", k1, +1), L("P", k2, -1), L("C", k3, -1), L("C", k4, +1)]
    if (name_up.startswith("IFLY") or name_up.startswith("FLY")
            or name_up.startswith("BUTTERFLY")) and len(ks) == 3:
        k1, k2, k3 = sorted(ks)
        cp = "P" if name_up.startswith("IFLY") else "C"
        return [L(cp, k1, +1), L(cp, k2, -2), L(cp, k3, +1)]
    if ("SPREAD" in name_up or name_up in ("CS", "PS")) and len(ks) == 2:
        lo, hi = sorted(ks)
        cp = "P" if name_up.startswith("P") else "C"
        return [L(cp, lo, +1), L(cp, hi, -1)]
    return None


# ── direction / orientation ────────────────────────────────────────────────────

def net_cash(rows: list[dict]) -> float:
    """Signed premium the taker paid: +PRICE for BUY legs, −PRICE for SELL legs,
    scaled by QTY. >0 → net debit (taker paid). <0 → net credit (taker received)."""
    tot = 0.0
    for r in rows:
        px = _f(r.get("PRICE"))
        qty = _f(r.get("QTY")) or 1.0
        if px is None:
            continue
        side = (r.get("SIDE") or "").upper()
        tot += (px if side == "BUY" else -px) * qty
    return tot


# Per-structure REFERENCE leg signs (in _named_legs) correspond to a known cash
# orientation. We flip the whole structure iff the taker's actual cash sign differs.
#   True  → reference pattern is a net DEBIT (taker pays: long straddle, long fly, …)
#   False → reference pattern is a net CREDIT (taker receives: the sold iron condor)
_REF_IS_DEBIT = {"ST": True, "SN": True, "BF": True, "CO": False, "CS": True, "PS": True}


def apply_orientation(parsed: dict, rows: list[dict]) -> tuple[list[dict], str, bool]:
    """Assign each leg the taker's real sign and return (legs, side_label, reliable).

    - Cstm: signs are explicit in the description → always reliable.
    - Single-leg: the one row's SIDE is the sign (BUY→+1 long, SELL→−1 short) → reliable.
    - Named structures with a known cash-orientation (straddle/strangle/fly/condor/
      vertical): reference pattern flipped iff actual cash sign differs → reliable.
    - Risk reversals and anything else: NOT reliably sign-able from the tape alone
      (RR roles depend on the leg sides, and the taker may have sold it) → reliable
      is False; the caller shows per-leg greeks and lets the model net them.
    """
    legs = parsed["legs"]
    code = parsed["code"]
    nc = net_cash(rows)
    debit = nc > 0
    side = "Buyer" if debit else "Seller"

    if any(l.get("_explicit") for l in legs):            # Cstm — explicit signs
        return legs, side, True

    if code in ("CL", "PL") and len(legs) == 1:          # single leg — row SIDE is truth
        s = (rows[0].get("SIDE") or "BUY").upper() if rows else "BUY"
        legs[0]["sign"] = 1 if s == "BUY" else -1
        return legs, ("Buyer" if s == "BUY" else "Seller"), True

    if code in _REF_IS_DEBIT:                             # symmetric named structure
        if debit != _REF_IS_DEBIT[code]:
            for l in legs:
                l["sign"] = -l["sign"]
        return legs, side, True

    # RR, calendar, unknowns: signs not reliably derivable → defer greeks to model
    for l in legs:
        if l.get("sign") is None:
            l["sign"] = 1
    return legs, side, False


# ── greeks / offset ────────────────────────────────────────────────────────────

def net_greeks(legs: list[dict], greek_by_key: dict, qty: float) -> dict:
    """net_g = Σ (sign × ratio × per-leg greek) × qty, per greek.
    greek_by_key maps a leg key → {'delta','vega','gamma','theta'} (Deribit units:
    delta/gamma in coin, vega/theta in USD). Perp legs carry delta ±1."""
    out = {"delta": 0.0, "vega": 0.0, "gamma": 0.0, "theta": 0.0}
    have = False
    for l in legs:
        g = greek_by_key.get(leg_key(l))
        if not g:
            continue
        have = True
        s = l["sign"] * l["ratio"]
        for k in out:
            v = g.get(k)
            if v is not None:
                out[k] += s * float(v)
    if not have:
        return {}
    return {k: v * qty for k, v in out.items()}


def leg_key(l: dict) -> str:
    if l["cp"] == "FUT":
        return "FUT"
    return f"{l['cp']}:{int(round(l['strike']))}:{l['expiry_c']}"


def offset(price: float, ref: float, quote: str = "") -> dict:
    """Fill vs mark, unit chosen by PREMIUM MAGNITUDE (robust across venues):
      • coin-fraction premium (|ref| < 1, e.g. BTC 0.0131) → bps (×10000)
      • dollar-priced premium (|ref| ≥ 1, e.g. SOL $2.90 or a Paradex $140 net,
        even when QUOTE_CURRENCY says BTC) → percent
    A dollar premium × 10000 would print an absurd bps (the −324953 bug)."""
    if price is None or ref is None:
        return {"txt": "n/a", "sign": 0}
    d = price - ref
    sign = 1 if d > 0 else -1 if d < 0 else 0
    if abs(ref) < 1:                      # coin-denominated fraction
        bps = round(d * 10000, 1)
        return {"txt": f"{bps:+g} bps", "sign": sign, "val": bps, "unit": "bps"}
    pct = round(d / ref * 100, 1) if ref else None
    return {"txt": (f"{pct:+g}%" if pct is not None else "n/a"),
            "sign": sign, "val": pct, "unit": "%"}


def _f(v):
    try:
        if v in (None, "", "NULL"):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None
