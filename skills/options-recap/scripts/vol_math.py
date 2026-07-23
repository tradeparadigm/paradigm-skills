"""
vol_math.py — deterministic vol calculations for the options-recap skill.

Single source of truth for the math the agent must NOT do by mental arithmetic:
realized volatility and Black-76 flow greeks. Both the production CLI
(recap.py) and the eval fixture generator (../evals/
generate_fixture.py) import from here, so the formula never forks.

Pure functions, no I/O, no network — unit-tested in test_vol_math.py.
"""

import math
from collections import defaultdict

# Crypto trades 24/7, so the calendar annualization factor is √(24×365).
HOURS_PER_YEAR = 24 * 365  # 8760
RV_LOOKBACK_DAYS = 7        # paired with DVOL's 30-day implied; desk standard

_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}


# ── Realized vol (#1) ──────────────────────────────────────────────────────

def compute_realized_vol(closes: list[float]) -> dict:
    """Close-to-close realized vol from hourly closes, annualized (24/7).

    Returns annualized vol in vol points (%). Realized-vs-implied is a slow
    statistic, so callers should pass a fixed multi-day lookback (see
    RV_LOOKBACK_DAYS), not a short recap window.
    """
    if not closes or len(closes) < 3:
        return {"annualized_vol": None, "candles": len(closes or [])}
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)   # sample variance
    sd_hourly = math.sqrt(var)
    rv = sd_hourly * math.sqrt(HOURS_PER_YEAR) * 100
    return {
        "annualized_vol": round(rv, 1),
        "candles": len(closes),
        "lookback_days": RV_LOOKBACK_DAYS,
        "method": "close-to-close log returns · sample stdev · ×√8760 (24/7) · ×100",
    }


def realized_vs_implied(closes: list[float], dvol_close: float | None) -> dict:
    """Realized vol + the vol risk premium read (implied − realized)."""
    rv = compute_realized_vol(closes)
    value = rv["annualized_vol"]
    vrp = None
    label = None
    if value is not None and dvol_close is not None:
        # Difference of the DISPLAY-rounded figures, so the rendered Snapshot
        # reconciles line-to-line (DVOL 36.4 − RV 33.1 = VRP +3.3, not +3.2 from
        # the unrounded inputs — a 0.1v mismatch readers flag as an error).
        vrp = round(round(dvol_close, 1) - value, 1)
        if vrp > 1:
            label = "implied rich vs realized — vol overpriced vs delivered"
        elif vrp < -1:
            label = "implied cheap vs realized — vol underpriced vs delivered"
        else:
            label = "implied roughly in line with realized"
    return {
        "value": value,
        "lookback_days": rv.get("lookback_days"),
        "vrp": vrp,
        "vrp_label": label,
    }


# ── Flow greeks (#2) ───────────────────────────────────────────────────────

def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def expiry_ms_from_instrument(inst: str) -> int | None:
    """Parse Deribit expiry (08:00 UTC) from an instrument name like
    BTC-26JUN26-55000-P → 2026-06-26T08:00Z in ms."""
    from datetime import datetime, timezone
    try:
        token = inst.split("-")[1]            # e.g. 26JUN26 or 5JUN26
        day = int(token[:-5])
        mon = _MONTHS[token[-5:-2]]
        yr = 2000 + int(token[-2:])
        return int(datetime(yr, mon, day, 8, 0, tzinfo=timezone.utc).timestamp() * 1000)
    except Exception:
        return None


def black76_greeks(F: float, K: float, T_years: float, iv_pct: float) -> dict:
    """Approximate per-contract vega and dollar-gamma via Black-76 (r=0,
    options on the index). Used to weight option flow by its vol / convexity
    exposure so a multi-leg structure nets correctly across tenors and strikes.

    - vega: USD change in option value per 1 vol-point (1%) move, per 1 BTC contract.
    - dollar_gamma: USD delta change per 1% spot move, per 1 BTC contract.

    Directional SIGN and RELATIVE magnitude across legs are robust; the
    absolute USD figure is approximate (ignores inverse-settlement nuance).
    """
    sigma = iv_pct / 100.0
    if T_years <= 0 or sigma <= 0 or F <= 0 or K <= 0:
        return {"vega": 0.0, "dollar_gamma": 0.0}
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T_years) / (sigma * math.sqrt(T_years))
    pdf = _norm_pdf(d1)
    vega = F * pdf * math.sqrt(T_years) / 100.0       # per 1 vol point, per contract
    gamma = pdf / (F * sigma * math.sqrt(T_years))    # ∂²price/∂F²
    dollar_gamma = gamma * F * F * 0.01               # per 1% spot move
    return {"vega": vega, "dollar_gamma": dollar_gamma}


