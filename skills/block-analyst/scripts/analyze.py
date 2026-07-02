#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
analyze.py — single-call orchestrator for the block analyst.

ONE invocation does everything after the tape resolve: it reads the FILL/HIST
CSVs the DuckDB step wrote (from analyze.sh), parses the structure, fetches every
leg's Deribit ticker + 30d trade buckets CONCURRENTLY, computes net greeks /
direction / fill-offset / recurrence, and prints the finished block (--render).

The agent runs `bash scripts/analyze.sh <rfq_id>` and relays stdout. The only
piece it may finalise itself is the [Greeks] net line when the structure's signs
aren't reliably derivable from the tape (risk reversals, calendars, exotics) —
those rows are printed as per-leg greeks with a `⚠ net: confirm signs` marker and
all the numbers it needs are right there.

Deterministic + no per-turn tool orchestration ⇒ fast and run-to-run stable.
"""
import argparse
import csv
import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze_core as ac  # noqa: E402

DERIBIT = "https://www.deribit.com/api/v2/public"
WARN: list[str] = []


def warn(m):
    WARN.append(m)


def _read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _get(path, params, timeout=15):
    url = f"{DERIBIT}/{path}?{urlencode(params)}"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        d = json.loads(r.read())
    if "error" in d:
        raise RuntimeError(d["error"])
    return d["result"]


def fetch_ticker(sym):
    try:
        t = _get("ticker", {"instrument_name": sym})
        g = t.get("greeks") or {}
        return sym, {"mark": t.get("mark_price"), "bid": t.get("best_bid_price"),
                     "ask": t.get("best_ask_price"), "iv": t.get("mark_iv"),
                     "delta": g.get("delta"), "vega": g.get("vega"),
                     "gamma": g.get("gamma"), "theta": g.get("theta"),
                     "oi": t.get("open_interest"), "under": t.get("underlying_price"),
                     "index": t.get("index_price")}
    except Exception as e:  # noqa: BLE001
        warn(f"ticker {sym}: {e}")
        return sym, None


def fetch_trades_bucket(sym, now_ms):
    """30d prints/blocks/contracts by 24h/7d/30d for one instrument."""
    start = now_ms - 30 * 86400_000
    try:
        r = _get("get_last_trades_by_instrument",
                 {"instrument_name": sym, "count": 1000, "start_timestamp": start,
                  "end_timestamp": now_ms, "sorting": "desc"})
        t = r.get("trades") or []
    except Exception as e:  # noqa: BLE001
        warn(f"trades {sym}: {e}")
        return sym, None
    if not t:
        return sym, {"24h": (0, 0, 0.0), "7d": (0, 0, 0.0), "30d": (0, 0, 0.0), "oi_iv": None}
    latest = max(x["timestamp"] for x in t)

    def bucket(days):
        c = latest - days * 864e5
        w = [x for x in t if x["timestamp"] >= c]
        b = [x for x in w if x.get("block_trade_id")]
        return len(w), len(b), round(sum(x["amount"] for x in w), 1)
    return sym, {"24h": bucket(1), "7d": bucket(7), "30d": bucket(30)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv-dir", default="/tmp/analyze")
    ap.add_argument("--now-ms", type=int)
    ap.add_argument("--render", action="store_true")
    args = ap.parse_args()

    import time
    now_ms = args.now_ms or int(time.time() * 1000)

    fill = _read_csv(os.path.join(args.csv_dir, "fill.csv"))
    hist = _read_csv(os.path.join(args.csv_dir, "hist.csv"))
    if not fill:
        print("RFQ not resolved (not on Paradigm tape / id not ingested) — "
              "no asset/structure/fill available.")
        return

    prod = ac.parse_product(fill[0].get("PRODUCT", ""))
    asset = prod["asset"]
    desc = fill[0].get("DESCRIPTION", "")
    quote = (fill[0].get("QUOTE_CURRENCY") or "").upper()
    qty = ac._f(fill[0].get("QTY")) or 1.0
    parsed = ac.parse_description(desc)
    legs, side, reliable = ac.apply_orientation(parsed, fill)

    # instruments: each option leg + the perp for spot
    syms = []
    for l in legs:
        if l["cp"] != "FUT" and l.get("expiry_c"):
            l["_sym"] = ac.deribit_symbol(asset, l["expiry_c"], l["strike"], l["cp"])
            syms.append(l["_sym"])
    perp = ac.perp_symbol(asset)

    # fetch everything concurrently: tickers (legs+perp) + per-leg 30d trades.
    # Submit all up front so they run in parallel, then collect into typed maps.
    tickers, buckets = {}, {}
    with ThreadPoolExecutor(max_workers=min(12, 2 * len(syms) + 2)) as ex:
        tfuts = [ex.submit(fetch_ticker, s) for s in syms + [perp]]
        bfuts = [ex.submit(fetch_trades_bucket, s, now_ms) for s in syms]
        for f in tfuts:
            s, v = f.result()
            tickers[s] = v
        for f in bfuts:
            s, v = f.result()
            buckets[s] = v

    spot = None
    tp = tickers.get(perp)
    if tp:
        spot = tp.get("mark") or tp.get("index")
    if spot is None:
        for l in legs:
            tt = tickers.get(l.get("_sym"))
            if tt and tt.get("under"):
                spot = tt["under"]
                break

    # greeks per leg key
    greek_by_key = {}
    for l in legs:
        tt = tickers.get(l.get("_sym"))
        if tt:
            greek_by_key[ac.leg_key(l)] = tt
    ng = ac.net_greeks(legs, greek_by_key, qty) if reliable else {}

    # net fill vs mark (per unit), offset unit by quote
    fill_net = sum((1 if (r.get("SIDE") or "").upper() == "BUY" else -1) * (ac._f(r.get("PRICE")) or 0)
                   for r in fill)
    ref_net = sum((1 if (r.get("SIDE") or "").upper() == "BUY" else -1) * (ac._f(r.get("REF_PRICE")) or 0)
                  for r in fill)
    off = ac.offset(abs(fill_net), abs(ref_net), quote) if ref_net else {"txt": "n/a"}

    # recurrence: HIST blocks clustered by BLOCK_TRADE_ID
    blocks = {}
    for r in hist:
        b = r.get("BLOCK_TRADE_ID")
        if b:
            blocks.setdefault(b, []).append(r)
    recurrence = len(blocks)

    result = {
        "asset": asset, "venue": prod["venue"], "structure": parsed["code"],
        "desc": desc, "side": side, "qty": qty, "reliable_signs": reliable,
        "spot": spot, "quote": quote,
        "fill_net": round(fill_net, 6), "ref_net": round(ref_net, 6), "offset": off,
        "legs": [{"cp": l["cp"], "strike": l["strike"], "ratio": l["ratio"],
                  "sign": l["sign"], "expiry": l.get("expiry_c"), "sym": l.get("_sym"),
                  "tkr": tickers.get(l.get("_sym")), "trades": buckets.get(l.get("_sym"))}
                 for l in legs],
        "net_greeks": ng, "recurrence_blocks": recurrence, "warnings": WARN,
    }

    if args.render:
        print(render(result))
    else:
        print(json.dumps(result, default=str))


def _sk(strike):
    k = int(round(strike))
    return f"{k//1000}k" if k >= 1000 and k % 1000 == 0 else str(k)


def render(r) -> str:
    a = r["asset"]
    legs = r["legs"]
    exp = legs[0]["expiry"] if legs else "?"
    strikes = "/".join(_sk(l["strike"]) for l in legs if l["cp"] != "FUT")
    verb = "Paid" if r["fill_net"] >= 0 else "Recd"
    fillabs = abs(r["fill_net"])
    struct = {"CL": "Call", "PL": "Put", "ST": "Straddle", "SN": "Strangle",
              "RR": "Risk Reversal", "CO": "Iron Condor", "BF": "Butterfly",
              "CA": "Calendar", "CM": "Custom"}.get(r["structure"], r["structure"])
    L = []
    L.append(f"**{a} {exp} {strikes} {struct} · ×{r['qty']:g} | {r['side']} | "
             f"{verb} {fillabs:g} | {r['offset']['txt']} vs mark**")
    sp = f"{r['spot']:,.0f}" if r.get("spot") else "n/a"
    L.append("")
    L.append(f"Spot {sp} · {struct} · {'signs verified' if r['reliable_signs'] else 'net greeks: confirm signs from legs below'} · drfq/{r['venue']}")
    L.append("")
    L.append("```yaml")
    # [Greeks]
    ng = r["net_greeks"]
    if ng and r["reliable_signs"]:
        L.append(f"[Greeks]   Δ {ng['delta']:+.2f} {a} · Vega {ng['vega']:+,.0f}/v · "
                 f"Γ {ng['gamma']:+.4f} · Θ {ng['theta']:+,.0f}/d")
    else:
        per = " · ".join(
            f"{_sk(l['strike'])}{l['cp']} Δ{(l['tkr'] or {}).get('delta')}"
            for l in legs if l["cp"] != "FUT" and l.get("tkr"))
        L.append(f"[Greeks]   ⚠ net: confirm signs — per-leg: {per}")
    # [Fair]
    ivs = " / ".join(f"{_sk(l['strike'])}{l['cp']} {(l['tkr'] or {}).get('iv')}v"
                     for l in legs if l["cp"] != "FUT" and l.get("tkr"))
    L.append(f"[Fair]     {r['offset']['txt']} vs mark · {ivs}")
    # [History]
    d30 = sum((l["trades"] or {}).get("30d", (0, 0, 0))[1] for l in legs if l.get("trades"))
    L.append(f"[History]  {r['recurrence_blocks']} same-structure block(s) on Paradigm 30d · "
             f"Deribit leg blocks 30d: {d30}")
    # [Live]
    live = " · ".join(f"{_sk(l['strike'])}{l['cp']} {(l['tkr'] or {}).get('bid')}/{(l['tkr'] or {}).get('ask')}"
                      for l in legs if l["cp"] != "FUT" and l.get("tkr"))
    L.append(f"[Live]     {live}")
    L.append("```")
    if r["warnings"]:
        L.append(f"<!-- warnings: {'; '.join(r['warnings'])} -->")
    return "\n".join(L)


if __name__ == "__main__":
    main()
