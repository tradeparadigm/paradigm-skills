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
    # strikes = the trailing a/b/c/d group AFTER the date (2-digit alt strikes
    # like SOL 88/95 are valid; the position guard keeps the date's YY out).
    ks = []
    km = re.search(r"(\d{2,7}(?:\s*/\s*\d{2,7})*)\s*$", raw)
    if km and dm and km.start() >= dm.end():
        ks = [int(x) for x in re.split(r"\s*/\s*", km.group(1))]
    if dm and ks:
        d, mon, yy = dm.groups()
        ec = compact_expiry(d, mon, yy)
        nl = _named_legs(up, ks, ec)
        if nl is not None:
            legs, ref_is_debit = nl
            return {"code": _code_of(up), "legs": legs, "expiries": _uniq_exp(legs),
                    "perp": bool(re.search(r"Perp", raw, re.I)),
                    "ref_is_debit": ref_is_debit, "classified": True, "raw": raw}

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


_LEG_RE = re.compile(
    r"(?:([+-]?\d*\.?\d+)\s+)?(Call|Put|C|P)\s+(\d{1,2})\s+([A-Za-z]{3})\s+(\d{2})\s+(\d+)")


def extract_legs_generic(desc: str) -> list[dict]:
    """Best-effort: pull every `[±ratio] Type DD Mon YY Strike` leg out of ANY
    description, even one whose structure name we don't map. Used as a fallback so
    an unmapped-but-leg-listing structure still gets per-leg instruments fetched and
    correct data shown (signs kept only where the description states them; else None
    → the model assigns them). Returns [] when the description gives no explicit legs
    (e.g. a named structure that lists only strikes) — caller then shows the raw rows."""
    legs = []
    for m in _LEG_RE.finditer(desc or ""):
        r_, cp, d, mon, yy, k = m.groups()
        sign, ratio = None, 1.0
        if r_ is not None:
            ratio = abs(float(r_))
            sign = -1 if float(r_) < 0 else 1
        lg = _leg(cp, k, ratio, sign, compact_expiry(d, mon, yy))
        if sign is not None:
            lg["_explicit"] = True
        legs.append(lg)
    return legs


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
    if name_up.startswith("RR"):
        return "RR"
    if "CONDOR" in name_up:
        return "CO"
    if "FLY" in name_up or "BUTTERFLY" in name_up:
        return "BF"
    if "SPREAD" in name_up or name_up in ("CS", "PS", "CSPD", "PSPD"):
        return "PS" if name_up.startswith("P") else "CS"
    return name_up[:4]


def _named_legs(name_up: str, ks: list[int], ec: str):
    """Canonical leg geometry for a named structure, returned as
    (legs, ref_is_debit): the leg signs describe a REFERENCE orientation and
    `ref_is_debit` says whether that reference is a net debit (taker pays) or a
    net credit (taker receives). apply_orientation() flips the whole structure iff
    the taker's actual cash sign differs. `ref_is_debit` is structure-specific —
    e.g. a long iron condor is a CREDIT (sell near-money body, buy OTM wings), but a
    long call condor is a DEBIT (the low-strike call it buys is the expensive leg).
    """
    def L(cp, k, sign, ratio=1.0):
        return _leg(cp, k, ratio, sign, ec)

    if name_up.startswith("STRADDLE") and len(ks) == 1:
        return [L("C", ks[0], +1), L("P", ks[0], +1)], True          # long straddle = debit
    if name_up.startswith("STRANGLE") and len(ks) == 2:
        lo, hi = sorted(ks)
        return [L("P", lo, +1), L("C", hi, +1)], True                # long strangle = debit
    if name_up.startswith("RR") and len(ks) == 2:
        lo, hi = sorted(ks)                                          # (RR is reliable=False anyway)
        return [L("P", lo, -1), L("C", hi, +1)], True
    if name_up.startswith("ICONDOR") and len(ks) == 4:
        k1, k2, k3, k4 = sorted(ks)                                  # iron condor (2P+2C)
        return [L("P", k1, +1), L("P", k2, -1), L("C", k3, -1), L("C", k4, +1)], False  # long = credit
    if (name_up.startswith("CCONDOR") or name_up.startswith("PCONDOR")
            or name_up.startswith("CONDOR")) and len(ks) == 4:
        k1, k2, k3, k4 = sorted(ks)                                  # single-type condor (4 calls or 4 puts)
        cp = "P" if name_up.startswith("PCONDOR") else "C"
        return [L(cp, k1, +1), L(cp, k2, -1), L(cp, k3, -1), L(cp, k4, +1)], True  # long = debit
    if name_up.startswith("IFLY") and len(ks) == 3:
        k1, k2, k3 = sorted(ks)                                      # iron fly (2P+2C: put wing, straddle body, call wing)
        return [L("P", k1, +1), L("P", k2, -1), L("C", k2, -1), L("C", k3, +1)], False  # long = credit
    if ("FLY" in name_up or "BUTTERFLY" in name_up) and len(ks) == 3:
        k1, k2, k3 = sorted(ks)
        cp = "P" if name_up.startswith("PFLY") else "C"
        return [L(cp, k1, +1), L(cp, k2, -1, 2), L(cp, k3, +1)], True  # long fly = debit (body ×2)
    if "RATIO" in name_up:
        return None                                                  # ratio legs aren't 1:1 → defer (unmapped)
    if ("SPREAD" in name_up or name_up in ("CS", "PS", "CSPD", "PSPD")) and len(ks) == 2:
        lo, hi = sorted(ks)
        cp = "P" if name_up.startswith("P") else "C"
        # [+lo, -hi] is a debit for calls (lo call is the dear leg) but a CREDIT
        # for puts (hi put is the dear leg) — ref_is_debit must follow the type.
        return [L(cp, lo, +1), L(cp, hi, -1)], cp == "C"
    return None


