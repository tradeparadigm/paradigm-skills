#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
recap.py — single-call orchestrator for the options recap.

ONE invocation does the entire recap: it fetches the Deribit tape (7d closes +
window option trades with concurrent, time-sliced pagination — no serial
backfill), ingests the hot-surface CSVs the DuckDB step wrote, runs the vol
math (realized-vs-implied, block clustering/ranking, vol-surface skew/term),
and prints ONE JSON object whose fields map 1:1 to the four output sections.

The agent runs this once and renders the four sections from the JSON. It must
not paginate, merge, cluster, or hand-assemble a snapshot — all of that is here.

Pipeline (concurrent where independent):
  • Deribit 7d hourly closes        → realized vol
  • Deribit window option trades    → biggest print + block flow leg geometry
  • hot CSVs in --csv-dir (DuckDB)  → DVOL/spot OHLC, volume+P/C, block totals,
                                      vol surface (markIV/delta per strike)

Hot CSVs are authoritative for DVOL/spot/volume/surface. Deribit is used only
for the 7d realized-vol input and block leg detail (hot never carries those).

Usage:
    uv run scripts/recap.py --asset btc --window 8h --csv-dir /tmp/recap
    uv run scripts/recap.py --asset btc --window 8h --no-s3   # local: Deribit-only
    uv run scripts/recap.py ... --pretty