def compute_flow_greeks(clusters: dict[str, list]) -> dict:
    """Aggregate signed vega / gamma across all block legs to read net
    customer (and therefore dealer) positioning.

    `clusters` maps block_trade_id → list of legs; each leg needs
    instrument_name, index_price, iv, timestamp, direction, amount.

    A customer who BUYS an option is long vega/gamma (sign +1); selling is -1.
    Dealers take the other side, so dealer exposure = −customer exposure.
    """
    net_vega = 0.0          # net CUSTOMER vega ($/vol-pt)
    net_dgamma = 0.0        # net CUSTOMER dollar-gamma ($/1% move)
    gross_vega = 0.0
    for legs in clusters.values():
        for leg in legs:
            parts = leg["instrument_name"].split("-")
            if len(parts) < 4:
                continue
            K = int(parts[2])
            F = leg.get("index_price")
            iv = leg.get("iv")
            exp_ms = expiry_ms_from_instrument(leg["instrument_name"])
            if not (F and iv and exp_ms):
                continue
            T = (exp_ms - leg["timestamp"]) / (HOURS_PER_YEAR * 3600_000)
            g = black76_greeks(F, K, T, iv)
            sign = 1.0 if leg["direction"] == "buy" else -1.0
            qty = leg["amount"]
            net_vega += sign * qty * g["vega"]
            net_dgamma += sign * qty * g["dollar_gamma"]
            gross_vega += qty * g["vega"]

    # Dealer is the opposite side of customer flow.
    dealer_vega = -net_vega
    dealer_dgamma = -net_dgamma

    # "Balanced" when net is small relative to gross vega traded.
    balanced = gross_vega > 0 and abs(net_vega) / gross_vega < 0.2

    if balanced:
        label = "two-way / vega-balanced — no decisive net positioning"
    else:
        vega_dir = "short vega (vulnerable to a vol spike)" if dealer_vega < 0 else "long vega"
        gamma_dir = ("short gamma → chase spot, amplify moves"
                     if dealer_dgamma < 0 else "long gamma → dampen, expect pinning")
        label = f"dealers {vega_dir}; {gamma_dir}"

    return {
        "net_customer_vega": round(net_vega),
        "net_customer_dollar_gamma": round(net_dgamma),
        "dealer_vega": round(dealer_vega),
        "dealer_dollar_gamma": round(dealer_dgamma),
        "gross_vega": round(gross_vega),
        "balanced": balanced,
        "positioning_label": label,
    }


def cluster_blocks(trades: list[dict]) -> dict[str, list]:
    """Group trades by block_trade_id (legs of the same block)."""
    clusters: dict[str, list] = defaultdict(list)
    for t in trades:
        bid = t.get("block_trade_id")
        if bid:
            clusters[bid].append(t)
    return dict(clusters)


# ── Block structures (#2b) ─────────────────────────────────────────────────

_FLY_RTOL = 0.05      # relative tolerance for ratio comparisons on float amounts


def _same_sign(a: float, b: float) -> bool:
    """True when both are strictly nonzero and share a sign."""
    return a != 0 and b != 0 and (a > 0) == (b > 0)


