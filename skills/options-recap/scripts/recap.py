#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Deterministic recap calculations and rendering. The live entrypoint supplies
non-hot partitioned inputs through direct_inputs.py; CSV inputs are retained
for offline fixtures.

recap.py — single-call orchestrator for the prior options recap implementation.

ONE invocation does the entire recap: it fetches the Deribit 30d closes (the
realized-vol input), ingests the DuckDB-written CSVs (hot surface + the
multi-venue block tape), runs the vol math (realized-vs-implied, block
ranking/rollup, vol-surface skew/term), and prints ONE JSON object whose fields
map 1:1 to the four output sections.

Pipeline (concurrent where independent):
  • Deribit 30d hourly closes       → realized vol (no non-Deribit source)
  • blocks.csv (DuckDB, tape)       → Biggest Print + Block Flow, across ALL
                                      venues Paradigm brokers (Deribit/Paradex/
                                      Bullish/…), notional already in USD per leg
  • hot CSVs in --csv-dir (DuckDB)  → DVOL/spot OHLC, $ Volume, activity+P/C
                                      trade counts, vol surface (markIV/delta)

Hot CSVs are authoritative for DVOL/spot/$Volume/activity/P-C/surface. Biggest
Print + Block Flow come from the Paradigm block tape (paradigm_trade_tape_slim)
— multi-venue, S3-sourced, no live exchange API. The tape carries no IV, so the
top blocks' IV is looked up from the vol surface (Deribit legs only). Deribit
still supplies the 30d realized-vol closes; nothing else hits an exchange API.

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
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data-discovery", "scripts"))
from http_client import get  # noqa: E402
from vol_math import (  # noqa: E402
    realized_vs_implied,
    build_tape_blocks,
    MIN_BLOCK_NOTIONAL_USD,
    compute_vol_surface,
    tape_block_key,
    _TAPE_VENUE as _VOL_MATH_VENUE_CODES,
    RV_LOOKBACK_DAYS,
    MAX_SURFACE_ROWS,
)

DERIBIT = "https://www.deribit.com/api/v2/public"
WARNINGS: list[str] = []


# ── Freshness gate ──────────────────────────────────────────────────────────
# How far behind the clock each HEARTBEAT source may fall before the recap stops
# treating it as live. See run_recap.sh's freshness.csv comment for why only
# continuously-written sources are probed and event-driven ones must not be.
#
# The limits are the publish cadence plus generous slack, sized so normal
# operation never trips them and a dead feed always does. Measured against a
# healthy pipeline on 2026-08-09: recap aggregates ran ~13-15 min behind (they
# are 5-min buckets and the open bucket is not yet published), the vol surface
# 8m30s. The failure this exists to catch was ~3.5 WEEKS, so precision here
# buys nothing — a wide margin that never cries wolf is worth far more than a
# tight one that trains people to ignore the banner.
STALENESS_LIMIT_S = {
    "recap_aggregates": 45 * 60,
    "vol_surface": 45 * 60,
}
# Sources whose staleness invalidates the Snapshot's DVOL/spot specifically, and
# so should divert those fields to the live Deribit fallback rather than merely
# annotate them.
_SNAPSHOT_SOURCES = ("recap_aggregates",)


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
    data = get(f"{DERIBIT}/{path}", params, timeout=timeout).json()
    if "error" in data:
        raise RuntimeError(f"Deribit {path}: {data['error']}")
    return data["result"]


def fetch_rv_closes(asset: str, end_ms: int) -> list[float]:
    start_ms = end_ms - RV_LOOKBACK_DAYS * 86400_000
    res = _get("get_tradingview_chart_data", {
        "instrument_name": f"{asset}-PERPETUAL", "resolution": "60",
        "start_timestamp": start_ms, "end_timestamp": end_ms,
    })
    return res.get("close") or []


def fetch_deribit(asset: str, start_ms: int, end_ms: int, want_market: bool) -> dict:
    """Always: RV closes (the realized-vol input; no non-Deribit source). If
    want_market (no S3), also DVOL, spot OHLC and a vol-surface ticker set so the
    pipeline runs end-to-end. Block flow no longer comes from here — it's the
    multi-venue Paradigm tape (blocks.csv), so no window-trade fetch."""
    res: dict = {"closes": [], "market": None}
    try:
        res["closes"] = fetch_rv_closes(asset, end_ms)
    except Exception as e:
        warn(f"deribit RV closes failed: {e}")
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


# One CSV per source, written by separate COPY statements — see run_recap.sh.
_FRESHNESS_FILES = {"recap_aggregates": "freshness_rec.csv",
                    "vol_surface": "freshness_vs.csv"}


def load_freshness(csv_dir: str) -> dict:
    """{source: max_at_ms or None}. None means the probe did not yield a
    usable timestamp; the key is ALWAYS present for every known source.

    Reporting None rather than omitting the key is the point. A probe can fail
    for reasons that say nothing about the data — the COPY erroring, the file
    never being written, the column being renamed, a value that will not parse
    — and every one of those used to leave the source simply absent, which
    check_freshness then read as "nothing to report" and the recap rendered a
    clean bill of health. A gate that cannot distinguish "fresh" from "I could
    not tell" is the original bug with extra steps."""
    out: dict = {}
    for source, filename in _FRESHNESS_FILES.items():
        at = None
        for row in _read_csv(csv_dir, filename):
            # `source` column is informational; the FILE identifies the source,
            # so a renamed/absent column cannot silently orphan the reading.
            value = _num(row, "max_at")
            if value is not None:
                at = int(value)
                break
        out[source] = at
    return out


def check_freshness(fresh: dict, now_ms: int) -> list[dict]:
    """Every heartbeat source that is not demonstrably fresh, worst first.

    Two statuses, and the distinction matters to the reader:
      stale   — read fine, too old. Lag is known.
      unknown — the probe produced no usable timestamp, so freshness CANNOT be
                asserted either way.

    `unknown` deliberately does NOT fail open. The earlier version skipped it,
    reasoning that the existing `hot['dvol'] is None` path already covers an
    unreadable source. That holds for ABSENT data, but the failure this gate
    exists for is stale-but-POPULATED: there `hot['dvol']` is set, nothing
    diverts, nothing banners, and a silently disabled gate is indistinguishable
    from an all-clear. Verified reachable three ways — a failing COPY writing
    zero bytes, the SQL column being renamed, and a value that will not parse.
    So an unverifiable source is surfaced and treated as not-live."""
    out = []
    for source, limit_s in STALENESS_LIMIT_S.items():
        at = fresh.get(source)
        if at is None:
            out.append({"source": source, "status": "unknown",
                        "lag_s": None, "limit_s": limit_s})
            continue
        lag_s = int((now_ms - at) / 1000)
        if lag_s > limit_s:
            out.append({"source": source, "status": "stale",
                        "lag_s": lag_s, "limit_s": limit_s})
    # unknown first (freshness unverifiable outranks a known lag), then by lag.
    return sorted(out, key=lambda d: (d["lag_s"] is not None, -(d["lag_s"] or 0)))