def legs_from_rows(rows: list[dict]):
    """When the tape has one row PER LEG (each a single-leg option DESCRIPTION, or a
    perp/future row), build the legs with each leg's sign taken straight from its own
    SIDE (BUY→+1 long, SELL→−1 short) — fully reliable, no convention guessing. This
    covers risk reversals, spreads, calendars, and option+perp combos whose legs are
    stored as separate rows. Returns None if the rows aren't in per-leg shape (then
    the caller parses the combined DESCRIPTION instead)."""
    if not rows or len(rows) < 2:
        return None
    out = []
    for r in rows:
        pr = parse_product(r.get("PRODUCT", ""))
        sgn = 1 if (r.get("SIDE") or "BUY").upper() == "BUY" else -1
        if pr["kind"] in ("PERPETUAL", "FUTURE"):
            out.append({"cp": "FUT", "strike": 0.0, "ratio": 1.0, "sign": sgn,
                        "expiry_c": None, "_row": r})
            continue
        d = parse_description(r.get("DESCRIPTION", ""))
        if d["classified"] and len(d["legs"]) == 1 and d["code"] in ("CL", "PL"):
            lg = d["legs"][0]
            lg["sign"] = sgn
            lg["_row"] = r
            out.append(lg)
        else:
            return None                      # a combined-DESCRIPTION block, not per-leg
    return out


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

    refdeb = parsed.get("ref_is_debit")
    if code != "RR" and refdeb is not None and legs:    # named structure w/ known orientation
        if debit != refdeb:                              # flip whole structure to actual side
            for l in legs:
                l["sign"] = -l["sign"]
        return legs, side, True

    # RR, calendar, unclassified: signs not reliably derivable → defer greeks to model
    for l in legs:
        if l.get("sign") is None:
            l["sign"] = 1
    return legs, side, False


# ── greeks / offset ────────────────────────────────────────────────────────────

def net_greeks(legs: list[dict], greek_by_key: dict, qty: float) -> dict:
    """net_g = Σ (sign × ratio × per-leg greek) × qty, per greek.
    greek_by_key maps a leg key → {'delta','vega','gamma','theta'} (Deribit units:
    delta/gamma in coin, vega/theta in USD). Perp legs carry delta ±1.
    Returns {} unless EVERY leg has greeks — a partial sum is a silently wrong
    net, so a missing leg degrades to the per-leg ⚠ display instead."""
    if not legs or any(not greek_by_key.get(leg_key(l)) for l in legs):
        return {}
    out = {"delta": 0.0, "vega": 0.0, "gamma": 0.0, "theta": 0.0}
    for l in legs:
        g = greek_by_key[leg_key(l)]
        s = l["sign"] * l["ratio"]
        for k in out:
            v = g.get(k)
            if v is not None:
                out[k] += s * float(v)
    return {k: v * qty for k, v in out.items()}


def leg_key(l: dict) -> str:
    if l["cp"] == "FUT":
        return "FUT"
    return f"{l['cp']}:{int(round(l['strike']))}:{l['expiry_c']}"


_STABLE_QUOTES = {"USD", "USDC", "USDT"}


def offset(price: float, ref: float, quote: str = "") -> dict:
    """Fill vs mark, unit chosen by QUOTE CURRENCY first, magnitude second:
      • stable/fiat quote (USD/USDC/USDT — dollar prices, incl. a $0.50 alt
        option) → percent; ×10000 on a dollar price is the −324953-bps bug.
      • coin quote (BTC/ETH/SOL…) with a fraction premium (|ref| < 1, e.g.
        BTC 0.0131) → bps (×10000).
      • coin quote with |ref| ≥ 1 → the price is actually dollars (Paradex
        labels $ nets 'BTC') → percent."""
    if price is None or ref is None:
        return {"txt": "n/a", "sign": 0}
    d = price - ref
    sign = 1 if d > 0 else -1 if d < 0 else 0
    if (quote or "").upper() not in _STABLE_QUOTES and abs(ref) < 1:
        bps = round(d * 10000, 1)         # coin-denominated fraction
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
