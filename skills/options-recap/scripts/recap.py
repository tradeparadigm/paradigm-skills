#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
recap.py — single-call orchestrator for the options recap.

ONE invocation does the entire recap: it fetches the Deribit 7d closes (the
realized-vol input), ingests the DuckDB-written CSVs (hot surface + the
multi-venue block tape), runs the vol math (realized-vs-implied, block
ranking/rollup, vol-surface skew/term), and prints ONE JSON object whose fields
map 1:1 to the four output sections.

The agent runs this once and renders the four sections from the JSON. It must
not paginate, merge, cluster, or hand-assemble a snapshot — all of that is here.

Pipeline (concurrent where independent):
  • Deribit 7d hourly closes        → realized vol (no non-Deribit source)
  • blocks.csv (DuckDB, tape)       → Biggest Print + Block Flow, across ALL
                                      venues Paradigm brokers (Deribit/Paradex/
                                      Bullish/…), notional already in USD per leg
  • hot CSVs in --csv-dir (DuckDB)  → DVOL/spot OHLC, $ Volume, activity+P/C
                                      trade counts, vol surface (markIV/delta)

Hot CSVs are authoritative for DVOL/spot/$Volume/activity/P-C/surface. Biggest
Print + Block Flow come from the Paradigm block tape (paradigm_trade_tape_slim)
— multi-venue, S3-sourced, no live exchange API. The tape carries no IV, so the
top blocks' IV is looked up from the vol surface (Deribit legs only). Deribit
still supplies the 7d realized-vol closes; nothing else hits an exchange API.

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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(__file__))
from vol_math import (  # noqa: E402
    realized_vs_implied,
    build_tape_blocks,
    compute_vol_surface,
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


def fetch_deribit(asset: str, start_ms: int, end_ms: int, want_market: bool) -> dict:
    """Always: 7d closes (the realized-vol input; no non-Deribit source). If
    want_market (no S3), also DVOL, spot OHLC and a vol-surface ticker set so the
    pipeline runs end-to-end. Block flow no longer comes from here — it's the
    multi-venue Paradigm tape (blocks.csv), so no window-trade fetch."""
    res: dict = {"closes_7d": [], "market": None}
    try:
        res["closes_7d"] = fetch_7d_closes(asset, end_ms)
    except Exception as e:
        warn(f"deribit 7d closes failed: {e}")
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


def _own_asset_rows(rows: list[dict], asset: str, name: str) -> list[dict]:
    """Keep only rows whose `asset` column matches the recap's asset. The SQL
    already filters by asset, so a mismatched row means the CSV is not this
    run's slice (wrong file, stale state, cross-run contamination) — exactly
    the corruption that once put an ETH Snapshot inside a BTC recap. Dropping
    the rows sends the field down the null → Deribit-fallback path with a loud
    warning instead of rendering the wrong asset's numbers. CSVs without the
    column (older fixtures) pass through untouched."""
    if not rows or "asset" not in rows[0]:
        return rows
    keep = [r for r in rows if (r.get("asset") or "").upper() == asset]
    if len(keep) != len(rows):
        others = sorted({(r.get("asset") or "?") for r in rows
                         if (r.get("asset") or "").upper() != asset})
        warn(f"hot {name}: dropped {len(rows) - len(keep)} rows for "
             f"{'/'.join(others)} (expected {asset}) — cross-run contamination?")
    return keep


def load_hot(csv_dir: str, asset: str) -> dict:
    """Parse the hot CSVs defensively — tolerate missing files/columns by
    leaving the field null and recording a warning, never crashing."""
    out = {"dvol": None, "dvol_open": None, "dvol_low": None, "dvol_high": None,
           "spot_close": None, "spot_open": None, "spot_low": None,
           "volume_btc": None, "put_vol": None, "call_vol": None,
           "trades_by_venue": {}, "trades_total": None,
           "put_trades": None, "call_trades": None,
           "tickers": {}, "vs_now": {}, "vs_open": {}}

    ds = _own_asset_rows(_read_csv(csv_dir, "dvol_spot.csv"), asset, "dvol_spot.csv")
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
    vol = [r for r in _own_asset_rows(_read_csv(csv_dir, "volume.csv"), asset, "volume.csv")
           if (r.get("optionType") or "").strip()]
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
    out["vs_now"] = _load_surface_tickers(csv_dir, "surface_now.csv", asset)
    out["vs_open"] = _load_surface_tickers(csv_dir, "surface_open.csv", asset)
    return out


def _load_surface_tickers(csv_dir: str, name: str, asset: str | None = None) -> dict:
    """Read a v_vol_surface CSV (symbol, mark_iv, delta) into a ticker map keyed
    by the full instrument symbol — the shape compute_vol_surface expects. Each
    symbol is e.g. BTC-1JUL26-58000-C, so its expiry/type parse exactly as the
    Deribit instrument names the surface math already handles. When `asset` is
    given, symbols for any other asset are dropped with a warning — same
    contamination guard as _own_asset_rows, keyed on the symbol prefix."""
    out: dict[str, dict] = {}
    dropped = 0
    for r in _read_csv(csv_dir, name):
        sym = r.get("symbol")
        iv = _num(r, "mark_iv")
        if not sym or iv is None:
            continue
        if asset and not sym.upper().startswith(f"{asset.upper()}-"):
            dropped += 1
            continue
        out[sym] = {"mark_iv": iv, "delta": _num(r, "delta")}
    if dropped:
        warn(f"hot {name}: dropped {dropped} symbols not for {asset} — "
             "cross-run contamination?")
    return out


# ── Block tape (paradigm_trade_tape_slim) ───────────────────────────────

def load_blocks(csv_dir: str) -> list[dict]:
    """Read blocks.csv — the window\'s option block legs from the Paradigm tape
    (paradigm_trade_tape_slim), one row per leg, across every venue. Missing file
    → [] (Biggest Print / Block Flow then read No data), never a crash."""
    rows = _read_csv(csv_dir, "blocks.csv")
    if not rows:
        warn("blocks.csv missing/empty — Biggest Print / Block Flow unavailable")
    return rows


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


def _blocks_asof_ms(block_rows: list[dict]) -> int | None:
    """Newest block-leg timestamp (ms) from blocks.csv DATE+TIME (UTC). Drives the
    tape-freshness stamp: the Paradigm tape is S3-sourced, so the recap discloses
    how current the block section is rather than implying it's live to the second."""
    newest = None
    for r in block_rows or []:
        d, t = r.get("DATE"), r.get("TIME")
        if not d:
            continue
        try:
            dt = datetime.strptime(f"{d} {(t or '00:00:00')[:8]}",
                                   "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        ms = int(dt.timestamp() * 1000)
        newest = ms if newest is None else max(newest, ms)
    return newest


def build(asset: str, window: str, start_ms: int, end_ms: int,
          deri: dict, hot: dict, block_rows: list[dict] | None = None) -> dict:
    asset = asset.upper()
    mkt = deri.get("market")
    window_h = (end_ms - start_ms) / 3600_000

    # DVOL / spot: hot authoritative for windows the rolling aggregates file
    # actually spans (~24h); fall back to Deribit market if hot is absent.
    # PAST ~24h the hot OHLC silently covers only the file's retention — a 2d
    # recap once quoted a 24h-scoped low under a banner claiming full-window
    # spot — so for >24h windows the Deribit market fetch (full history) is
    # authoritative instead, with hot as the fallback.
    prefer_mkt = window_h > 24
    dvol_close = hot.get("dvol"); dvol_open = hot.get("dvol_open")
    dvol_low, dvol_high = hot.get("dvol_low"), hot.get("dvol_high")
    spot_close = hot.get("spot_close"); spot_open = hot.get("spot_open")
    spot_low = hot.get("spot_low")
    if (dvol_close is None or prefer_mkt) and mkt and mkt.get("dvol"):
        d = mkt["dvol"]
        dvol_open = d[0][1]; dvol_close = d[-1][4]
        dvol_low = min(r[3] for r in d); dvol_high = max(r[2] for r in d)
    if (spot_close is None or prefer_mkt) and mkt and mkt.get("spot"):
        s = mkt["spot"]
        spot_open = (s.get("open") or [None])[0]
        spot_close = (s.get("close") or [None])[-1]
        spot_low = min(s.get("low") or [0]) or None

    spot = spot_close or hot.get("surface_spot") or (mkt or {}).get("spot_now")

    rv = realized_vs_implied(deri.get("closes_7d") or [], dvol_close)

    # Volume ($) is Deribit-scoped, from the hot rollup: call+put BTC volume × spot.
    # (Only Deribit is reliably priced 1 contract = 1 BTC; volume_sum units differ by
    # venue and notional_usd isn't yet cross-venue-normalized, so the line stays
    # Deribit-scoped.) The rollup head-lags the live tape ~10-15 min, so a very thin
    # window may under-count the newest prints — an accepted trade-off now that Block
    # Flow is the multi-venue Paradigm tape, a different universe from this line.
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

    # Biggest Print + Block Flow: the multi-venue Paradigm block tape (blocks.csv),
    # ranked/rolled-up in vol_math. Notional is USD per leg on the tape, so this path
    # does no cross-venue normalization. IV isn't on the tape, so annotate the top
    # blocks from the vol surface — Deribit legs only (the surface is Deribit-scoped);
    # non-Deribit venues show IV n/a.
    def iv_lookup(cp: str, strike: int, expiry_c: str):
        t = (vs_now or {}).get(f"{asset}-{expiry_c}-{int(strike)}-{cp}")
        return t.get("mark_iv") if t else None

    # Defense in depth: the DuckDB query already scopes blocks.csv to this asset,
    # but drop any stray other-asset row (PRODUCT '<ASSET> OPTION - …') before
    # ranking — a leaked ETH row must never win a BTC recap's Biggest Print.
    own_blocks = [r for r in (block_rows or [])
                  if (r.get("PRODUCT") or "").upper().startswith(f"{asset} ")]
    dropped = len(block_rows or []) - len(own_blocks)
    if dropped:
        warn(f"blocks.csv: dropped {dropped} non-{asset} rows — cross-asset contamination?")
    block = build_tape_blocks(own_blocks, iv_lookup=iv_lookup)

    # Tape-freshness stamp. The block tape is S3-sourced (near-real-time but not
    # live-to-the-second), so disclose how current the block section is instead of
    # implying it matches the window end exactly. Also a >24h flag: Volume/Activity/
    # P-C/DVOL/spot come from the ~24h hot rollup, so a longer window under-covers
    # them (run_recap caps at 24h; this defends a direct >24h call).
    asof_ms = _blocks_asof_ms(own_blocks)
    blocks_asof = fmt_hhmm(asof_ms) if asof_ms else None
    blocks_lag_min = round((end_ms - asof_ms) / 60000) if asof_ms else None
    hot_horizon = round(window_h) if window_h > 24 else None

    snapshot = {
        "spot": round(spot) if spot else None,
        "spot_from": round(spot_open) if spot_open else None,
        "spot_low": round(spot_low) if spot_low else None,
        # % from the ROUNDED display prices, so the line reconciles with the
        # two dollar figures it sits next to (unrounded inputs once produced
        # "down 0.1%" beside prices whose own arithmetic gives 0.2%).
        "spot_change_pct": pct(round(spot_close) if spot_close else None,
                               round(spot_open) if spot_open else None),
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
                       "n_structures": block["n_structures"], "rows": block["rows"],
                       "asof_utc": blocks_asof, "lag_min": blocks_lag_min},
        "vol_surface": surface_out,
        "hot_horizon": hot_horizon,
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

    # >24h window: Volume/Activity/P-C/DVOL/spot come from the ~24h hot rollup, so
    # they under-cover a longer window while Block Flow (Paradigm tape) and the
    # surface span it fully. run_recap caps at 24h; this defends a direct >24h call.
    hh = r.get("hot_horizon")
    if hh:
        L.append(f"⚠ Volume · Activity · P/C · DVOL/spot cover ~24h (hot-rollup "
                 f"horizon); Block Flow and surface span the full {hh}h.")
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
    # Same ±1v dead-band as the RV line above — otherwise a VRP in (0,1] prints
    # "IN LINE" and "overpriced" on adjacent lines.
    upo = ("underpriced" if vrp is not None and vrp < -1 else
           "overpriced" if vrp is not None and vrp > 1 else "roughly fair")
    L.append(f"{'VRP':<9} {vrp_txt:<11} vol {upo} vs delivered")

    # Activity always renders — an empty window reads n/a like Volume/P-C do;
    # silently dropping the line makes the Snapshot shape depend on the data.
    if s.get("activity_trades"):
        tt = s["activity_trades"]
        tnum = (f"{tt / 1e6:.1f}M" if tt >= 1e6 else
                f"{round(tt / 1e3)}k" if tt >= 1e3 else f"{int(tt)}")
        split = " · ".join(f"{v['venue']} {v['pct']}%"
                           for v in (s.get("activity_split") or [])[:4])
        L.append(f"{'Activity':<9} {tnum:<11} trades — {split} (by trade count)")
    else:
        L.append(f"{'Activity':<9} {'n/a':<11} trades (by trade count)")
    vol = f"${s['volume_usd_m']}M" if s.get("volume_usd_m") else "n/a"
    L.append(f"{'Volume':<9} {vol:<11} Deribit only (cross-venue $ pending)")
    pc = f"{s['pc_ratio']}x" if s.get("pc_ratio") is not None else "n/a"
    pc_desc = f"{s['pc_descriptor']} " if s.get("pc_descriptor") else ""
    L.append(f"{'P/C':<9} {pc:<11} {pc_desc}(all venues, by trades)")
    L += ["```", "", "**Biggest Print — Paradigm block flow**", "", "```yaml"]

    if bp:
        # "Mixed" is a structure fact (legs point both ways), not an aggressor
        # read — don't put it in the side slot. Venue names the executing venue
        # for this Paradigm-brokered block (Deribit/Paradex/Bullish/…).
        tags = [bp["side"]] if bp.get("side") in ("Buy", "Sell") else []
        if bp.get("avg_iv") is not None:
            tags.append(f"{bp['avg_iv']}v avg")
        tag_txt = f" ({', '.join(tags)})" if tags else ""
        L.append(f"{bp['expiry']} {bp['structure']}   {bp['size']:g}x   "
                 f"${bp['notional_m']}M   {bp['time_utc']} UTC   "
                 f"via Paradigm/{bp.get('venue') or '?'}{tag_txt}")
    else:
        L.append("No data")
    n_struct = bf.get("n_structures", len(bf["rows"]))
    struct_word = "structure" if n_struct == 1 else "structures"
    block_word = "block" if bf["n_blocks"] == 1 else "blocks"
    trunc = f" (top {len(bf['rows'])} by notional)" if n_struct > len(bf["rows"]) else ""
    # S3-tape freshness stamp: disclose how current the block section is (the tape
    # is near-real-time but not live), and flag a material lag when present.
    asof = (f" · tape through {bf['asof_utc']} UTC" if bf.get("asof_utc") else "")
    if bf.get("lag_min") and bf["lag_min"] >= 90:
        asof += f" ({round(bf['lag_min'] / 60)}h behind)"
    # Structure column stretches to the longest label in this window (typed
    # labels like "24JUL26/31JUL26 Call Diagonal" overflow a fixed 27).
    sw = max([27] + [len(row["structure"]) + 2 for row in bf["rows"]])
    vw = max([8] + [len(row.get("venue") or "") + 2 for row in bf["rows"]])
    L += ["```", "", f"**Block Flow (Paradigm RFQ) — ${bf['total_m']}M / {bf['n_blocks']} {block_word} / "
          f"{n_struct} {struct_word}{trunc}{asof}**",
          "", "```yaml",
          f"{'#':<3}{'Structure':<{sw}}{'Venue':<{vw}}{'Notl':<9}{'Blocks':<8}Detail",
          f"{'-':<3}{'-' * (sw - 2):<{sw}}{'-' * (vw - 2):<{vw}}{'-' * 7:<9}{'-' * 6:<8}{'-' * 40}"]
    for row in bf["rows"]:
        notl = f"${row['notl_m']}M"
        L.append(f"{str(row['rank']):<3}{row['structure']:<{sw}}{(row.get('venue') or ''):<{vw}}"
                 f"{notl:<9}{str(row.get('blocks', 1)):<8}{row['detail']}")
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

    block_rows: list[dict] = []
    if args.no_s3:
        # Offline/local: no hot CSVs or block tape; Deribit supplies DVOL/spot/
        # surface. Block flow is empty (No data) since it's S3-only now.
        hot = {"tickers": {}}
        deri = fetch_deribit(ASSET, start_ms, now_ms, want_market=True)
    else:
        # Parallelize the DuckDB read (hot CSVs + block tape) with the Deribit 7d
        # closes fetch (the realized-vol input) — both are network-bound.
        with ThreadPoolExecutor(max_workers=2) as ex:
            duck_fut = ex.submit(run_duckdb, args.duckdb_sql) if args.duckdb_sql else None
            deri_fut = ex.submit(fetch_deribit, ASSET, start_ms, now_ms, False)
            if duck_fut is not None:
                duck_fut.result()
            deri = deri_fut.result()
        hot = load_hot(args.csv_dir, ASSET)
        block_rows = load_blocks(args.csv_dir)
        # No hot dvol_spot row: the DuckDB read of the rolling recap-aggregates file
        # failed or returned nothing for this window. Either way DVOL/spot must come
        # from Deribit. Also fetch for any >24h window — the rolling file only
        # retains ~24h, so its OHLC silently under-covers longer windows (build()
        # then prefers the full-span Deribit series). Only pull the expensive
        # per-strike ticker surface when v_vol_surface also gave us nothing — for
        # a normal dynamic window vs_now is populated, so we skip ~50 serial
        # ticker calls (the bulk of the cost).
        if hot.get("dvol") is None or (now_ms - start_ms) > 24 * 3600_000:
            want_surface = not hot.get("vs_now")
            try:
                deri["market"] = _fetch_market_fallback(
                    ASSET, start_ms, now_ms, want_surface=want_surface)
            except Exception as e:  # noqa: BLE001
                warn(f"deribit market fallback failed: {e}")

    result = build(asset, args.window, start_ms, now_ms, deri, hot, block_rows)
    if args.render:
        print(render_md(result))
    else:
        print(json.dumps(result, indent=2 if args.pretty else None, default=str))


if __name__ == "__main__":
    main()