def _is_fly(q: list[float]) -> bool:
    """Net signed quantities on 3 ascending strikes form a 1:−2:1 fly (either
    polarity): wings same sign and ~equal, middle opposite with |mid| ≈ sum of
    wings. Broken-wing (uneven strike spacing) shares this ratio, so no spacing
    check — the ratio alone is the test."""
    q1, q2, q3 = q
    if q1 == 0 or q2 == 0 or q3 == 0:
        return False
    if not _same_sign(q1, q3) or _same_sign(q1, q2):
        return False
    return (math.isclose(abs(q1), abs(q3), rel_tol=_FLY_RTOL)
            and math.isclose(abs(q2), abs(q1) + abs(q3), rel_tol=_FLY_RTOL))


def _is_condor(q: list[float]) -> bool:
    """Net signed quantities on 4 ascending strikes form +q/−q/−q/+q (either
    polarity): outer pair same sign, inner pair the opposite sign, all ~equal
    magnitude."""
    q1, q2, q3, q4 = q
    if any(x == 0 for x in q):
        return False
    if not (_same_sign(q1, q4) and _same_sign(q2, q3)) or _same_sign(q1, q2):
        return False
    m0 = abs(q1)
    return all(math.isclose(abs(x), m0, rel_tol=_FLY_RTOL) for x in q)


