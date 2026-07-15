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
import subprocess
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
    aggregate_clips,
    compute_vol_surface,
    classify_structure,
    RV_LOOKBACK_DAYS,
)

DERIBIT = "https://www.deribit.com/api/v2/public"
WARNINGS: list[str] = []

# Vol-surface table caps to the front N expiries (chronological). The v_vol_surface
# store carries the full curve (~12 expiries); the recap only shows the near tenors.
MAX_SURFACE_ROWS = 5


def warn(msg: str) -> None:
    WARNINGS.append(msg)


def parse_window_ms(window: str) -> int:
    w = window.strip().lower()
    if w == "1d":
        return 24 * 3600_000
    units = {"m": 60_000, "h": 3600_000, "d": 86400_000}
    unit = w[-1]
    if unit not in units:
        raise ValueError(f"bad window '{window}' — use Nm/Nh/Nd, e.g. 30m/3h/8h/2d")
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


def _fetch_market_fallback(asset: str, start_ms: int, end_ms: int,
                           want_surface: bool = True) -> dict:
    """DVOL + spot OHLC from Deribit (these have no non-Deribit source), plus — only
    when want_surface — a small ATM±4 per-strike surface. The surface is ~50 Deribit
    `ticker` calls and dominates this call's latency, so callers that already hold a
    v_vol_surface snapshot (the normal dynamic-window case) pass want_surface=False
    and skip it entirely. When the surface IS needed, the ticker calls run
    concurrently rather than one-at-a-time."""
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
    if want_surface and spot_now:
        insts = _get("get_instruments", {"currency": asset, "kind": "option", "expired": "false"})
        names: list[str] = []
        for exp in sorted(set(i["expiration_timestamp"] for i in insts))[:3]:
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
                    if nm:
                        names.append(nm)

        def _one(nm: str):
            try:
                t = _get("ticker", {"instrument_name": nm})
                return nm, {"mark_iv": t.get("mark_iv"),
                            "delta": (t.get("greeks") or {}).get("delta")}
            except Exception:
                return nm, None

        if names:
            with ThreadPoolExecutor(max_workers=min(8, len(names))) as ex:
                for nm, v in ex.map(_one, names):
                    if v is not None:
                        tickers[nm] = v
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


# Maps a venue id to its display label. deribit and deribit-usdc are distinct
# production venues (BTC-inverse vs USDC-linear) that both render as "Deribit" —
# activity is aggregated by this label so they collapse into one Activity entry.
_VENUE_LABELS = {"deribit": "Deribit", "deribit-usdc": "Deribit",
                 "okex-options": "OKX", "bybit-options": "Bybit",
                 "bullish": "Bullish"}


def _venue_label(exchange: str) -> str:
    """Short display label for a venue id (e.g. okex-options -> OKX). Unknown/future
    venues degrade to a readable stem (e.g. cme-options -> Cme) — never crashes,
    never collides with a mapped label."""
    e = (exchange or "").lower()
    return _VENUE_LABELS.get(e, (exchange or "?").split("-")[0].title())