# Two INDEPENDENTLY gated groups. A single list gated on `dvol` alone popped the
# spot keys too, so a half-successful Deribit fetch — get_volatility_index_data
# succeeds, get_tradingview_chart_data returns 200 with no series — destroyed
# spot and then reported "re-sourced live from Deribit". That is the
# false-liveness class this whole gate exists to eliminate, and it was a
# regression FROM the surface_spot fix: before surface_spot joined the drop set
# it backstopped `spot`, so the path degraded instead of blanking.
_STALE_DVOL_FIELDS = ("dvol", "dvol_open", "dvol_low", "dvol_high")
# surface_spot is a COPY of spot_close (load_hot seeds it from the same value)
# and build() ranks it ABOVE the Deribit value — `spot_close or surface_spot or
# spot_now` — so it belongs to the SPOT group or the stale price survives one
# slot down, feeding vol_usd, venue-tape block notionals and surface moneyness.
_STALE_SPOT_FIELDS = ("spot_close", "spot_open", "spot_low", "surface_spot")


def _usable_spot(market: dict | None) -> bool:
    """A spot series we can actually render from. `market["spot"]` can be a
    truthy dict with an empty close list (status: no_data, a rate limit, a thin
    instrument), which is why presence is not enough."""
    m = market or {}
    return bool((m.get("spot") or {}).get("close")) or m.get("spot_now") is not None


def drop_stale_snapshot_fields(hot: dict, market: dict | None) -> dict:
    """Drop each stale group only where a live replacement exists.

    Returns {"dvol": bool, "spot": bool} — whether each group was replaced.
    Gating them together meant one endpoint failing blanked the other's data
    under a success banner. Refuses to drop a group with no replacement: stale
    figures plus an accurate banner beat blank ones, and the caller reports it."""
    replaced = {"dvol": False, "spot": False}
    if (market or {}).get("dvol"):
        for k in _STALE_DVOL_FIELDS:
            hot.pop(k, None)
        replaced["dvol"] = True
    if _usable_spot(market):
        for k in _STALE_SPOT_FIELDS:
            hot.pop(k, None)
        replaced["spot"] = True
    return replaced