def classify_structure(legs: list[dict]) -> str:
    """Name a block cluster's structure from its leg instruments and, where it
    disambiguates, the disclosed per-leg directions. Legs are first consolidated
    per instrument into a net signed quantity (+amount buy, −amount sell) so
    ratio patterns survive multiple prints at one strike.

    Labels follow the DRFQ StrategyCodeEnum vocabulary (rfq-trader
    references/instruments.md): Butterfly family (never "Fly"), typed
    calendars, and "Custom" (DRFQ code CM) for any package we can't name.

    Same expiry, ≥3 legs (runs before the 2-leg C&P branch, else a 4-leg iron
    butterfly reads as a Risk Reversal):
      • one type, 3 strikes → Call/Put Butterfly ONLY when directions are all
        disclosed and the net quantities on ascending strikes form the 1:−2:1
        fly ratio (broken wings count); ladders/strips/ratios → Custom.
      • one type, 4 strikes → Call/Put Condor ONLY when disclosed and the nets
        form +q/−q/−q/+q with equal magnitudes; else Custom.
      • both types, 4 strikes, 2 calls & 2 puts → Iron Condor.
      • both types, 3 strikes with a C AND a P on the middle strike → Iron
        Butterfly ONLY when the low-strike wing is puts-only, the high-strike
        wing is calls-only, wing/body sizes are ~equal, and (when disclosed)
        the body legs share one direction and the wings the opposite; else
        Custom.
      • anything else → Custom.
    Same expiry, 2 legs:
      • C&P same strike → Combo (synthetic forward) when directions are
        disclosed and opposite; Straddle otherwise (disclosed-and-equal, or
        undisclosed — the common case).
      • C&P diff strikes → Strangle (disclosed, all same direction), Risk
        Reversal (disclosed, differ), else Strangle/RR.
      • one type, diff strikes → Call/Put Spread.
    Multi-expiry:
      • single strike → Call/Put/(mixed→)Calendar; when directions are all
        disclosed it must be long one expiry / short the other (≥1 buy AND ≥1
        sell) — an all-same-direction time strip → Custom.
      • 2 legs, diff strikes → Call/Put Diagonal (same long/short requirement;
        one call + one put is not a diagonal → Custom).
      • anything else → Custom."""
    if len(legs) == 1:
        return "Call" if legs[0]["instrument_name"].endswith("-C") else "Put"
    expiries, strikes, types = set(), set(), set()
    pairs = []                    # (strike, type) per leg
    net: dict[tuple, float] = defaultdict(float)   # signed qty per (strike,type)
    amt: dict[tuple, float] = defaultdict(float)   # unsigned qty per (strike,type)
    for leg in legs:
        parts = leg["instrument_name"].split("-")
        k, t = int(parts[2]), parts[3]
        expiries.add(parts[1])
        strikes.add(k)
        types.add(t)
        pairs.append((k, t))
        a = leg.get("amount") or 0
        d = leg.get("direction")
        net[(k, t)] += a if d == "buy" else -a if d == "sell" else 0
        amt[(k, t)] += a
    dirs = [leg.get("direction") for leg in legs]
    disclosed = all(d in ("buy", "sell") for d in dirs)
    has_buy = any(d == "buy" for d in dirs)
    has_sell = any(d == "sell" for d in dirs)

    # Multi-expiry. Calendars/diagonals are a long-one-tenor / short-the-other
    # trade: when every direction is disclosed, require both a buy and a sell;
    # an all-same-direction package (time strip) is Custom.
    if len(expiries) > 1:
        if len(strikes) == 1:
            label = ("Call Calendar" if types == {"C"}
                     else "Put Calendar" if types == {"P"} else "Calendar")
            if disclosed and not (has_buy and has_sell):
                return "Custom"
            return label
        if len(legs) == 2:
            if types == {"C"}:
                label = "Call Diagonal"
            elif types == {"P"}:
                label = "Put Diagonal"
            else:
                return "Custom"   # one call + one put across expiries isn't a diagonal
            if disclosed and not (has_buy and has_sell):
                return "Custom"
            return label
        return "Custom"

    # Same expiry.
    if len(legs) >= 3:
        n_strikes = len(strikes)
        sk = sorted(strikes)
        if len(types) == 1:
            base = "Call" if types == {"C"} else "Put"
            t0 = "C" if types == {"C"} else "P"
            if not disclosed:
                return "Custom"   # net-ratio patterns need disclosed signs
            if n_strikes == 3 and _is_fly([net[(k, t0)] for k in sk]):
                return f"{base} Butterfly"
            if n_strikes == 4 and _is_condor([net[(k, t0)] for k in sk]):
                return f"{base} Condor"
            return "Custom"
        if types == {"C", "P"}:
            n_calls = sum(1 for _, t in pairs if t == "C")
            n_puts = sum(1 for _, t in pairs if t == "P")
            if n_strikes == 4 and n_calls == 2 and n_puts == 2:
                return "Iron Condor"
            if n_strikes == 3:
                low, mid, high = sk[0], sk[1], sk[2]
                low_types = {t for (k, t) in pairs if k == low}
                high_types = {t for (k, t) in pairs if k == high}
                body_dirs = {leg.get("direction") for leg in legs
                             if int(leg["instrument_name"].split("-")[2]) == mid}
                wing_dirs = {leg.get("direction") for leg in legs
                             if int(leg["instrument_name"].split("-")[2]) in (low, high)}
                is_ironfly = (
                    (mid, "C") in pairs and (mid, "P") in pairs
                    and low_types == {"P"} and high_types == {"C"}
                )
                if is_ironfly:
                    sizes = [amt[(low, "P")], amt[(mid, "C")],
                             amt[(mid, "P")], amt[(high, "C")]]
                    ratio_ok = all(math.isclose(s, sizes[0], rel_tol=_FLY_RTOL)
                                   for s in sizes)
                    dir_ok = (not disclosed) or (
                        len(body_dirs) == 1 and len(wing_dirs) == 1
                        and body_dirs != wing_dirs)
                    if ratio_ok and dir_ok:
                        return "Iron Butterfly"
        return "Custom"
    if types == {"C", "P"} and len(strikes) == 1:
        # C&P at one strike: opposite disclosed directions = synthetic forward
        # (Combo, matching block-analyst); same or undisclosed = Straddle.
        if disclosed and len(set(dirs)) > 1:
            return "Combo"
        return "Straddle"
    if types == {"C", "P"} and len(strikes) > 1:
        if disclosed:
            return "Strangle" if len(set(dirs)) == 1 else "Risk Reversal"
        return "Strangle/RR"
    if len(types) == 1 and len(strikes) > 1:
        return "Call Spread" if types == {"C"} else "Put Spread"
    return "Custom"


def dominant_side(legs: list[dict]) -> str:
    """Buy, Sell, or Mixed for a block cluster (by leg direction count).

    "Mixed" means the structure's legs point both ways (every spread does) —
    NOT that the aggressor is unknown. Never render it as "two-way": on a
    block desk that word means the taker side is undisclosed, and the per-leg
    direction field here is disclosed."""
    buys = sum(1 for l in legs if l["direction"] == "buy")
    sells = len(legs) - buys
    if buys == 0:
        return "Sell"
    if sells == 0:
        return "Buy"
    return "Mixed"