def load_hot(csv_dir: str, asset: str) -> dict:
    """Parse the hot CSVs defensively — tolerate missing files/columns by
    leaving the field null and recording a warning, never crashing."""
    out = {"dvol": None, "dvol_open": None, "dvol_low": None, "dvol_high": None,
           "spot_close": None, "spot_open": None, "spot_low": None,
           "volume_btc": None, "put_vol": None, "call_vol": None,
           "trades_by_venue": {}, "trades_total": None,
           "put_trades": None, "call_trades": None,
           "tickers": {}, "vs_now": {}, "vs_open": {}}

    ds = _read_csv(csv_dir, "dvol_spot.csv")
    # DVOL/spot are Deribit-only today. If a future venue ever emits dvol/spot rows,
    # this per-metric assignment would be last-row-wins (nondeterministic), so sort
    # Deribit rows last — they then win under the loop's overwrite. When no Deribit
    # row exists we still read whatever is present rather than crash/blank.
    for r in sorted(ds, key=lambda r: (r.get("exchange") or "").lower() == "deribit"):
        metric = (r.get("metric") or "").lower()
        if metric == "dvol":
            out["dvol"] = _num(r, "close"); out["dvol_open"] = _num(r, "open")
            out["dvol_low"] = _num(r, "low"); out["dvol_high"] = _num(r, "high")
        elif metric == "spot":
            out["spot_close"] = _num(r, "close"); out["spot_open"] = _num(r, "open")
            out["spot_low"] = _num(r, "low")
    if not ds:
        warn("hot dvol_spot.csv missing — DVOL/spot from snapshot or fallback")

    # Volume / P/C. Two reads, each on a basis that's honest for its scope:
    #   • Dollar volume — ONLY Deribit is priced in USD reliably (1 contract = 1 BTC,
    #     confirmed from the venue instrument feed). `volume_sum` units differ by
    #     venue and `notional_usd` isn't yet cross-venue-normalized, so we do NOT
    #     sum $ across venues; the Volume line is explicitly Deribit-scoped.
    #   • Activity + P/C — `trade_count` is unit-free (a trade is a trade), so it
    #     aggregates across ALL venues truthfully, with no contract multiplier.
    # Blank-optionType rows are per-exchange aggregates that double-count — drop them.
    vol = [r for r in _read_csv(csv_dir, "volume.csv") if (r.get("optionType") or "").strip()]
    if vol:
        # Exact "deribit" only — NOT startswith: the sibling venue deribit-usdc is
        # USDC-linear (a different contract unit), so folding it into this
        # BTC-inverse dollar-volume sum would contaminate the Volume line.
        deri = [r for r in vol if (r.get("exchange") or "").lower() == "deribit"]
        out["call_vol"] = sum(_num(r, "volume_sum") or 0 for r in deri
                              if (r.get("optionType") or "").upper().startswith("C")) or None
        out["put_vol"] = sum(_num(r, "volume_sum") or 0 for r in deri
                             if (r.get("optionType") or "").upper().startswith("P")) or None
        out["volume_btc"] = ((out["call_vol"] or 0) + (out["put_vol"] or 0)) or None
        byv = defaultdict(float)
        for r in vol:
            byv[r.get("exchange") or "?"] += _num(r, "trade_count") or 0
        out["trades_by_venue"] = dict(byv)
        out["trades_total"] = sum(byv.values()) or None
        out["put_trades"] = sum(_num(r, "trade_count") or 0 for r in vol
                                if (r.get("optionType") or "").upper().startswith("P")) or None
        out["call_trades"] = sum(_num(r, "trade_count") or 0 for r in vol
                                 if (r.get("optionType") or "").upper().startswith("C")) or None
    else:
        warn("hot volume.csv missing — volume/P/C unavailable")

    # surface.csv is a legacy fallback source for out["tickers"]; post-migration
    # run_recap.sh no longer emits it (the recap aggregates file has no surface
    # rows), so surf is normally empty and vs_now (below) drives the surface. The
    # reader is kept for back-compat and unit coverage — a no-op when absent.
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

    # v_vol_surface snapshots (consolidated per-strike IV+delta) for the window-
    # over-window deltas: surface_now.csv = latest snapshot, surface_open.csv =
    # snapshot nearest window-start. Both optional — absent → deltas read n/a.
    out["vs_now"] = _load_surface_tickers(csv_dir, "surface_now.csv")
    out["vs_open"] = _load_surface_tickers(csv_dir, "surface_open.csv")
    return out


def _load_surface_tickers(csv_dir: str, name: str) -> dict:
    """Read a v_vol_surface CSV (symbol, mark_iv, delta) into a ticker map keyed
    by the full instrument symbol — the shape compute_vol_surface expects. Each
    symbol is e.g. BTC-1JUL26-58000-C, so its expiry/type parse exactly as the
    Deribit instrument names the surface math already handles."""
    out: dict[str, dict] = {}
    for r in _read_csv(csv_dir, name):
        sym = r.get("symbol")
        iv = _num(r, "mark_iv")
        if not sym or iv is None:
            continue
        out[sym] = {"mark_iv": iv, "delta": _num(r, "delta")}
    return out


# ── Block-flow leg detail ───────────────────────────────────────────────────