def _fmt_lag(seconds: int) -> str:
    """Human lag: the banner's whole job is making a month-long gap obvious at
    a glance, and '2179800s' does not do that."""
    seconds = max(0, int(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


# Maps a venue id to its display label. deribit and deribit-usdc are distinct
# production venues (BTC-inverse vs USDC-linear) that both render as "Deribit" —
# activity is aggregated by this label so they collapse into one Activity entry.
_VENUE_LABELS = {"deribit": "Deribit", "deribit-usdc": "Deribit",
                 "okex-options": "OKX", "bybit-options": "Bybit",
                 "bullish": "Bullish"}


# Gap lines name ONE venue each, so they must not fold — two "Deribit: ..." lines
# with different numbers is worse than the raw id. Shares and Coverage do fold,
# deliberately, because there the two ids are one market to the reader.
_VENUE_NAMES = dict(_VENUE_LABELS, **{"deribit-usdc": "Deribit USDC"})


def venue_name(exchange: str) -> str:
    """Display name for one venue in a ⚠ line. Same vocabulary the Snapshot uses,
    so `okex-options` never appears three lines above `OKX` for the same venue."""
    e = (exchange or "").lower()
    return _VENUE_NAMES.get(e, (exchange or "?").split("-")[0].title())


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
           "turnover_usd": None, "turnover_complete": False,
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

    # Volume / P/C. Three reads, each on a basis that's honest for its scope:
    #   • Dollar volume — `turnover_usd` is the upstream per-trade USD premium
    #     (priced at each trade's own index, contract multipliers applied at
    #     ingestion from the venue instrument spec), so it sums truthfully across
    #     ALL venues. Null/absent cells contribute nothing; if NO row carries it
    #     (pre-upgrade recap file), build() falls back to the Deribit-scoped
    #     volume_sum × spot calc below and labels the line accordingly.
    #   • Deribit coin volume — the fallback basis: ONLY Deribit is priced in USD
    #     reliably without turnover_usd (1 contract = 1 BTC); `volume_sum` units
    #     differ by venue, so this sum never crosses venues.
    #   • Activity + P/C — `trade_count` is unit-free (a trade is a trade), so it
    #     aggregates across ALL venues truthfully, with no contract multiplier.
    # Blank-optionType rows are per-exchange aggregates that double-count — drop them.
    vol = [r for r in _own_asset_rows(_read_csv(csv_dir, "volume.csv"), asset, "volume.csv")
           if (r.get("optionType") or "").strip()]
    if vol:
        tus = [t for t in (_num(r, "turnover_usd") for r in vol) if t is not None]
        out["turnover_usd"] = sum(tus) if tus else None
        # "all venues" must mean ALL: during a partial upstream rollout some
        # venues' cells are null and contribute $0 — presenting that sum as
        # an all-venue total under-reports while claiming completeness. The
        # label upgrade is gated on every venue that traded carrying at
        # least one non-null turnover cell.
        venues_traded = {(r.get("exchange") or "").lower() for r in vol}
        venues_with_tu = {(r.get("exchange") or "").lower() for r in vol
                          if _num(r, "turnover_usd") is not None}
        out["turnover_complete"] = bool(tus) and venues_traded == venues_with_tu
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


def load_venue_blocks(csv_dir: str, asset: str) -> list[dict]:
    """Read venue_blocks.csv — OPTION block/OTC prints off the EXCHANGES' own
    tapes (the hot recap file's `block` rows, grouped per block id in DuckDB,
    `instrument_kind='option'` — a perp/spot OTC block must never compete in
    an options recap). Missing file → [] (the Paradigm tape still renders
    alone). Dedup against the Paradigm tape happens in _dedupe_venue_blocks.

    Columns are unit-explicit: `volume_coin` (Σ leg amounts, coin units) and
    `premium_usd` (Σ premium — carried for debuggability, NEVER displayed as
    notional: it is ~50-100x smaller than the underlying-USD basis the block
    sections use). Underlying notional is derived later as volume_coin × spot.
    """
    rows = _own_asset_rows(_read_csv(csv_dir, "venue_blocks.csv"), asset,
                           "venue_blocks.csv")
    # Belt to the SQL's WHERE: a row that still carries a non-option kind
    # (older CSV shape) is dropped here too.
    return [r for r in rows
            if (r.get("instrument_kind") or "option") == "option"]


# Venues Paradigm brokers — a block on any of these CAN appear on BOTH the
# Paradigm tape and the exchange's own tape, so merging its venue-tape copy
# would double-count the brokered flow. Venues NOT in this set are never
# brokered by Paradigm, so their venue-tape blocks have zero overlap with the
# Paradigm tape and always merge — OKX today (Bybit has no group id, so it
# never reaches `block` rows at all).
#
# For the brokered venues the dedupe is now EXACT where the data allows:
# blocks.csv (the hot paradigm_trade tape) carries VENUE_BLOCK_TRADE_ID —
# the venue's OWN block id (Deribit `BLOCK-…`, Bullish otc id), the same id
# the venue tape's `block_id` column carries — so a venue-tape block that
# matches a brokered id is the SAME print and is dropped, while a
# genuinely non-Paradigm Deribit/Bullish block merges into Block Flow.
# Guard rails, both falling back to the old structural exclusion (never
# double-count on uncertainty):
#   - tape without the column / no stamped ids in the window → structural
#     for every brokered venue (pre-migration behavior, byte-identical);
#   - PER-VENUE coverage gate: if any tape block row ON THAT VENUE lacks
#     the id (e.g. unstamped metadata), that venue's tape copies can't be
#     matched by id, so its venue-tape rows stay excluded structurally.
_TAPE_VENUE_CODE = {
    "deribit": "DBT",
    "deribit-usdc": "DBT",
    "paradex": "PRDX",
    "bullish": "BLSH",
}
_TAPE_BROKERED_VENUES = set(_TAPE_VENUE_CODE)


def _tape_venue_code(tape_row: dict) -> str:
    """The venue code off a tape row's PRODUCT ('BTC OPTION - DBT');
    unknown shape -> '?' (treated as covering NO venue safely)."""
    product = tape_row.get("PRODUCT") or ""
    return product.rsplit(" - ", 1)[-1].strip().upper() if " - " in product else "?"


def _tape_block_id(tape_row: dict) -> str:
    """The id vol_math will actually block this row under.

    MUST match build_tape_blocks' key (`BLOCK_TRADE_ID or TRADE_ID`). A
    narrower definition here — BLOCK_TRADE_ID only — let a row that DOES
    become a block in Block Flow be invisible to the coverage gate, so a
    venue was treated as fully id-covered when it wasn't."""
    return tape_block_key(tape_row) or ""


def _dedupe_venue_blocks(venue_rows: list[dict],
                         tape_rows: list[dict] | None = None, tape_available: bool = True) -> tuple[list[dict], list[dict]]:
    """Venue-tape blocks minus anything that could be a Paradigm-brokered
    duplicate.

    The pre-id design made double-counting STRUCTURALLY impossible for the
    brokered venues: they were simply never merged. Exact-id dedupe is more
    precise but it trades that invariant for string equality in which a
    NON-match means merge — so any benign format difference between the two
    independent pipelines (`BLOCK-280624` vs `280624`, case, zero-padding)
    silently double-counts the headline number instead of failing closed.

    So the id path is only trusted for a venue once it has PROVED itself on
    that venue, in this window, by matching at least once. Until then the
    venue keeps the structural exclusion. A format mismatch therefore
    degrades to the old, safe behaviour (a genuinely non-Paradigm block is
    missed) rather than to double-counting, and every gate below fails in
    that same direction — for BROKERED venues. A venue outside
    _TAPE_VENUE_CODE is not deduped at all and merges unconditionally, which is
    a real double-count path if such a venue ever appears on both tapes.

    The guarantee is conditional, not absolute: a double count requires BOTH a
    format regression AND full-coverage proof to have been granted anyway. The
    coverage rule is designed to withhold proof in precisely that case, but it
    is evidence, not a proof of correctness, so the claim is stated as the
    realistic failure direction rather than an impossibility."""
    tape_rows = tape_rows or []

    # Ids are scoped PER VENUE. One global set let an id from one venue delete
    # a block on another: venue id spaces are independent and several are plain
    # numeric, so a Bullish id could erase a genuinely non-Paradigm OKX block —
    # a silent under-count on a venue Paradigm never brokers at all.
    ids_by_code: dict = {}
    unstamped_codes = set()
    for r in tape_rows:
        code = _tape_venue_code(r)
        venue_id = (r.get("VENUE_BLOCK_TRADE_ID") or "").strip()
        if venue_id:
            ids_by_code.setdefault(code, set()).add(venue_id)
        elif _tape_block_id(r):
            # A block row with no venue id: its venue-tape copy cannot be
            # matched, so that venue's coverage is incomplete.
            unstamped_codes.add(code)
    if not any(ids_by_code.values()):
        brokered = [r for r in venue_rows
                    if (r.get("exchange") or "").lower() in _TAPE_BROKERED_VENUES]
        others = [r for r in venue_rows
                  if (r.get("exchange") or "").lower() not in _TAPE_BROKERED_VENUES]
        if not tape_available or not tape_rows:
            # Nothing to double-count AGAINST, so the blocks stay. Keying on the
            # exception alone left the empty case deleting everything: the
            # producer stopped on 2026-09-12 and this branch then fired on every
            # run, dropping 109 Deribit blocks and $1.25bn of underlying
            # notional per day from Block Flow in silence.
            #
            # The two sub-cases keep different reasons. That is the ONLY thing
            # `tape_available` decides — it does not change what is kept — and
            # naming it here means the caller reads this decision instead of
            # recomputing the same signal from its own local, which is how the
            # two drifted apart.
            reason = "tape_unreadable" if not tape_available else "tape_empty"
            return venue_rows, [{"reason": reason, "rows": brokered}] if brokered else []
        # The tape is readable and simply carries no venue ids — the id space
        # really is unproven, so the conservative exclusion stands.
        return others, ([{"reason": "id_space_unproven", "rows": brokered}]
                        if brokered else [])

    # An UNPARSEABLE PRODUCT ('?') must remove trust, not silently grant it.
    # Previously '?' could only ever land in `unstamped_codes`, where it matched
    # no real venue code — so a malformed row disabled the very gate it should
    # have tripped. Treat it as compromising every brokered venue, since we
    # cannot tell which one it belonged to.
    # ANY code we do not recognise taints every brokered venue, not just the
    # no-separator '?' case. A parseable-but-unknown code (a three-token
    # `BTC OPTION - DBT - USDC`, a lowercase or padded suffix) otherwise landed
    # in ids_by_code, was filtered out of matched_codes, and tainted nothing —
    # so the remaining ids made DBT look fully covered and a Paradigm print
    # merged on top of its own tape copy. It also matters that run_recap.sh
    # reads token 2 via split_part while _tape_venue_code reads token N via
    # rsplit: they agree only for exactly-two-token products and diverge toward
    # INCLUSION, so the unknown-code path is reachable by ordinary drift rather
    # than by malformed data.
    # Known codes come from vol_math's venue list, not just the 4-entry dedupe
    # map: BYB and BIT are ordinary Paradigm-tape suffixes, so treating them as
    # "unrecognised" let a single Bybit print taint every brokered venue and
    # silently revert the merge for the whole window. Recognised-but-not-deduped
    # is a different thing from unrecognised.
    _known = set(_TAPE_VENUE_CODE.values()) | set(_VOL_MATH_VENUE_CODES)
    if not (set(ids_by_code) | unstamped_codes) <= _known:
        unstamped_codes |= _known

    # PASS 1 — which venues have PROVED their id space is comparable.
    #
    # The bar is FULL coverage, not one match. "At least one match" proves only
    # that some ids line up, so a PARTIAL format regression — 4 of 5 stamped
    # consistently — proved the venue on the 4 and then merged the 5th, which
    # is a Paradigm print counted twice. Mixed stamping is the realistic shape
    # of such a regression; a uniform mismatch is the one a soak catches
    # immediately. The venue-code pooling makes it worse: deribit and
    # deribit-usdc share DBT, so a match in one book would vouch for the other.
    #
    # So: every tape id on that venue must find a counterpart in the venue
    # tape. Then an unmatched venue-tape block cannot be a mis-formatted
    # Paradigm print — every Paradigm print is already accounted for — and is
    # therefore genuinely non-Paradigm and safe to merge. One unmatched tape id
    # is enough to withhold proof, because it is indistinguishable from a
    # Paradigm print whose id we failed to recognise.
    #
    # The cost is real and deliberate: a Paradigm block absent from the venue
    # tape entirely (it carries only option `block` rows) also withholds proof,
    # so the venue falls back to structural. That is the pre-PR behaviour and
    # the safe direction.
    venue_ids_seen: dict = {}
    for r in venue_rows:
        code = _TAPE_VENUE_CODE.get((r.get("exchange") or "").lower())
        block_id = (r.get("block_id") or "").strip()
        if code and block_id:
            venue_ids_seen.setdefault(code, set()).add(block_id)
    matched_codes = {
        code for code, tape_ids in ids_by_code.items()
        if code in _TAPE_VENUE_CODE.values()
        and tape_ids and tape_ids <= venue_ids_seen.get(code, set())
    }

    # PASS 2 — apply. Every branch fails toward EXCLUSION, so the worst case is
    # the pre-PR behaviour (a genuinely non-Paradigm block is missed) rather
    # than an inflated headline.
    out, dropped = [], {}
    for r in venue_rows:
        exchange = (r.get("exchange") or "").lower()
        code = _TAPE_VENUE_CODE.get(exchange)
        block_id = (r.get("block_id") or "").strip()
        if code is None:
            out.append(r)          # never brokered by Paradigm -> always merges
            continue
        if block_id and block_id in (ids_by_code.get(code) or set()):
            continue               # the same print, already on the Paradigm tape
        if code in unstamped_codes:
            dropped.setdefault("unstamped_tape_rows", []).append(r)
            continue               # incomplete id coverage -> structural
        if code not in matched_codes:
            dropped.setdefault("id_space_unproven", []).append(r)
            continue               # id space unproven for this venue -> structural
        out.append(r)
    return out, [{"reason": reason, "rows": rows} for reason, rows in dropped.items()]


def _price_at(row):
    """A venue block's own trade-time index, or None. NaN is truthy and would
    otherwise pass straight through an `or spot` fallback into round()."""
    px = _num(row, "index_px")
    return px if px is not None and px == px and px > 0 else None


# The venue-coverage vocabulary, in ascending severity. ONE list: `build` and
# `render_md` previously kept their own, so `feed_gap` counted as read on the
# Coverage line while the Activity line called it unread, on adjacent rows.
COVERAGE_STATES = ("complete", "quiet", "companion_gap", "feed_gap", "unknown", "unreadable")
# Its trade data is missing or unproven, so every other venue's share is a share
# of a short denominator.
COVERAGE_UNDERSTATES = ("feed_gap", "unknown", "unreadable")


def _understates(state) -> bool:
    """Whether this venue's absence shortens every other venue's denominator.
    An UNRECOGNISED state counts too — not knowing is not the same as having
    read it. No state at all does NOT: the hot path carries no coverage, and
    treating its silence as a claim marked every venue unread."""
    if state is None:
        return False
    return state not in COVERAGE_STATES or state in COVERAGE_UNDERSTATES


def _severity(state) -> int:
    """Rank within COVERAGE_STATES. An unrecognised state sorts worst rather
    than raising: `rank.index` took the entire render down with a ValueError
    where the lookup it replaced had degraded to a missing word."""
    try:
        return COVERAGE_STATES.index(state)
    except ValueError:
        return len(COVERAGE_STATES)


def _state(value):
    """A venue's coverage state. It is stored as (state, detail); comparing the
    tuple against a string silently disabled the Activity marker once already."""
    return value[0] if isinstance(value, (list, tuple)) else value


def _lost_blocks(excluded: list[dict], start_ms: int, spot: float | None) -> list[dict]:
    """Per exclusion, the blocks Block Flow would have shown but for it.

    Counting raw ROWS overstated it three ways: the dedupe gate looks wider than
    the window, _venue_tape_blocks drops rows with no id or no coin, and the
    $250k floor would have removed some of what survived anyway.
    """
    out = []
    for group in excluded:
        rows = [r for r in group["rows"] if _would_have_counted(r, start_ms)]
        blocks = [b for b in _venue_tape_blocks(rows, spot)
                  if (b.get("notional_usd") or 0) >= MIN_BLOCK_NOTIONAL_USD]
        if blocks:
            # `venue` and `unit_size`, not `exchange`/`volume_coin`: these are
            # built blocks, not tape rows, and reading the row keys printed
            # "68 ? block(s) ... 0 coin" against a real $682M exclusion.
            out.append({"reason": group["reason"],
                        "venues": sorted({b.get("venue") or "?" for b in blocks}),
                        "blocks": len(blocks),
                        "notional_m": round(sum(b["notional_usd"] for b in blocks) / 1e6, 2),
                        "coin": round(sum(b.get("unit_size") or 0 for b in blocks), 2)})
    return out


def _would_have_counted(row: dict, start_ms: int) -> bool:
    """Whether this row would have reached Block Flow but for the exclusion.

    A null `bucket_at` is NOT in-window: coercing it to 0 dropped such a row from
    Block Flow and from the note meant to say what was dropped, so it is named
    here explicitly rather than by accident.
    """
    at = _num(row, "bucket_at")
    return (at is not None and at >= start_ms
            and bool(row.get("block_id")) and bool(_num(row, "volume_coin")))


def _venue_tape_blocks(rows: list[dict], spot: float | None) -> list[dict]:
    """Shape venue-tape block rows into the block dicts build_tape_blocks
    merges (source="venue"). The venue tape carries totals per block — no leg
    geometry (expiry/strike/type/side) — so the structure label is the venue +
    "Block" (there is no per-row venue column; the label is where the venue
    shows) and the detail carries a compact "(venue tape)" provenance note.
    notional_usd = volume_coin × the block's own coin-weighted index price,
    falling back to window-close spot only when the venue rows carry none.
    Underlying-USD, the same basis as the Paradigm tape's NOTIONAL_VOLUME_USD —
    and now the same price EPOCH too: these blocks are ranked against Paradigm's
    trade-time figures for Biggest Print, so pricing them at the window close
    made the ranking a function of the spot move over the window. No price at
    all → skip with a warning, never guess."""
    if rows and not spot and not all(_price_at(r) for r in rows):
        # `any` was wrong: with no spot, one row carrying an index and one
        # without still reached `vol * (index_px or spot)` and multiplied by None.
        warn("venue-tape blocks skipped — no trade-time index or spot to price coin volume")
        return []
    out = []
    for r in rows:
        vol = _num(r, "volume_coin")
        if not vol or not r.get("block_id"):
            continue
        legs = int(_num(r, "leg_count") or 0)
        bucket_ms = _num(r, "bucket_at")
        venue = _venue_label(r.get("exchange"))
        detail = f"x{vol:g} — {legs or '?'} legs (venue tape)"
        out.append({
            "block_trade_id": r.get("block_id"),
            "rfq_id": r.get("block_id"),  # its own structure
            "structure": f"{venue} Block", "expiry": "",
            "venue": venue,
            "notional_usd": round(vol * (_price_at(r) or spot)),
            "unit_size": round(vol, 1),  # total coin size — legs unknown
            "side": "",
            # bucket_at is the block's first 5m bucket — ~5-min resolution,
            # hence the "~" prefix (the Paradigm tape has exact times).
            "time_utc": f"~{fmt_hhmm(int(bucket_ms))}" if bucket_ms else "",
            "detail": detail, "leg_count": legs,
            "source": "venue", "close_priced": not _price_at(r),
        })
    return out


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
          deri: dict, hot: dict, block_rows: list[dict] | None = None,
          venue_block_rows: list[dict] | None = None,
          stale: list[dict] | None = None,
          tape_available: bool | None = None, leg_ivs=None) -> dict:
    asset = asset.upper()
    # Defaulting to True silently kept the pre-PR deletion on whichever caller
    # forgot to pass it. Unset now means "read it off the rows you handed me",
    # which is the answer that caller would have computed anyway.
    if tape_available is None:
        tape_available = bool(block_rows)
    mkt = deri.get("market")
    window_h = (end_ms - start_ms) / 3600_000

    # Spot: the supplied evidence wins for windows it actually spans (~24h for
    # the old rolling aggregates file); past that its OHLC silently covered only
    # the file's retention — a 2d recap once quoted a 24h-scoped low under a
    # banner claiming full-window spot — so the Deribit market fetch (full
    # history) is authoritative instead, with the supplied value as fallback.
    #
    # DVOL is exempt when the caller SAYS its value spans the window. The direct
    # path reads dvol_window, bounded to the request whatever its width, and
    # drops it upstream when freshness cannot be proved — preferring the REST
    # fetch there computed that evidence and threw it away on every window over
    # a day. Injected legacy evidence makes no such claim and keeps the guard.
    prefer_mkt = window_h > 24
    dvol_close = hot.get("dvol"); dvol_open = hot.get("dvol_open")
    dvol_low, dvol_high = hot.get("dvol_low"), hot.get("dvol_high")
    spot_close = hot.get("spot_close"); spot_open = hot.get("spot_open")
    spot_low = hot.get("spot_low")
    dvol_spans_window = bool(hot.get("dvol_window_scoped"))
    if (dvol_close is None or (prefer_mkt and not dvol_spans_window)) and mkt and mkt.get("dvol"):
        d = mkt["dvol"]
        dvol_open = d[0][1]; dvol_close = d[-1][4]
        dvol_low = min(r[3] for r in d); dvol_high = max(r[2] for r in d)
    if (spot_close is None or prefer_mkt) and mkt and mkt.get("spot"):
        s = mkt["spot"]
        spot_open = (s.get("open") or [None])[0]
        spot_close = (s.get("close") or [None])[-1]
        spot_low = min(s.get("low") or [0]) or None

    spot = spot_close or hot.get("surface_spot") or (mkt or {}).get("spot_now")
    spot_from_venue_tape = False
    if not spot and hot.get("venue_index_close"):
        # Deribit's public API is the only spot source in direct mode, and when
        # it fails every venue block loses its price: Block Flow rendered
        # $0.0M / 0 blocks on a window holding 97 real blocks. The venue tape's
        # own trade-time index is already in memory, so use it and say so.
        spot = float(hot["venue_index_close"])
        # Reported through the result, not `warn()`: WARNINGS is discarded on the
        # --render path, which is the only path a reader sees, so the hedge on
        # this number was silent exactly where it mattered. Every other hedge in
        # this recap travels as a gap line.
        spot_from_venue_tape = True

    rv = realized_vs_implied(deri.get("closes") or [], dvol_close)

    # Volume ($): the upstream turnover_usd sum is a true cross-venue USD total
    # (per-trade, priced at trade time). On a recap file that predates the column
    # the line falls back to the old Deribit-scoped volume_sum × spot calc —
    # labeled as such in render_md via volume_scope, so nothing is overstated
    # either way. The rollup head-lags the live tape ~10-15 min, so a very thin
    # window may under-count the newest prints — an accepted trade-off now that
    # Block Flow is the multi-venue Paradigm tape, a different universe from this
    # line.
    turnover_usd = hot.get("turnover_usd")
    vol_btc = hot.get("volume_btc")
    if turnover_usd and hot.get("turnover_complete"):
        vol_usd = turnover_usd
        volume_scope = "all"
    else:
        # No turnover, or PARTIAL turnover (a venue traded with only null
        # cells — mid-rollout): a partial sum must not present as an
        # all-venue total, so fall back to the Deribit-scoped calc.
        vol_usd = vol_btc * spot if (vol_btc and spot) else None
        volume_scope = "deribit"
    # Activity + P/C use trade_count — unit-free, so they span ALL venues truthfully.
    pt, ct = hot.get("put_trades"), hot.get("call_trades")
    pc = round(pt / ct, 2) if pt is not None and ct else None
    tt = hot.get("trades_total")
    activity_split, activity_unread = None, []
    if tt:
        # Fold raw venue ids into display labels FIRST, so venues that share a label
        # (deribit + deribit-usdc → "Deribit") collapse into a single entry before
        # pct/sort — otherwise "Deribit" appears twice and the [:4] display cap can
        # push a real venue off the line. tt already spans all raw venues, so the
        # per-label pcts remain a correct share of total activity.
        by_label: dict[str, float] = defaultdict(float)
        # A venue that could not be READ contributes 0 trades, so it silently
        # left the denominator and inflated everyone else's share — a failed
        # Deribit read made Bybit look like more of the market than it was.
        # Carry its state instead of its absence.
        states = hot.get("venue_coverage") or {}
        unread = set()
        for v, n in (hot.get("trades_by_venue") or {}).items():
            label = _venue_label(v)
            if _understates(_state(states.get(v))):
                unread.add(label)
            by_label[label] += n
        # An unread venue's own share is unknowable, and `0%+` said nothing while
        # leaving the OTHER rows — the ones actually inflated by its absence —
        # unmarked. Drop it from the split and name it beside the line instead:
        # every pct there is then plainly a share of what was read.
        # The denominator has to drop with them. A feed_gap venue still
        # contributes SOME trades to `tt`, so dividing by `tt` left the shown
        # shares summing to less than 100% — `OKX 20% · Bybit 20%` under a line
        # promising the shares were of what was read. They are now.
        shown = {lbl: n for lbl, n in by_label.items() if lbl not in unread}
        read_total = sum(shown.values())
        activity_split = [
            {"venue": lbl, "pct": round(100 * n / read_total)}
            for lbl, n in sorted(shown.items(), key=lambda kv: -kv[1])
        ] if read_total else []
        activity_unread = sorted(unread)

    # Vol surface — v_vol_surface "now" snapshot is authoritative (it pairs with
    # the "open" snapshot for consistent window-over-window deltas); fall back to
    # the hot surface.csv, then the Deribit market set. surf_open drives the deltas.
    surf_spot = hot.get("surface_spot") or spot
    vs_now = hot.get("vs_now") or {}
    vs_open = hot.get("vs_open") or {}
    tickers = vs_now or hot.get("tickers") or (mkt or {}).get("tickers") or {}
    # Cap the "now" surface at the display limit so the term-structure label
    # describes exactly the tenors the table shows (not invisible back months).
    surf = (compute_vol_surface(tickers, surf_spot, max_expiries=MAX_SURFACE_ROWS,
                                as_of_ms=end_ms) if tickers else None)
    surf_open = compute_vol_surface(vs_open, surf_spot) if vs_open else None

    # Biggest Print + Block Flow: the multi-venue Paradigm block tape (blocks.csv),
    # ranked/rolled-up in vol_math. Notional is USD per leg on the tape, so this path
    # does no cross-venue normalization. Leg IVs come from `leg_ivs`, the surface
    # at each block's print time; without it the legs carry none.
    # Defense in depth: the DuckDB query already scopes blocks.csv to this asset,
    # but drop any stray other-asset row (PRODUCT '<ASSET> OPTION - …') before
    # ranking — a leaked ETH row must never win a BTC recap's Biggest Print.
    own_blocks = [r for r in (block_rows or [])
                  if (r.get("PRODUCT") or "").upper().startswith(f"{asset} ")]
    dropped = len(block_rows or []) - len(own_blocks)
    if dropped:
        warn(f"blocks.csv: dropped {dropped} non-{asset} rows — cross-asset contamination?")
    # Venue-tape blocks join the same pool (min-notional filter, Biggest Print
    # candidacy, top-N ranking on equal underlying-USD terms), after
    # _dedupe_venue_blocks removes anything that could be a Paradigm-brokered
    # duplicate — exact per-block id dedupe against this window's tape rows
    # where their VENUE_BLOCK_TRADE_ID coverage allows, the structural
    # brokered-venue exclusion otherwise (see _dedupe_venue_blocks).
    # The venue fetch is deliberately widened to the 5-minute bucket CONTAINING
    # window-open, because the coverage gate needs the counterpart of any
    # Paradigm print in that bucket. But roughly half of a straddling bucket
    # precedes the window, so those rows must not reach the ranked pool: a
    # pre-window block was entering Block Flow and could win Biggest Print,
    # rendering a timestamp outside the window banner printed above it.
    # "Not duplicated" and "in the window" are different claims — the widening
    # is correct for the GATE and wrong for the OUTPUT, so it is re-filtered
    # here rather than narrowed at the source.
    _deduped, _excluded = _dedupe_venue_blocks(venue_block_rows or [], own_blocks,
                                               tape_available=tape_available)
    _in_window = [r for r in _deduped
                  if (_num(r, "bucket_at") or 0) >= start_ms]
    venue_blocks = _venue_tape_blocks(_in_window, spot)
    block = build_tape_blocks(own_blocks, leg_ivs=leg_ivs,
                              extra_blocks=venue_blocks)

    # >24h flag: Volume/Activity/P-C/DVOL/spot come from the ~24h hot rollup, so a
    # longer window under-covers them (run_recap caps at 24h; this defends a direct
    # >24h call).
    hot_horizon = round(window_h) if window_h > 24 else None

    snapshot = {
        # Carried through so the Snapshot can lead with how much of the window
        # was actually read; every figure beside it is a function of that.
        "venue_coverage": hot.get("venue_coverage") or {},
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
        "rv": rv.get("value"), "vrp": rv.get("vrp"), "vrp_label": rv.get("vrp_label"),
        "volume_usd_m": round(vol_usd / 1e6) if vol_usd else None,
        "volume_scope": volume_scope,
        "activity_trades": tt,
        "activity_split": activity_split,
        "activity_unread": activity_unread,
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
        for e in surf.get("expiries", []):
            o = open_by_exp.get(e["expiry"])
            rows.append({
                "expiry": e["expiry"], "atm": e["atm_iv"],
                "rr_25d": e["rr_25d"], "fly": e["fly_25d"],
                "d_atm": _delta(e["atm_iv"], "atm_iv", o),
                "d_rr": _delta(e["rr_25d"], "rr_25d", o),
                "d_fly": _delta(e["fly_25d"], "fly_25d", o),
                "extrapolated": e["wings_extrapolated"],
                "atm_extrapolated": e.get("atm_extrapolated", False),
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
                       "n_venue_blocks": block.get("n_venue_blocks", 0)},
        "vol_surface": surface_out,
        "hot_horizon": hot_horizon,
        "stale_sources": stale or [],
        "warnings": list(WARNINGS),
        # What Block Flow ACTUALLY lost: the excluded rows run through the same
        # aggregation and the same floor the totals use, so the count is the
        # blocks that would have appeared, not the rows that were removed.
        "block_exclusions": _lost_blocks(_excluded, start_ms, spot),
        "blocks_below_floor": block.get("trimmed", {}),
        "spot_from_venue_tape": spot_from_venue_tape,
        # Only blocks ranked at the closing spot: a venue whose blocks were all
        # excluded, unpriced or under the floor never was.
        "close_priced_venues": sorted({b["venue"] for b in venue_blocks if b["close_priced"]
                                       and b["notional_usd"] >= MIN_BLOCK_NOTIONAL_USD}),
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
    for gap in r.get("source_gaps", []):
        L.append(f"⚠ {gap}")
    if r.get("source_gaps"):
        L.append("")

    # STALENESS FIRST. This banner outranks the others because it is the only
    # one that says the numbers below may be WRONG rather than missing — a
    # reader who spots "No data" knows not to trust it, but silently old DVOL
    # and spot look exactly like live ones. Ordering it first is the whole
    # point: it is what was absent when a frozen feed rendered for 3.5 weeks.
    for st in (r.get("stale_sources") or []):
        if st.get("status") == "unknown":
            age = "freshness could not be verified"
        else:
            age = (f"last updated {_fmt_lag(st['lag_s'])} ago "
                   f"(limit {_fmt_lag(st['limit_s'])})")
        L.append(f"⚠ {st['source']}: {age}.")
        # Name the CONSEQUENCE per source, not one generic line. "figures
        # sourced from it are NOT live" left the reader unable to tell which
        # figures, whether the divert worked, or that the window was truncated.
        if st["source"] == "recap_aggregates":
            kept = st.get("retained_groups") or []
            if kept:
                got = [g for g in ("dvol", "spot") if g not in kept]
                line = f"   {'/'.join(kept)} could NOT be re-sourced — those values below are stale."
                if got:
                    line += f" ({'/'.join(got)} re-sourced live from Deribit.)"
                L.append(line)
            else:
                L.append("   DVOL/spot re-sourced live from Deribit.")
            # These come from the SAME parquet but are windowed by bucket_at, so
            # a partial freeze silently truncates them — they cover only up to
            # the freeze, under a header claiming the full window. The Snapshot
            # divert does not help them, and an understated volume that looks
            # precise is the same class of harm as the stale DVOL.
            L.append("   $ Volume · Activity · P/C · venue Block Flow come from "
                     "the same source and cover only up to that point — they "
                     "UNDERSTATE the window.")
        elif st["source"] == "vol_surface":
            L.append("   Vol Surface (ATM/RR/Fly, skew, term) and its Δ columns "
                     "are from that data. A stale surface does not itself "
                     "trigger a refetch — only recap_aggregates does — so the "
                     "Deribit ticker surface backfills these only when that "
                     "fallback runs for another reason.")
    if r.get("stale_sources"):
        L.append("")

    # THIRD cycle for this: the zero-row warn() lands in WARNINGS, and WARNINGS
    # is discarded on --render — the only path a user sees. So a dead migration
    # (hot tape never promoted) was indistinguishable from a healthy recap.
    # Rendered explicitly rather than routed through the warning machinery.
    if r.get("block_tape_empty"):
        L.append("⚠ Block Flow unavailable — the hot paradigm_trade read returned "
                 "no rows. Biggest Print and Block Flow below are NOT a quiet "
                 "market, they are a missing feed.")
        L.append("")

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

    # Everything appended so far is a warning banner. It used to sit ABOVE the
    # header, outside any fence — and the relaying model demonstrably copies
    # fenced blocks verbatim and drops the prose around them: on 2026-09-08 a
    # /recap relay kept every Snapshot figure and deleted all three ⚠ lines
    # (Bullish partial, a 66-minute Paradigm coverage shortfall, 13k unvalued
    # trades). The lines that say what NOT to trust must travel with the
    # numbers they qualify, so they are emitted as the first lines INSIDE the
    # Snapshot fence, where they cannot be dropped without dropping Snapshot.
    banner, L = L, []
    while banner and banner[-1] == "":
        banner.pop()
    L.append(f"**{h['asset']} Options · {h['window']} Recap · "
             f"{h['start_utc']}–{h['end_utc']} UTC**")
    L += ["", "**Snapshot**", "", "```yaml"]
    if banner:
        L += banner + [""]

    # Coverage leads the Snapshot: every figure below is a function of how much
    # of the window was actually read, and a reader cannot infer that from the
    # numbers themselves. Inside the fence for the same reason the warnings are
    # — on 2026-09-08 the relay kept every Snapshot figure and deleted all three
    # unfenced ⚠ lines.
    states = s.get("venue_coverage") or {}
    if states:
        # `quiet` is "hours with no prints", NOT "this venue never traded" —
        # Bullish traded 341 of 720 hours and would have read `Bullish no
        # trades` beside `Bullish 40%` in the same fence.
        _WORDS = {"complete": None, "quiet": "quiet hours", "feed_gap": "feed gap",
                  "companion_gap": "quote gap", "unreadable": "READ FAILED",
                  "unknown": "unverified"}
        _UNKNOWN_WORD = "state not recognised"
        # Counted over the SAME folded labels the notes use: counting raw venue
        # ids printed `3/5 venues` beside four labels, a denominator the
        # Activity line below could not be reconciled with.
        by_label: dict = {}
        for venue, state in states.items():
            label = _venue_label(venue)
            # Worst state wins when two ids fold into one label.
            current = by_label.get(label)
            if current is None or _severity(_state(state)) > _severity(current):
                by_label[label] = _state(state)
        read = sum(1 for st in by_label.values() if not _understates(st))
        notes = [f"{label} {_WORDS.get(st, _UNKNOWN_WORD)}"
                 for label, st in by_label.items()
                 if st not in _WORDS or _WORDS[st]]
        detail = " · ".join(notes) if notes else "all venue feeds complete"
        L.append(f"{'Coverage':<9} {f'{read}/{len(by_label)} venues':<11} {detail}")

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
    rich = ("unavailable" if vrp is None else "CHEAP" if vrp is not None and vrp < -1 else
            "RICH" if vrp is not None and vrp > 1 else "IN LINE")
    rv = f"{s['rv']}v" if s.get("rv") is not None else "n/a"
    L.append(f"{f'RV {RV_LOOKBACK_DAYS}d':<9} {rv:<11} implied {rich} vs realized")

    vrp_txt = f"{vrp:+}v" if vrp is not None else "n/a"
    # Same ±1v dead-band as the RV line above — otherwise a VRP in (0,1] prints
    # "IN LINE" and "overpriced" on adjacent lines.
    upo = ("unavailable" if vrp is None else "underpriced" if vrp is not None and vrp < -1 else
           "overpriced" if vrp is not None and vrp > 1 else "roughly fair")
    L.append(f"{'VRP':<9} {vrp_txt:<11} vol {upo} vs delivered")

    # Activity always renders — an empty window reads n/a like Volume/P-C do;
    # silently dropping the line makes the Snapshot shape depend on the data.
    if s.get("activity_trades"):
        tt = s["activity_trades"]
        tnum = (f"{tt / 1e6:.1f}M" if tt >= 1e6 else
                f"{round(tt / 1e3)}k" if tt >= 1e3 else f"{int(tt)}")
        # No `+` floor marker: an unread venue is named after the line instead
        # of carrying a share, so nothing on the line is a floor any more.
        split = " · ".join(f"{v['venue']} {v['pct']}%"
                           for v in (s.get("activity_split") or [])[:4])
        # Naming the unread venue is what makes the other shares readable: they
        # are shares of what was read, and the denominator is short by it.
        unread = s.get("activity_unread") or []
        note = ("by trade count" if not unread else
                f"by trade count; {', '.join(unread)} unread — shares are of what was read")
        # An all-unread window leaves `split` empty; the separator would dangle.
        body = f"trades — {split} ({note})" if split else f"trades ({note})"
        L.append(f"{'Activity':<9} {tnum:<11} {body}")
    else:
        L.append(f"{'Activity':<9} {'n/a':<11} trades (by trade count)")
    vol = f"${s['volume_usd_m']}M" if s.get("volume_usd_m") else "n/a"
    # "all venues" when the cross-venue turnover_usd sum drove the number;
    # the Deribit-scoped label survives only on the pre-upgrade fallback.
    vol_note = ("all venues" if s.get("volume_scope") == "all" else
                "Deribit only" if s.get("volume_scope") == "deribit" else s.get("volume_scope", "unavailable"))
    L.append(f"{'Volume':<9} {vol:<11} {vol_note}")
    pc = f"{s['pc_ratio']}x" if s.get("pc_ratio") is not None else "n/a"
    pc_desc = f"{s['pc_descriptor']} " if s.get("pc_descriptor") else ""
    L.append(f"{'P/C':<9} {pc:<11} {pc_desc}({s.get('activity_scope', 'all venues, by trades')})")
    L += ["```", "", "**Biggest Print**", "", "```yaml"]

    if bp:
        # The detail is the same leg list Block Flow shows: each leg's size and
        # taker side, so the line never needs a separate size or side slot.
        via = ("via venue tape" if bp.get("source") == "venue"
               else f"via Paradigm/{bp.get('venue') or '?'}")
        label = f"{bp['expiry']} {bp['structure']}".strip()  # venue blocks have no expiry
        detail = (bp.get("detail") or "").replace(" (venue tape)", "")
        L.append(f"{label}   ${bp['notional_m']}M   {bp['time_utc']} UTC   "
                 f"{via}   {detail}".rstrip())
    else:
        # output-format.md: name the source and reason rather than going blank.
        # True whichever way the pool emptied — no blocks at all, all excluded by
        # the Paradigm dedupe, or all below the floor. The gaps above say which.
        L.append("Unavailable — no qualifying block in this window; any blocks "
                 "excluded from the totals are listed above.")
    n_struct = bf.get("n_structures", len(bf["rows"]))
    struct_word = "structure" if n_struct == 1 else "structures"
    block_word = "block" if bf["n_blocks"] == 1 else "blocks"
    trunc = f" (top {len(bf['rows'])} by notional)" if n_struct > len(bf["rows"]) else ""
    # Structure column stretches to the longest label in this window (typed
    # labels like "24JUL26/31JUL26 Call Diagonal" overflow a fixed 27). Per-row
    # venue isn't a column — the Biggest Print line's via Paradigm/<venue> tag
    # is where the venue shows.
    sw = max([27] + [len(row["structure"]) + 2 for row in bf["rows"]])
    L += ["```", "", f"**Block Flow — ${bf['total_m']}M notional / {bf['n_blocks']} {block_word} / "
          f"{n_struct} {struct_word}{trunc}**",
          "", "```yaml",
          f"{'#':<3}{'Structure':<{sw}}{'Notl':<9}{'Blocks':<8}Detail (+ taker bought, - taker sold)",
          f"{'-':<3}{'-' * (sw - 2):<{sw}}{'-' * 7:<9}{'-' * 6:<8}{'-' * 44}"]
    for row in bf["rows"]:
        notl = f"${row['notl_m']}M"
        L.append(f"{str(row['rank']):<3}{row['structure']:<{sw}}{notl:<9}"
                 f"{str(row.get('blocks', 1)):<8}{row['detail']}")
    L += ["```", "", "**Vol Surface**"]

    if vs and vs.get("rows"):
        fa, ba, term = vs.get("front_atm"), vs.get("back_atm"), vs.get("term_line")
        # The term label is read off front/back ATM, so an extrapolated ATM
        # anywhere makes the label itself provisional — the reason the ATM
        # column got its own star in the first place.
        term_star = "*" if any(r.get("atm_extrapolated") for r in vs["rows"]) else ""
        term_txt = (f"{fa}v → {ba}v → {term}{term_star}" if fa is not None and ba is not None
                    and term else (term or "n/a"))
        L.append(f"Skew: {vs.get('skew_line') or 'n/a'} · Term: {term_txt}")
        L += ["", "```yaml",
              f"{'Expiry':<11}{'ATM':<9}{'ΔATM':<9}{'25d RR':<10}{'ΔRR':<9}{'Fly':<8}ΔFly",
              f"{'-' * 9:<11}{'-' * 6:<9}{'-' * 6:<9}{'-' * 8:<10}{'-' * 6:<9}{'-' * 5:<8}{'-' * 6}"]
        for e in vs["rows"]:
            star = "*" if e.get("extrapolated") else ""
            # The ATM column gets its own star: a thin chain reaches ATM by
            # clamping to an endpoint just as the wings do, and that figure also
            # drives front/back ATM and the term-structure label.
            atm_star = "*" if e.get("atm_extrapolated") else ""
            atm = f"{e['atm']}v{atm_star}" if e.get("atm") is not None else "n/a"
            rr = f"{e['rr_25d']:+}v{star}" if e.get("rr_25d") is not None else "n/a"
            # Fly is (c25 + p25)/2 - atm, so it inherits BOTH clamps: it was
            # rendering bare beside a starred RR built from the same points.
            fly_star = star or atm_star
            fly = f"{e['fly']}v{fly_star}" if e.get("fly") is not None else "n/a"
            datm = _delta_fmt(e.get("d_atm"), atm_star)
            drr = _delta_fmt(e.get("d_rr"), star)
            dfly = _delta_fmt(e.get("d_fly"), fly_star)
            L.append(f"{e['expiry']:<11}{atm:<9}{datm:<9}{rr:<10}{drr:<9}{fly:<8}{dfly}")
        L.append("```")
    else:
        # output-format.md: a section states a specific source and reason rather
        # than going blank. "No data" reads as a quiet market; it never was one.
        L.append("Unavailable — no vol surface could be built from the window's "
                 "option_summary snapshots.")
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
    venue_block_rows: list[dict] = []
    # Bound before the branch: --no-s3 reads no hot sources at all, so there is
    # nothing to be stale, but build() is called on both paths.
    stale: list[dict] = []
    if args.no_s3:
        # Offline/local: no hot CSVs or block tape; Deribit supplies DVOL/spot/
        # surface. Block flow is empty (No data) since it's S3-only now.
        hot = {"tickers": {}}
        deri = fetch_deribit(ASSET, start_ms, now_ms, want_market=True)
    else:
        # Parallelize the DuckDB read (hot CSVs + block tape) with the Deribit 30d
        # closes fetch (the realized-vol input) — both are network-bound.
        with ThreadPoolExecutor(max_workers=2) as ex:
            duck_fut = ex.submit(run_duckdb, args.duckdb_sql) if args.duckdb_sql else None
            deri_fut = ex.submit(fetch_deribit, ASSET, start_ms, now_ms, False)
            if duck_fut is not None:
                duck_fut.result()
            deri = deri_fut.result()
        # AFTER DuckDB has run — this cannot live in run_recap.sh. That script
        # only WRITES recap.sql; the statements execute here, inside
        # run_duckdb(). A promotion gate placed in the shell therefore ran
        # before blocks_pt.csv could exist, so it never fired: the hot tape was
        # never promoted, VENUE_BLOCK_TRADE_ID was never present, and the
        # dedupe silently took the structural branch forever. That closed the
        # zero-row hole by opening its exact opposite.
        hot = load_hot(args.csv_dir, ASSET)
        block_rows = load_blocks(args.csv_dir)
        venue_block_rows = load_venue_blocks(args.csv_dir, ASSET)
        # No hot dvol_spot row: the DuckDB read of the rolling recap-aggregates file
        # failed or returned nothing for this window. Either way DVOL/spot must come
        # from Deribit. Also fetch for any >24h window — the rolling file only
        # retains ~24h, so its OHLC silently under-covers longer windows (build()
        # then prefers the full-span Deribit series). Only pull the expensive
        # per-strike ticker surface when v_vol_surface also gave us nothing — for
        # a normal dynamic window vs_now is populated, so we skip ~50 serial
        # ticker calls (the bulk of the cost).
        # FRESHNESS GATE. The three triggers below are distinct failures:
        #   dvol is None          -> the source could not be read at all
        #   window > 24h          -> the source is fine but under-covers
        #   stale_snapshot        -> the source read fine and is OUT OF DATE
        # The third had no trigger until now, so a frozen feed rendered as live:
        # hot['dvol'] was populated, the window was short, and the recap printed
        # three-week-old DVOL/spot with no indication anything was wrong.
        stale = check_freshness(load_freshness(args.csv_dir), now_ms)
        for s in stale:
            warn(f"{s['source']} {s['status']} "
                 f"({_fmt_lag(s['lag_s']) if s['lag_s'] is not None else 'no reading'}, "
                 f"limit {_fmt_lag(s['limit_s'])}) — treating as not live")
        stale_snapshot = any(s["source"] in _SNAPSHOT_SOURCES for s in stale)
        if (hot.get("dvol") is None or stale_snapshot
                or (now_ms - start_ms) > 24 * 3600_000):
            want_surface = not hot.get("vs_now")
            try:
                deri["market"] = _fetch_market_fallback(
                    ASSET, start_ms, now_ms, want_surface=want_surface)
            except Exception as e:  # noqa: BLE001
                warn(f"deribit market fallback failed: {e}")
        if stale_snapshot:
            # Record the OUTCOME on the entries the banner is built from. Without
            # this the banner reads identically whether the divert worked or the
            # stale figures survived, which leaves the reader unable to answer
            # the one question the feature exists for: are these numbers live?
            replaced = drop_stale_snapshot_fields(hot, deri.get("market"))
            kept = [g for g, ok in replaced.items() if not ok]
            for s in stale:
                if s["source"] in _SNAPSHOT_SOURCES:
                    s["retained"] = bool(kept)
                    s["retained_groups"] = kept
            if kept:
                # No "unavailable"/"missing"/"failed" wording: those substrings
                # route this into render_md's `crit` bucket, whose own guard
                # (volume_usd_m is None and vs is None) never fires on a
                # stale-but-present feed, so it would be silently swallowed.
                warn(f"stale hot {'/'.join(kept)} retained — no live replacement; "
                     "those Snapshot figures are NOT live")

    # The same signal computed below for `block_tape_empty`: --no-s3 never
    # attempts the read, so the tape is not "missing" there.
    result = build(asset, args.window, start_ms, now_ms, deri, hot, block_rows,
                   venue_block_rows, stale=stale,
                   tape_available=bool(args.no_s3 or block_rows))
    # With the legacy csv.gz read gone there is nothing to fall back TO, so an
    # empty blocks.csv is no longer "serving stale" — it is Block Flow missing
    # outright. Still rendered rather than warned: WARNINGS are discarded on
    # --render, which is the only path a user sees.
    # NOT on --no-s3: that path never attempts a block read at all, so the
    # banner's claim ("a missing feed") would be literally false on every
    # offline run — and a banner that cries wolf offline is exactly the
    # ignore-the-banner outcome this PR is trying to avoid.
    result["block_tape_empty"] = not args.no_s3 and not block_rows
    if args.render:
        print(render_md(result))
    else:
        print(json.dumps(result, indent=2 if args.pretty else None, default=str))


if __name__ == "__main__":
    main()