def summarize_blocks(clusters: dict[str, list], top_n: int = 8,
                     min_btc: float = 10.0) -> list[dict]:
    """Rank block clusters by USD notional and describe each.

    Identifying the largest block and its structure is deterministic (cluster
    by block_trade_id, sum size × index, classify legs) — exactly the read an
    LLM gets wrong by eyeballing raw tape (mis-ranking, hallucinated notional),
    so it belongs here next to the vol math. Returns the top `top_n` clusters
    of at least `min_btc` total size, largest notional first.
    """
    from datetime import datetime, timezone
    rows = []
    for bid, legs in clusters.items():
        if not legs:
            continue
        total_btc = sum(l.get("amount", 0) for l in legs)
        index_price = legs[0].get("index_price") or 0
        # Size-weight avg_iv by leg amount (matching aggregate_clips), so a
        # small far-OTM leg can't drag the package IV around; unweighted leg
        # means over-counted the tiny legs.
        iv_num = sum(l["iv"] * (l.get("amount") or 0)
                     for l in legs if l.get("iv") is not None)
        iv_den = sum((l.get("amount") or 0)
                     for l in legs if l.get("iv") is not None)
        ts = min(l["timestamp"] for l in legs)
        dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        # Structure UNIT size for display: the base leg of the package (per-
        # instrument sums, then min — the ratio-1 leg). A 4×63-lot iron fly is
        # a 63x structure, not "252x"; leg-sum stays in size_btc for the
        # min-size floor and IV weighting, where gross traded size is the point.
        by_inst: dict[str, float] = defaultdict(float)
        for l in legs:
            by_inst[l["instrument_name"]] += l.get("amount", 0)
        unit = min(by_inst.values()) if by_inst else 0
        # Expiry label: chronological; a multi-expiry structure (calendar,
        # diagonal, cross-expiry package) names its near AND far tenor. Leg
        # order is tape order, so a legs[0]-based label was nondeterministic —
        # identical structures could render under different expiries.
        # "/" only when the pair IS the complete expiry set; with interior
        # tenors elided (>2 expiries) use "→" so the label reads as a range,
        # not an enumeration that contradicts the Detail column's legs.
        exps = sorted({l["instrument_name"].split("-")[1] for l in legs
                       if len(l["instrument_name"].split("-")) > 1},
                      key=lambda e: expiry_ms_from_instrument(f"X-{e}-0-C") or 0)
        if not exps:
            expiry = None
        elif len(exps) == 1:
            expiry = exps[0]
        else:
            expiry = f"{exps[0]}{'/' if len(exps) == 2 else '→'}{exps[-1]}"
        parts = legs[0]["instrument_name"].split("-")
        rows.append({
            "block_trade_id": bid,
            "time_utc": dt.strftime("%H:%M"),
            "structure": classify_structure(legs),
            "size_btc": round(total_btc, 1),
            "unit_size": round(unit, 1),
            "notional_usd": round(total_btc * index_price),
            "side": dominant_side(legs),
            "avg_iv": round(iv_num / iv_den, 1) if iv_den else None,
            "expiry": expiry,
            "strike": parts[2] if len(parts) > 2 else None,
            "leg_count": len(legs),
        })
    rows.sort(key=lambda r: r["notional_usd"], reverse=True)
    return [r for r in rows if r["size_btc"] >= min_btc][:top_n]


def clip_signature(legs: list[dict]) -> tuple:
    """Structure signature for clip detection: leg instruments, directions,
    and the leg-size *ratio* (amounts normalized by the smallest leg).
    Sequential prints of one order being worked differ only in absolute size,
    so its clips share a signature; distinct structures don't."""
    amts = [l.get("amount") or 0 for l in legs]
    base = min((a for a in amts if a), default=1)
    return tuple(sorted(
        (l["instrument_name"], l.get("direction"),
         round((l.get("amount") or 0) / base, 2))
        for l in legs
    ))