def _leg_phrase(legs: list[dict], size: float | None = None,
                iv_label: str | None = None) -> str:
    """One-line human detail for a block cluster, e.g.
    'sold 75C / bought 90C x150 42.3v'.

    Directional verbs come from the per-leg taker `direction` field. If any leg
    lacks one, no direction is asserted: legs render neutrally ('75C vs 90C')
    tagged "two-way" — the desk term for an undisclosed side, which must never
    appear next to bought/sold. `size`/`iv_label` override the single-cluster
    values when the row aggregates several clips of a worked order (iv_label
    then carries the clip range, e.g. '36.5–37.0')."""
    directional = all(l.get("direction") in ("buy", "sell") for l in legs)
    parts = []
    for leg in sorted(legs, key=lambda l: -l.get("amount", 0))[:4]:
        seg = leg["instrument_name"].split("-")
        if len(seg) < 4:
            continue
        strike_k = f"{int(int(seg[2]) / 1000)}K" if seg[2].isdigit() else seg[2]
        if directional:
            verb = "bought" if leg["direction"] == "buy" else "sold"
            parts.append(f"{verb} {strike_k}{seg[3]}")
        else:
            parts.append(f"{strike_k}{seg[3]}")
    if size is None:
        size = round(sum(l.get("amount", 0) for l in legs), 1)
    if iv_label is None:
        ivs = [l["iv"] for l in legs if l.get("iv") is not None]
        iv_label = f"{round(sum(ivs) / len(ivs), 1)}" if ivs else None
    iv = f" {iv_label}v" if iv_label else ""
    joiner = " / " if directional else " vs "
    tag = "" if directional else " two-way"
    return f"{joiner.join(parts)} x{size:g}{tag}{iv}".strip()


def _iv_label(b: dict) -> str | None:
    """IV text for a (possibly clip-aggregated) block-flow row: the clip range
    '36.5–37.0' when a worked order printed at different vols, else one value."""
    lo, hi = b.get("iv_lo"), b.get("iv_hi")
    if lo is not None and hi is not None and lo != hi:
        return f"{lo}–{hi}"
    if b.get("avg_iv") is not None:
        return f"{b['avg_iv']}"
    return None


def build_block_flow(trades: list[dict], hot: dict, spot: float | None,
                     top_n: int = 8, min_btc: float = 5.0) -> dict:
    clusters = cluster_blocks(trades)
    # Two granularities, both surfaced: tape BLOCKS (block_trade_ids ≥min_btc)
    # and STRUCTURES (clips of one worked order grouped by leg signature).
    # Rows are structures; the Blocks column carries each row's print count, so
    # header and table reconcile by construction. One ≥min_btc basis for
    # everything — the hot block.csv has unit-corrupt rows (one block at $5B),
    # so the tape clustering is the only source we trust here.
    ranked_all = summarize_blocks(clusters, top_n=10**9, min_btc=min_btc)
    grouped = aggregate_clips(ranked_all, clusters)
    rows = []
    for i, b in enumerate(grouped[:top_n], 1):
        legs = clusters.get(b["block_trade_id"], [])
        exp = b.get("expiry") or ""
        rows.append({
            "rank": i,
            "structure": f"{exp} {b['structure']}".strip(),
            "notl_m": round(b["notional_usd"] / 1e6, 1),
            "blocks": b["clip_count"],
            "detail": _leg_phrase(legs, size=b["size_btc"], iv_label=_iv_label(b)),
            "side": b["side"], "avg_iv": b["avg_iv"], "time_utc": b["time_utc"],
        })
    total_usd = sum(b["notional_usd"] for b in ranked_all)
    biggest = None
    if ranked_all:
        b0 = ranked_all[0]  # largest single print, not the clip aggregate
        biggest = {
            "expiry": b0.get("expiry"), "structure": b0["structure"],
            "size": b0["size_btc"], "notional_m": round(b0["notional_usd"] / 1e6, 1),
            "time_utc": b0["time_utc"], "side": b0["side"], "avg_iv": b0["avg_iv"],
        }
    return {
        "total_m": round((total_usd or 0) / 1e6, 1),
        "n_blocks": len(ranked_all), "n_structures": len(grouped),
        "rows": rows, "biggest_print": biggest,
    }


# ── Assembly ────────────────────────────────────────────────────────────────

def pct(a, b):
    return round((a / b - 1) * 100, 1) if a and b else None