Output (stdout, JSON): {header, snapshot, biggest_print, block_flow, vol_surface, warnings}
On any single-source failure the affected fields are null and a line is added to
`warnings`; the process still exits 0 with a renderable object.
"""

import argparse
import csv
import json
import os
import sys
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(__file__))
from vol_math import (  # noqa: E402
    realized_vs_implied,
    cluster_blocks,
    summarize_blocks,
    compute_vol_surface,
    classify_structure,
    dominant_side,
    RV_LOOKBACK_DAYS,
)

DERIBIT = "https://www.deribit.com/api/v2/public"
WARNINGS: list[str] = []


def warn(msg: str) -> None:
    WARNINGS.append(msg)


def parse_window_ms(window: str) -> int:
    w = window.strip().lower()
    if w == "1d":
        return 24 * 3600_000
    units = {"m": 60_000, "h": 3600_000, "d": 86400_000}
    unit = w[-1]
    if unit not in units:
        raise ValueError(f"bad window '{window}' — use 5m/1h/4h/8h/24h/1d")
    return int(w[:-1]) * units[unit]


# ── Deribit (public API, no auth) ───────────────────────────────────────────

def _get(path: str, params: dict, timeout: int = 15) -> dict:
    url = f"{DERIBIT}/{path}?{urlencode(params)}"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        data = json.loads(r.read())
    if "error" in data:
        raise RuntimeError(f"Deribit {path}: {data['error']}")
    return data["result"]


def fetch_7d_closes(asset: str, end_ms: int) -> list[float]:
    start_ms = end_ms - RV_LOOKBACK_DAYS * 86400_000
    res = _get("get_tradingview_chart_data", {
        "instrument_name": f"{asset}-PERPETUAL", "resolution": "60",
        "start_timestamp": start_ms, "end_timestamp": end_ms,
    })
    return res.get("close") or []


def _fetch_trade_slice(asset: str, start_ms: int, end_ms: int, depth: int = 0) -> list[dict]:
    """Fetch every option trade in [start_ms, end_ms]. Deribit caps a response
    at 1000 trades; when a slice overflows (`has_more`) we bisect it and fetch
    the halves concurrently. Time-slicing removes the serial cursor dependency
    that made the old runbook backfill page-by-page."""
    res = _get("get_last_trades_by_currency", {
        "currency": asset, "kind": "option", "count": 1000,
        "start_timestamp": start_ms, "end_timestamp": end_ms, "sorting": "desc",
    })
    trades = res.get("trades") or []
    if res.get("has_more") and depth < 8 and end_ms - start_ms > 60_000:
        mid = (start_ms + end_ms) // 2
        with ThreadPoolExecutor(max_workers=2) as ex:
            halves = list(ex.map(
                lambda se: _fetch_trade_slice(asset, se[0], se[1], depth + 1),
                [(start_ms, mid), (mid + 1, end_ms)],
            ))
        for h in halves:
            trades.extend(h)
    return trades


def fetch_window_trades(asset: str, start_ms: int, end_ms: int) -> list[dict]:
    """Concurrent, time-sliced fetch of the whole window, deduped by trade_id."""
    span_h = (end_ms - start_ms) / 3600_000
    n_slices = 1 if span_h <= 2 else min(24, max(2, round(span_h)))
    edges = [start_ms + round(i * (end_ms - start_ms) / n_slices) for i in range(n_slices + 1)]
    slices = [(edges[i] + (1 if i else 0), edges[i + 1]) for i in range(n_slices)]
    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(8, len(slices))) as ex:
        futs = [ex.submit(_fetch_trade_slice, asset, s, e) for s, e in slices]
        for f in as_completed(futs):
            out.extend(f.result())
    dedup = {t["trade_id"]: t for t in out}
    return list(dedup.values())


def fetch_deribit(asset: str, start_ms: int, end_ms: int, want_market: bool) -> dict:
    """Always: 7d closes + window trades. If want_market (no S3), also DVOL,
    spot OHLC and a vol-surface ticker set so the pipeline runs end-to-end."""
    res: dict = {"closes_7d": [], "trades": [], "market": None}
    try:
        res["closes_7d"] = fetch_7d_closes(asset, end_ms)
    except Exception as e:
        warn(f"deribit 7d closes failed: {e}")
    try:
        res["trades"] = fetch_window_trades(asset, start_ms, end_ms)
    except Exception as e:
        warn(f"deribit trades failed: {e}")
    if want_market:
        try:
            res["market"] = _fetch_market_fallback(asset, start_ms, end_ms)
        except Exception as e:
            warn(f"deribit market fallback failed: {e}")
    return res


def _fetch_market_fallback(asset: str, start_ms: int, end_ms: int) -> dict:
    """DVOL + spot OHLC + a small ATM±4 surface from Deribit (test/no-S3 only)."""
    dvol = _get("get_volatility_index_data", {
        "currency": asset, "resolution": "3600",
        "start_timestamp": start_ms, "end_timestamp": end_ms,
    }).get("data") or []
    spot = _get("get_tradingview_chart_data", {
        "instrument_name": f"{asset}-PERPETUAL", "resolution": "60",
        "start_timestamp": start_ms, "end_timestamp": end_ms,
    })
    spot_now = (spot.get("close") or [None])[-1]
    tickers = {}
    if spot_now:
        insts = _get("get_instruments", {"currency": asset, "kind": "option", "expired": "false"})
        expiries = sorted(set(i["expiration_timestamp"] for i in insts))[:3]
        for exp in expiries:
            ex_insts = [i for i in insts if i["expiration_timestamp"] == exp]
            strikes = sorted(set(int(i["instrument_name"].split("-")[2]) for i in ex_insts))
            if not strikes:
                continue
            atm = min(range(len(strikes)), key=lambda k: abs(strikes[k] - spot_now))
            for k in strikes[max(0, atm - 4): atm + 5]:
                for ot in ("C", "P"):
                    nm = next((i["instrument_name"] for i in ex_insts
                               if int(i["instrument_name"].split("-")[2]) == k
                               and i["instrument_name"].endswith(ot)), None)
                    if not nm:
                        continue
                    try:
                        t = _get("ticker", {"instrument_name": nm})
                        tickers[nm] = {"mark_iv": t.get("mark_iv"),
                                       "delta": (t.get("greeks") or {}).get("delta")}
                    except Exception:
                        pass
    return {"dvol": dvol, "spot": spot, "spot_now": spot_now, "tickers": tickers}


# ── Hot CSVs (written by the single DuckDB session) ─────────────────────────

def _read_csv(csv_dir: str, name: str) -> list[dict]:
    path = os.path.join(csv_dir, name)
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _num(row: dict, *keys):
    for k in keys:
        v = row.get(k)
        if v not in (None, "", "NULL"):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


def load_hot(csv_dir: str, asset: str) -> dict:
    """Parse the hot CSVs defensively — tolerate missing files/columns by
    leaving the field null and recording a warning, never crashing."""
    out = {"dvol": None, "dvol_open": None, "dvol_low": None, "dvol_high": None,
           "spot_close": None, "spot_open": None, "spot_low": None,
           "volume_usd": None, "put_vol": None, "call_vol": None,
           "block_total_usd": None, "block_count": None,
           "tickers": {}, "primary_venue": None}

    ds = _read_csv(csv_dir, "dvol_spot.csv")
    for r in ds:
        metric = (r.get("metric") or "").lower()
        if metric == "dvol":
            out["dvol"] = _num(r, "close"); out["dvol_open"] = _num(r, "open")
            out["dvol_low"] = _num(r, "low"); out["dvol_high"] = _num(r, "high")
        elif metric == "spot":
            out["spot_close"] = _num(r, "close"); out["spot_open"] = _num(r, "open")
            out["spot_low"] = _num(r, "low")
    if not ds:
        warn("hot dvol_spot.csv missing — DVOL/spot from snapshot or fallback")

    vol = _read_csv(csv_dir, "volume.csv")
    if vol:
        tot = sum(_num(r, "notional") or 0 for r in vol)
        out["volume_usd"] = tot or None
        out["put_vol"] = sum(_num(r, "volume_sum") or 0 for r in vol
                             if (r.get("optionType") or "").upper().startswith("P")) or None
        out["call_vol"] = sum(_num(r, "volume_sum") or 0 for r in vol
                              if (r.get("optionType") or "").upper().startswith("C")) or None
        venues = defaultdict(float)
        for r in vol:
            ex = r.get("exchange") or r.get("venue")
            if ex:
                venues[ex] += _num(r, "notional") or 0
        if venues:
            out["primary_venue"] = max(venues, key=venues.get)
    else:
        warn("hot volume.csv missing — volume/P/C unavailable")

    blk = _read_csv(csv_dir, "block.csv")
    if blk:
        out["block_count"] = len(blk)
        out["block_total_usd"] = sum(_num(r, "notional") or 0 for r in blk) or None

    surf = _read_csv(csv_dir, "surface.csv")
    spot_for_surf = out["spot_close"]
    for r in surf:
        iv = _num(r, "markIV_close", "mark_iv", "markiv_close")
        delta = _num(r, "delta")
        exp = r.get("expiry"); strike = r.get("strike"); ot = (r.get("optionType") or "").upper()
        if iv is None or not exp or not strike:
            continue
        ot = "C" if ot.startswith("C") else ("P" if ot.startswith("P") else ot)
        try:
            sym = f"{asset}-{exp}-{int(float(strike))}-{ot}"
        except (TypeError, ValueError):
            continue
        out["tickers"][sym] = {"mark_iv": iv, "delta": delta}
        if spot_for_surf is None:
            spot_for_surf = _num(r, "underlying_price")
    out["surface_spot"] = spot_for_surf
    if not surf:
        warn("hot surface.csv missing — vol surface from fallback or No data")
    return out


# ── Block-flow leg detail ───────────────────────────────────────────────────

def _leg_phrase(legs: list[dict]) -> str:
    """One-line human detail for a block cluster, e.g.
    'sold 75C / bought 90C x150 two-way 42.3v'."""
    parts = []
    for leg in sorted(legs, key=lambda l: -l.get("amount", 0))[:4]:
        seg = leg["instrument_name"].split("-")
        if len(seg) < 4:
            continue
        verb = "bought" if leg.get("direction") == "buy" else "sold"
        strike_k = f"{int(int(seg[2]) / 1000)}K" if seg[2].isdigit() else seg[2]
        parts.append(f"{verb} {strike_k}{seg[3]}")
    size = round(sum(l.get("amount", 0) for l in legs), 1)
    side = dominant_side(legs).lower()
    ivs = [l["iv"] for l in legs if l.get("iv") is not None]
    iv = f" {round(sum(ivs)/len(ivs),1)}v" if ivs else ""
    return f"{' / '.join(parts)} x{size:g} {side}{iv}".strip()


def build_block_flow(trades: list[dict], hot: dict, spot: float | None) -> dict:
    clusters = cluster_blocks(trades)
    ranked = summarize_blocks(clusters, top_n=8, min_btc=5.0)
    rows = []
    for i, b in enumerate(ranked, 1):
        legs = clusters.get(b["block_trade_id"], [])
        exp = b.get("expiry") or ""
        rows.append({
            "rank": i,
            "structure": f"{exp} {b['structure']}".strip(),
            "notl_m": round(b["notional_usd"] / 1e6, 1),
            "detail": _leg_phrase(legs),
            "side": b["side"], "avg_iv": b["avg_iv"], "time_utc": b["time_utc"],
        })
    # Header totals: prefer authoritative hot block totals, else derive from tape.
    total_usd = hot.get("block_total_usd")
    n_blocks = hot.get("block_count")
    if total_usd is None:
        total_usd = sum(b["notional_usd"] for b in summarize_blocks(clusters, top_n=999, min_btc=0))
    if n_blocks is None:
        n_blocks = len(clusters)
    biggest = None
    if ranked:
        b0 = ranked[0]
        biggest = {
            "expiry": b0.get("expiry"), "structure": b0["structure"],
            "size": b0["size_btc"], "notional_m": round(b0["notional_usd"] / 1e6, 1),
            "time_utc": b0["time_utc"], "side": b0["side"], "avg_iv": b0["avg_iv"],
        }
    return {
        "total_m": round((total_usd or 0) / 1e6, 1), "n_blocks": n_blocks,
        "rows": rows, "biggest_print": biggest,
    }


# ── Assembly ────────────────────────────────────────────────────────────────

def pct(a, b):
    return round((a / b - 1) * 100, 1) if a and b else None


def fmt_hhmm(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%H:%M")


def dvol_label(o, c):
    if o is None or c is None:
        return None
    d = c - o
    return "rising" if d > 0.5 else "falling" if d < -0.5 else "flat"


def spot_vol_label(spot_open, spot_close, dvol_open, dvol_close):
    if None in (spot_open, spot_close, dvol_open, dvol_close):
        return None
    su, vu = spot_close > spot_open, dvol_close > dvol_open
    if su and not vu:
        return "vol sold through rally"
    if not su and vu:
        return "vol bid into weakness"
    if su and vu:
        return "vol bought through rally"
    return "vol faded with spot"


def build(asset: str, window: str, start_ms: int, end_ms: int,
          deri: dict, hot: dict) -> dict:
    asset = asset.upper()
    mkt = deri.get("market")

    # DVOL / spot: hot authoritative; fall back to Deribit market only if absent.
    dvol_close = hot.get("dvol"); dvol_open = hot.get("dvol_open")
    dvol_low, dvol_high = hot.get("dvol_low"), hot.get("dvol_high")
    spot_close = hot.get("spot_close"); spot_open = hot.get("spot_open")
    spot_low = hot.get("spot_low")
    if dvol_close is None and mkt and mkt.get("dvol"):
        d = mkt["dvol"]
        dvol_open = d[0][1]; dvol_close = d[-1][4]
        dvol_low = min(r[3] for r in d); dvol_high = max(r[2] for r in d)
    if spot_close is None and mkt and mkt.get("spot"):
        s = mkt["spot"]
        spot_open = (s.get("open") or [None])[0]
        spot_close = (s.get("close") or [None])[-1]
        spot_low = min(s.get("low") or [0]) or None

    spot = spot_close or hot.get("surface_spot") or (mkt or {}).get("spot_now")

    rv = realized_vs_implied(deri.get("closes_7d") or [], dvol_close)

    # Volume / P/C
    vol_usd = hot.get("volume_usd")
    pv, cv = hot.get("put_vol"), hot.get("call_vol")
    pc = round(pv / cv, 2) if pv and cv else None

    # Vol surface — hot tickers authoritative; fallback to Deribit market set.
    tickers = hot.get("tickers") or (mkt or {}).get("tickers") or {}
    surf = compute_vol_surface(tickers, hot.get("surface_spot") or spot) if tickers else None

    block = build_block_flow(deri.get("trades") or [], hot, spot)

    snapshot = {
        "spot": round(spot) if spot else None,
        "spot_from": round(spot_open) if spot_open else None,
        "spot_low": round(spot_low) if spot_low else None,
        "spot_change_pct": pct(spot_close, spot_open),
        "dvol": round(dvol_close, 1) if dvol_close is not None else None,
        "dvol_open": round(dvol_open, 2) if dvol_open is not None else None,
        "dvol_close": round(dvol_close, 2) if dvol_close is not None else None,
        "dvol_low": round(dvol_low, 1) if dvol_low is not None else None,
        "dvol_high": round(dvol_high, 1) if dvol_high is not None else None,
        "dvol_label": dvol_label(dvol_open, dvol_close),
        "rv_7d": rv.get("value"), "vrp": rv.get("vrp"), "vrp_label": rv.get("vrp_label"),
        "volume_usd_m": round(vol_usd / 1e6) if vol_usd else None,
        "primary_venue": hot.get("primary_venue"),
        "pc_ratio": pc, "pc_dominant": ("puts" if pc and pc > 1 else "calls" if pc else None),
        "spot_vol_label": spot_vol_label(spot_open, spot_close, dvol_open, dvol_close),
    }

    surface_out = None
    if surf:
        rows = []
        for e in surf.get("expiries", []):
            rows.append({
                "expiry": e["expiry"], "atm": e["atm_iv"],
                "rr_25d": e["rr_25d"], "fly": e["fly_25d"],
                "extrapolated": e["wings_extrapolated"],
            })
        surface_out = {
            "skew_line": surf.get("skew_label"),
            "term_line": surf.get("term_structure"),
            "front_atm": surf.get("front_atm"), "back_atm": surf.get("back_atm"),
            "rows": rows,
        }

    return {
        "header": {"asset": asset, "window": window,
                   "start_utc": fmt_hhmm(start_ms), "end_utc": fmt_hhmm(end_ms)},
        "snapshot": snapshot,
        "biggest_print": block["biggest_print"],
        "block_flow": {"total_m": block["total_m"], "n_blocks": block["n_blocks"],
                       "rows": block["rows"]},
        "vol_surface": surface_out,
        "warnings": WARNINGS,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Single-call options-recap orchestrator")
    ap.add_argument("--asset", default="btc")
    ap.add_argument("--window", default="8h")
    ap.add_argument("--csv-dir", default="/tmp/recap", help="dir with hot CSVs from DuckDB")
    ap.add_argument("--no-s3", action="store_true",
                    help="skip hot CSVs; pull DVOL/spot/surface from Deribit (local test)")
    ap.add_argument("--now-ms", type=int, help="override wall-clock (testing)")
    ap.add_argument("--pretty", action="store_true")
    args = ap.parse_args()

    asset = args.asset.lower()
    now_ms = args.now_ms or int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - parse_window_ms(args.window)

    hot = {} if args.no_s3 else load_hot(args.csv_dir, asset.upper())
    if not hot:  # --no-s3: empty hot dict, market fallback fills DVOL/spot/surface
        hot = {"tickers": {}}
    want_market = args.no_s3 or hot.get("dvol") is None

    # Deribit instrument names are case-sensitive (BTC-PERPETUAL); currency is not.
    deri = fetch_deribit(asset.upper(), start_ms, now_ms, want_market)
    result = build(asset, args.window, start_ms, now_ms, deri, hot)
    print(json.dumps(result, indent=2 if args.pretty else None, default=str))


if __name__ == "__main__":
    main()