def aggregate_clips(ranked: list[dict], clusters: dict[str, list]) -> list[dict]:
    """Merge ranked blocks that share a clip signature into one entry.

    Without this, one order worked in clips floods the top-N table with
    near-duplicate rows and crowds out distinct flow. Each merged entry keeps
    the largest clip's block_trade_id (ranked arrives notional-desc, so the
    first seen is the largest), sums size/notional, size-weights the IV, takes
    the earliest time, and carries `clip_count`. Returns entries sorted by
    combined notional."""
    groups: dict[tuple, dict] = {}
    for b in ranked:
        legs = clusters.get(b["block_trade_id"]) or []
        sig = clip_signature(legs) if legs else ("solo", b["block_trade_id"])
        g = groups.get(sig)
        if g is None:
            groups[sig] = dict(
                b, clip_count=1, iv_lo=b["avg_iv"], iv_hi=b["avg_iv"],
                _iv_num=(b["avg_iv"] or 0) * b["size_btc"],
                _iv_den=b["size_btc"] if b["avg_iv"] is not None else 0,
            )
            continue
        g["clip_count"] += 1
        g["size_btc"] = round(g["size_btc"] + b["size_btc"], 1)
        g["unit_size"] = round(g.get("unit_size", 0) + b.get("unit_size", 0), 1)
        g["notional_usd"] += b["notional_usd"]
        g["time_utc"] = min(g["time_utc"], b["time_utc"])
        if b["avg_iv"] is not None:
            g["iv_lo"] = b["avg_iv"] if g["iv_lo"] is None else min(g["iv_lo"], b["avg_iv"])
            g["iv_hi"] = b["avg_iv"] if g["iv_hi"] is None else max(g["iv_hi"], b["avg_iv"])
            g["_iv_num"] += b["avg_iv"] * b["size_btc"]
            g["_iv_den"] += b["size_btc"]
    out = []
    for g in groups.values():
        den = g.pop("_iv_den")
        num = g.pop("_iv_num")
        g["avg_iv"] = round(num / den, 1) if den else None
        out.append(g)
    out.sort(key=lambda r: r["notional_usd"], reverse=True)
    return out


# ── Vol surface (#3) ───────────────────────────────────────────────────────

def _interp(points: list[tuple], x: float) -> tuple:
    """Linear interpolate y at x over points = sorted [(x_i, y_i)] ascending.
    Returns (y, extrapolated) — extrapolated=True when x is outside the
    observed range and the nearest endpoint was clamped to."""
    if not points:
        return None, True
    if x <= points[0][0]:
        return points[0][1], x < points[0][0]
    if x >= points[-1][0]:
        return points[-1][1], x > points[-1][0]
    for i in range(1, len(points)):
        x0, y0 = points[i - 1]
        x1, y1 = points[i]
        if x0 <= x <= x1:
            if x1 == x0:
                return y0, False
            return y0 + (x - x0) / (x1 - x0) * (y1 - y0), False
    return points[-1][1], True


def _call_delta(inst: str, delta: float) -> float | None:
    """Normalize a leg's delta to the CALL delta for its strike.
    Put delta = call delta − 1, so call delta = put delta + 1."""
    if delta is None:
        return None
    if inst.endswith("-C"):
        return delta
    if inst.endswith("-P"):
        return delta + 1.0
    return None