def pc_descriptor(pc: float | None) -> str | None:
    """Banded P/C label — reciprocal-symmetric (1/1.05 ≈ 0.95, 1/1.25 = 0.80),
    so a 1.05x ratio reads near-neutral instead of 'puts dominant'."""
    if pc is None:
        return None
    if pc > 1.25:
        return "puts dominant"
    if pc > 1.05:
        return "put-tilt"
    if pc >= 0.95:
        return "balanced"
    if pc >= 0.80:
        return "call-tilt"
    return "calls dominant"


def fmt_hhmm(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%H:%M")


def fmt_stamp(ms: int, with_date: bool) -> str:
    """Header timestamp. Windows ≥24h span at least a day, and any multiple-of-24h
    window has identical start/end clock times (e.g. 48h → 17:30–17:30), so include
    the date once the window reaches a day; keep it HH:MM-only for intraday windows."""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%b %d %H:%M") if with_date else dt.strftime("%H:%M")


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

    # Volume ($) is Deribit-scoped — the only venue we can price in USD reliably.
    vol_btc = hot.get("volume_btc")
    vol_usd = vol_btc * spot if (vol_btc and spot) else None
    # Activity + P/C use trade_count — unit-free, so they span ALL venues truthfully.
    pt, ct = hot.get("put_trades"), hot.get("call_trades")
    pc = round(pt / ct, 2) if pt and ct else None
    tt = hot.get("trades_total")
    activity_split = None
    if tt:
        # Fold raw venue ids into display labels FIRST, so venues that share a label
        # (deribit + deribit-usdc → "Deribit") collapse into a single entry before
        # pct/sort — otherwise "Deribit" appears twice and the [:4] display cap can
        # push a real venue off the line. tt already spans all raw venues, so the
        # per-label pcts remain a correct share of total activity.
        by_label: dict[str, float] = defaultdict(float)
        for v, n in (hot.get("trades_by_venue") or {}).items():
            by_label[_venue_label(v)] += n
        activity_split = [
            {"venue": lbl, "pct": round(100 * n / tt)}
            for lbl, n in sorted(by_label.items(), key=lambda kv: -kv[1])
        ]

    # Vol surface — v_vol_surface "now" snapshot is authoritative (it pairs with
    # the "open" snapshot for consistent window-over-window deltas); fall back to
    # the hot surface.csv, then the Deribit market set. surf_open drives the deltas.
    surf_spot = hot.get("surface_spot") or spot
    vs_now = hot.get("vs_now") or {}
    vs_open = hot.get("vs_open") or {}
    tickers = vs_now or hot.get("tickers") or (mkt or {}).get("tickers") or {}
    # Cap the "now" surface at the display limit so the term-structure label
    # describes exactly the tenors the table shows (not invisible back months).
    surf = (compute_vol_surface(tickers, surf_spot, max_expiries=MAX_SURFACE_ROWS)
            if tickers else None)
    surf_open = compute_vol_surface(vs_open, surf_spot) if vs_open else None

    trades = deri.get("trades") or []
    block = build_block_flow(trades, hot, spot)

    # Flow-horizon check. The flow sections only reach back ~24h: Volume / Activity /
    # P/C come from the rolling recap-aggregates file (trailing 24h) and Biggest
    # Print / Block Flow from the Deribit public tape (also ~24h retention); DVOL/spot
    # (OHLC) and the vol surface (v_vol_surface) retain much longer. So for a window
    # past that horizon the flow sections silently cover less than the header claims.
    # The tape's oldest trade is the tightest available proxy for the covered span
    # (both flow sources cap near 24h). Surface a banner (render_md). Cleared
    # automatically once >24h flow is sourced from the cold store.
    flow_horizon = None
    window_h = (end_ms - start_ms) / 3600_000
    if window_h > 24 and trades:
        oldest = min(t.get("timestamp") or end_ms for t in trades)
        covered_h = (end_ms - oldest) / 3600_000
        if window_h - covered_h > 2:
            flow_horizon = {"covered_h": round(covered_h), "window_h": round(window_h)}

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
        "activity_trades": tt,
        "activity_split": activity_split,
        "pc_ratio": pc, "pc_descriptor": pc_descriptor(pc),
        "spot_vol_label": spot_vol_label(spot_open, spot_close, dvol_open, dvol_close),
    }

    surface_out = None
    if surf:
        open_by_exp = {e["expiry"]: e for e in (surf_open or {}).get("expiries", [])}

        def _delta(curr, key, o):
            prev = o.get(key) if o else None
            return round(curr - prev, 1) if (curr is not None and prev is not None) else None

        rows = []
        for e in surf.get("expiries", [])[:MAX_SURFACE_ROWS]:
            o = open_by_exp.get(e["expiry"])
            rows.append({
                "expiry": e["expiry"], "atm": e["atm_iv"],
                "rr_25d": e["rr_25d"], "fly": e["fly_25d"],
                "d_atm": _delta(e["atm_iv"], "atm_iv", o),
                "d_rr": _delta(e["rr_25d"], "rr_25d", o),
                "d_fly": _delta(e["fly_25d"], "fly_25d", o),
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
                   "start_utc": fmt_stamp(start_ms, window_h >= 24),
                   "end_utc": fmt_stamp(end_ms, window_h >= 24)},
        "snapshot": snapshot,
        "biggest_print": block["biggest_print"],
        "block_flow": {"total_m": block["total_m"], "n_blocks": block["n_blocks"],
                       "n_structures": block["n_structures"], "rows": block["rows"]},
        "vol_surface": surface_out,
        "flow_horizon": flow_horizon,
        "warnings": WARNINGS,
    }


def run_duckdb(sql_path: str) -> int:
    """Run one DuckDB session from a .sql file (its COPY statements write the hot
    CSVs). Invoked in a thread so it overlaps the Deribit fetch — both are
    network-bound, so the two run concurrently instead of back-to-back."""
    try:
        with open(sql_path) as f:
            r = subprocess.run(["duckdb"], stdin=f, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, timeout=60)
        if r.returncode != 0:
            warn(f"duckdb rc={r.returncode}: {(r.stderr or b'').decode()[:200]}")
        return r.returncode
    except FileNotFoundError:
        warn("duckdb not found on PATH")
        return -1
    except Exception as e:  # noqa: BLE001
        warn(f"duckdb invocation failed: {e}")
        return -1


def _delta_fmt(d, star: str = "") -> str:
    """Format a vol-surface delta cell: signed `+1.2v`, `flat` when it rounds to
    zero, `n/a` when no window-open value was available. `star` carries the wing-
    extrapolation flag from the paired metric."""
    if d is None:
        return "n/a"
    if abs(d) < 0.05:
        return "flat"
    return f"{d:+}v{star}"


def render_md(r: dict) -> str:
    """Render the final four-section recap markdown so the agent relays it
    verbatim — no field-mapping reasoning, fully deterministic output."""
    h, s, bp, bf, vs = (r["header"], r["snapshot"], r["biggest_print"],
                        r["block_flow"], r["vol_surface"])
    L: list[str] = []

    crit = [w for w in (r.get("warnings") or []) if any(
        k in w for k in ("missing", "unavailable", "failed"))]
    if crit and s.get("volume_usd_m") is None and vs is None:
        L.append("⚠ hot surface unavailable — affected sections read No data")
        L.append("")

    # >24h window: the flow sections only reach back ~24h (rolling recap-aggregates
    # file + Deribit tape both cap near 24h), while DVOL/spot/surface span the full
    # window. Flag it so the header isn't read as covering the whole window for flow.
    # Goes away once >24h flow comes from the cold store.
    fh = r.get("flow_horizon")
    if fh:
        L.append(f"⚠ Volume · Activity · Biggest Print · Block Flow cover "
                 f"~{fh['covered_h']}h (~24h flow-data horizon); DVOL/spot/surface "
                 f"span the full {fh['window_h']}h.")
        L.append("")

    L.append(f"**{h['asset']} Options · {h['window']} Recap · "
             f"{h['start_utc']}–{h['end_utc']} UTC**")
    L += ["", "**Snapshot**", "", "```yaml"]

    spot = f"${s['spot']:,}" if s.get("spot") else "n/a"
    chg = s.get("spot_change_pct")
    chg_txt = ("flat" if not chg else f"{'up' if chg > 0 else 'down'} {abs(chg)}%")
    extra = []
    if s.get("spot_from"):
        extra.append(f"from ${s['spot_from']:,}")
    if s.get("spot_low"):
        extra.append(f"low ${s['spot_low']:,}")
    extra_txt = f" ({', '.join(extra)})" if extra else ""
    L.append(f"{'Spot':<9} {spot:<11} {chg_txt}{extra_txt}")

    dvol = f"{s['dvol']}v" if s.get("dvol") is not None else "n/a"
    dv = (f" ({round(s['dvol_open'], 1)} -> {round(s['dvol_close'], 1)})"
          if s.get("dvol_open") is not None and s.get("dvol_close") is not None else "")
    L.append(f"{'DVOL':<9} {dvol:<11} {s.get('dvol_label') or ''}{dv}")

    vrp = s.get("vrp")
    rich = ("CHEAP" if vrp is not None and vrp < -1 else
            "RICH" if vrp is not None and vrp > 1 else "IN LINE")
    rv = f"{s['rv_7d']}v" if s.get("rv_7d") is not None else "n/a"
    L.append(f"{'RV 7d':<9} {rv:<11} implied {rich} vs realized")

    vrp_txt = f"{vrp:+}v" if vrp is not None else "n/a"
    upo = ("underpriced" if vrp is not None and vrp < 0 else
           "overpriced" if vrp is not None and vrp > 0 else "fair")
    L.append(f"{'VRP':<9} {vrp_txt:<11} vol {upo} vs delivered")

    if s.get("activity_trades"):
        tt = s["activity_trades"]
        tnum = (f"{tt / 1e6:.1f}M" if tt >= 1e6 else
                f"{round(tt / 1e3)}k" if tt >= 1e3 else f"{int(tt)}")
        split = " · ".join(f"{v['venue']} {v['pct']}%"
                           for v in (s.get("activity_split") or [])[:4])
        L.append(f"{'Activity':<9} {tnum:<11} trades — {split} (by trade count)")
    vol = f"${s['volume_usd_m']}M" if s.get("volume_usd_m") else "n/a"
    L.append(f"{'Volume':<9} {vol:<11} Deribit only (cross-venue $ pending)")
    pc = f"{s['pc_ratio']}x" if s.get("pc_ratio") is not None else "n/a"
    pc_desc = f"{s['pc_descriptor']} " if s.get("pc_descriptor") else ""
    L.append(f"{'P/C':<9} {pc:<11} {pc_desc}(all venues, by trades)")
    L += ["```", "", "**Biggest Print**", "", "```yaml"]

    if bp:
        # dominant_side's "Mixed" is a structure fact (legs point both ways),
        # not an aggressor read — don't put it in the side slot.
        tags = [bp["side"]] if bp.get("side") in ("Buy", "Sell") else []
        if bp.get("avg_iv") is not None:
            tags.append(f"{bp['avg_iv']}v avg")
        tag_txt = f" ({', '.join(tags)})" if tags else ""
        L.append(f"{bp['expiry']} {bp['structure']}   {bp['size']:g}x   "
                 f"${bp['notional_m']}M   {bp['time_utc']} UTC   "
                 f"via Paradigm/Deribit{tag_txt}")
    else:
        L.append("No data")
    n_struct = bf.get("n_structures", len(bf["rows"]))
    struct_word = "structure" if n_struct == 1 else "structures"
    trunc = f" (top {len(bf['rows'])} by notional)" if n_struct > len(bf["rows"]) else ""
    L += ["```", "", f"**Block Flow — ${bf['total_m']}M / {bf['n_blocks']} blocks / "
          f"{n_struct} {struct_word}{trunc}**",
          "", "```yaml", f"{'#':<3}{'Structure':<27}{'Notl':<9}{'Blocks':<8}Detail",
          f"{'-':<3}{'-' * 25:<27}{'-' * 7:<9}{'-' * 6:<8}{'-' * 44}"]
    for row in bf["rows"]:
        notl = f"${row['notl_m']}M"
        L.append(f"{str(row['rank']):<3}{row['structure']:<27}{notl:<9}"
                 f"{str(row.get('blocks', 1)):<8}{row['detail']}")
    L += ["```", "", "**Vol Surface**"]

    if vs and vs.get("rows"):
        fa, ba, term = vs.get("front_atm"), vs.get("back_atm"), vs.get("term_line")
        term_txt = (f"{fa}v → {ba}v → {term}" if fa is not None and ba is not None
                    and term else (term or "n/a"))
        L.append(f"Skew: {vs.get('skew_line') or 'n/a'} · Term: {term_txt}")
        L += ["", "```yaml",
              f"{'Expiry':<11}{'ATM':<9}{'ΔATM':<9}{'25d RR':<10}{'ΔRR':<9}{'Fly':<8}ΔFly",
              f"{'-' * 9:<11}{'-' * 6:<9}{'-' * 6:<9}{'-' * 8:<10}{'-' * 6:<9}{'-' * 5:<8}{'-' * 6}"]
        for e in vs["rows"]:
            star = "*" if e.get("extrapolated") else ""
            atm = f"{e['atm']}v" if e.get("atm") is not None else "n/a"
            rr = f"{e['rr_25d']:+}v{star}" if e.get("rr_25d") is not None else "n/a"
            fly = f"{e['fly']}v" if e.get("fly") is not None else "n/a"
            datm = _delta_fmt(e.get("d_atm"))
            drr = _delta_fmt(e.get("d_rr"), star)
            dfly = _delta_fmt(e.get("d_fly"))
            L.append(f"{e['expiry']:<11}{atm:<9}{datm:<9}{rr:<10}{drr:<9}{fly:<8}{dfly}")
        L.append("```")
    else:
        L.append("No data")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="Single-call options-recap orchestrator")
    ap.add_argument("--asset", default="btc")
    ap.add_argument("--window", default="8h")
    ap.add_argument("--csv-dir", default="/tmp/recap", help="dir with hot CSVs from DuckDB")
    ap.add_argument("--no-s3", action="store_true",
                    help="skip hot CSVs; pull DVOL/spot/surface from Deribit (local test)")
    ap.add_argument("--duckdb-sql", help="run this .sql via DuckDB concurrently with the "
                    "Deribit fetch (produces the hot CSVs); omit if CSVs already exist")
    ap.add_argument("--now-ms", type=int, help="override wall-clock (testing)")
    ap.add_argument("--pretty", action="store_true")
    ap.add_argument("--render", action="store_true",
                    help="print the final four-section recap markdown (live path)")
    args = ap.parse_args()

    asset = args.asset.lower()
    ASSET = asset.upper()  # Deribit instrument names are case-sensitive; currency is not.
    now_ms = args.now_ms or int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - parse_window_ms(args.window)

    if args.no_s3:
        # Offline/local: no hot CSVs; Deribit supplies DVOL/spot/surface too.
        hot = {"tickers": {}}
        deri = fetch_deribit(ASSET, start_ms, now_ms, want_market=True)
    else:
        # Parallelize the DuckDB read (hot CSVs) with the always-needed Deribit
        # core fetch (7d closes + window trades) — both are network-bound.
        with ThreadPoolExecutor(max_workers=2) as ex:
            duck_fut = ex.submit(run_duckdb, args.duckdb_sql) if args.duckdb_sql else None
            deri_fut = ex.submit(fetch_deribit, ASSET, start_ms, now_ms, False)
            if duck_fut is not None:
                duck_fut.result()
            deri = deri_fut.result()
        hot = load_hot(args.csv_dir, ASSET)
        # No hot dvol_spot row: the DuckDB read of the rolling recap-aggregates file
        # failed or returned nothing for this window. Either way DVOL/spot must come
        # from Deribit. Only pull the expensive per-strike ticker surface when
        # v_vol_surface also gave us nothing — for a normal dynamic window vs_now
        # is populated, so we skip ~50 serial ticker calls (the bulk of the cost).
        if hot.get("dvol") is None:
            want_surface = not hot.get("vs_now")
            try:
                deri["market"] = _fetch_market_fallback(
                    ASSET, start_ms, now_ms, want_surface=want_surface)
            except Exception as e:  # noqa: BLE001
                warn(f"deribit market fallback failed: {e}")

    result = build(asset, args.window, start_ms, now_ms, deri, hot)
    if args.render:
        print(render_md(result))
    else:
        print(json.dumps(result, indent=2 if args.pretty else None, default=str))


if __name__ == "__main__":
    main()