def compute_vol_surface(tickers: dict[str, dict], spot: float | None = None,
                        max_expiries: int | None = None) -> dict:
    """Derive per-expiry ATM IV, 25-delta risk reversal (skew), 25-delta
    butterfly (wings), and the cross-expiry term-structure read from raw
    per-strike tickers (each carrying `mark_iv` and `delta`).

    Interpolates IV against call-delta: 25Δ call = delta 0.25, 25Δ put =
    delta 0.75 (same strike, put delta −0.25), ATM = 0.50. Metrics whose
    target delta falls outside the strike range are flagged `extrapolated`.

    `max_expiries` truncates the (chronologically sorted) curve before the
    term read — pass the display cap so the term label describes the tenors
    the reader actually sees, not invisible back-month ones.
    """
    # Build per-expiry { call_delta: iv } (call & put at a strike share mark_iv).
    by_exp: dict[str, dict[float, float]] = defaultdict(dict)
    exp_ms: dict[str, int | None] = {}
    for name, d in tickers.items():
        iv = d.get("mark_iv")
        cd = _call_delta(name, d.get("delta"))
        if iv is None or cd is None:
            continue
        exp = name.split("-")[1]
        by_exp[exp][round(cd, 6)] = iv
        exp_ms.setdefault(exp, expiry_ms_from_instrument(name))

    expiries = []
    for exp, dmap in by_exp.items():
        pts = sorted(dmap.items())  # [(call_delta, iv)] ascending
        atm, atm_ex = _interp(pts, 0.50)
        c25, c25_ex = _interp(pts, 0.25)   # 25Δ call (OTM call)
        p25, p25_ex = _interp(pts, 0.75)   # 25Δ put  (OTM put)
        rr = fly = None
        if c25 is not None and p25 is not None:
            rr = round(c25 - p25, 1)                       # >0 calls bid, <0 puts bid
        if c25 is not None and p25 is not None and atm is not None:
            fly = round((c25 + p25) / 2 - atm, 1)          # >0 wings bid
        expiries.append({
            "expiry": exp,
            "expiry_ms": exp_ms.get(exp),
            "atm_iv": round(atm, 1) if atm is not None else None,
            "rr_25d": rr,
            "fly_25d": fly,
            "wings_extrapolated": bool(c25_ex or p25_ex),
        })

    # Chronological order (unknown expiry_ms sorts last).
    expiries.sort(key=lambda e: (e["expiry_ms"] is None, e["expiry_ms"] or 0))
    if max_expiries:
        expiries = expiries[:max_expiries]

    front = expiries[0] if expiries else None

    # Term structure reads the WHOLE curve, front to last expiry — a two-point
    # front-vs-next comparison calls a humped curve (up then down) "contango".
    # A counter-move ≤ TERM_TOL doesn't break monotonicity (surface noise); the
    # ±1v span gate keeps genuinely shallow slopes labeled "flat".
    TERM_TOL = 0.2
    atm_pts = [e for e in expiries if e["atm_iv"] is not None]
    atms = [e["atm_iv"] for e in atm_pts]
    front_atm = atms[0] if atms else None
    back_atm = atms[-1] if atms else None

    # Labels are the CONTRACT tokens from references/output-format.md, verbatim —
    # no explanatory suffixes ("(35.2v)", "non-monotonic", "downside skew"). The
    # recap template is fixed; embellishments here render as template drift.
    term = None
    if len(atms) >= 2:
        up = all(b - a >= -TERM_TOL for a, b in zip(atms, atms[1:]))
        down = all(b - a <= TERM_TOL for a, b in zip(atms, atms[1:]))
        span = atms[-1] - atms[0]
        if up and not down and span > 1:
            term = "contango"
        elif down and not up and span < -1:
            term = "backwardation"
        elif up or down:
            term = "flat"
        else:
            peak = max(atm_pts, key=lambda e: e["atm_iv"])
            trough = min(atm_pts, key=lambda e: e["atm_iv"])
            if peak is not atm_pts[0] and peak is not atm_pts[-1]:
                term = f"humped — peak at {peak['expiry']}"
            elif trough is not atm_pts[0] and trough is not atm_pts[-1]:
                term = f"dished — trough at {trough['expiry']}"
            else:
                term = "mixed"

    skew = None
    if front and front["rr_25d"] is not None:
        rr = front["rr_25d"]
        side = "puts bid" if rr < 0 else "calls bid" if rr > 0 else "flat"
        # Wing extrapolation is flagged the same way as table cells: a star on
        # the figure, not prose.
        star = "*" if front["wings_extrapolated"] else ""
        skew = f"front 25Δ RR {rr:+}v{star} → {side}"

    return {
        "spot": spot,
        "expiries": expiries,
        "front_atm": front_atm,
        "back_atm": back_atm,
        "term_structure": term,
        "skew_label": skew,
    }
