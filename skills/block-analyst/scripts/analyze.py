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
import re
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
    """30d prints/blocks/contracts by 24h/7d/30d for one instrument.
    The whole body is guarded: one malformed trade record must degrade THIS
    instrument to None (warned), never leak an exception into the caller."""
    start = now_ms - 30 * 86400_000
    try:
        r = _get("get_last_trades_by_instrument",
                 {"instrument_name": sym, "count": 1000, "start_timestamp": start,
                  "end_timestamp": now_ms, "sorting": "desc"})
        t = r.get("trades") or []
        if not t:
            return sym, {"24h": (0, 0, 0.0), "7d": (0, 0, 0.0), "30d": (0, 0, 0.0)}
        latest = max(x.get("timestamp") or 0 for x in t)

        def bucket(days):
            c = latest - days * 86400_000
            w = [x for x in t if (x.get("timestamp") or 0) >= c]
            b = [x for x in w if x.get("block_trade_id")]
            return len(w), len(b), round(sum(float(x.get("amount") or 0) for x in w), 1)
        return sym, {"24h": bucket(1), "7d": bucket(7), "30d": bucket(30)}
    except Exception as e:  # noqa: BLE001
        warn(f"trades {sym}: {e}")
        return sym, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv-dir", default="/tmp/analyze")
    ap.add_argument("--now-ms", type=int)
    ap.add_argument("--render", action="store_true")
    args = ap.parse_args()
    try:
        _run(args)
    except Exception as e:  # noqa: BLE001 — never a traceback; degrade to raw rows
        try:
            rows = _read_csv(os.path.join(args.csv_dir, "fill.csv"))
        except Exception:  # noqa: BLE001 — the fallback itself must not raise
            rows = []
        if not rows:
            print("RFQ not resolved / analysis error — no data available.")
            return
        print(f"⚠ analysis hit an error ({type(e).__name__}) — the resolved tape rows below "
              f"are correct; build the block from them (fetch each leg on Deribit):")
        print("```yaml")
        for r in rows:
            print(f"  {r.get('SIDE')} {r.get('QTY')} @ {r.get('PRICE')} (ref {r.get('REF_PRICE')}) "
                  f"· {r.get('DESCRIPTION')} · {r.get('PRODUCT')}")
        print("```")


def _run(args):
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
    # Two tape shapes: (a) one combined-DESCRIPTION block (ICondor/Cstm/single) →
    # parse fill[0]; (b) one row PER LEG, each a single-leg desc or a perp/future →
    # build legs from the rows, sign straight from each row's SIDE (most reliable).
    unmapped = False
    legs = ac.legs_from_rows(fill)
    if legs is not None:
        side = "Buyer" if ac.net_cash(fill) > 0 else "Seller"
        # per-leg option signs are exact; a perp leg needs delta sizing we don't do
        # here, so defer the net to the model (⚠) when a future/perp leg is present.
        reliable = not any(l["cp"] == "FUT" for l in legs)
        parsed = {"code": "combo"}
    else:
        parsed = ac.parse_description(desc)
        if parsed["classified"] and parsed["legs"]:
            legs, side, reliable = ac.apply_orientation(parsed, fill)
        else:
            # Structure name not mapped. Safe fallback ladder (correctness > speed):
            # 1) if the description still lists explicit legs (Type/date/strike), pull
            #    them → we can fetch correct per-leg data; model assigns the net.
            # 2) otherwise legs stay empty → emit the raw tape rows + strikes and let
            #    the model build the whole block. Never a confident empty/guessed block.
            legs = ac.extract_legs_generic(desc)
            side = "Buyer" if ac.net_cash(fill) > 0 else "Seller"
            # explicit per-leg signs are authoritative even under an unmapped name →
            # still net reliably; otherwise defer to the model.
            reliable = (bool(legs)
                        and all(l.get("sign") is not None for l in legs)
                        and not any(l["cp"] == "FUT" for l in legs))
            unmapped = True

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

    # Net package offset (SKILL Step 7, the ONE convention) — per structure unit, unit by quote.
    # struct_net weights each option leg by its QTY relative to the structure's base unit, so a
    # 1×2×1 fly's body counts twice (net = +wing − 2×body + wing); a plain per-row sum over-states
    # it. The offset compares |net_fill| vs |net_mark| — the displayed Paid/Recd magnitude — so a
    # positive result always means the fill was richer than mark (above), negative cheaper (below),
    # deterministically, regardless of debit/credit. Never a per-leg OFFSET_BPS. Single-leg reduces
    # to (PRICE − REF_PRICE) × 10000 (backward compatible).
    fill_net = ac.struct_net(fill, "PRICE")
    ref_net = ac.struct_net(fill, "REF_PRICE")
    off = ac.offset(abs(fill_net), abs(ref_net), quote) if ref_net else {"txt": "n/a"}

    # recurrence: HIST blocks clustered by BLOCK_TRADE_ID
    blocks = {}
    for r in hist:
        b = r.get("BLOCK_TRADE_ID")
        if b:
            blocks.setdefault(b, []).append(r)
    recurrence = len(blocks)

    # grfq (multi-maker) vs drfq (directed) — from the resolved RFQ_ID's routing
    # prefix (GRFQ- / DRFQv2-), the authoritative source per SKILL Step 0.
    rfq_kind = "grfq" if (fill[0].get("RFQ_ID") or "").upper().startswith("GRFQ") else "drfq"

    result = {
        "asset": asset, "venue": prod["venue"], "structure": parsed["code"],
        "rfq_kind": rfq_kind,
        "desc": desc, "side": side, "qty": qty, "reliable_signs": reliable,
        "unmapped": unmapped, "spot": spot, "quote": quote,
        "fill_net": round(fill_net, 6), "ref_net": round(ref_net, 6), "offset": off,
        "legs": [{"cp": l["cp"], "strike": l["strike"], "ratio": l["ratio"],
                  "sign": l["sign"], "expiry": l.get("expiry_c"), "sym": l.get("_sym"),
                  "tkr": tickers.get(l.get("_sym")), "trades": buckets.get(l.get("_sym"))}
                 for l in legs],
        # raw tape rows — the authoritative ground truth for the model to build from
        # in the unmapped/⚠ cases (always correct straight from the resolved block).
        "fill_rows": [{"desc": r.get("DESCRIPTION"), "side": r.get("SIDE"),
                       "qty": ac._f(r.get("QTY")), "price": ac._f(r.get("PRICE")),
                       "ref": ac._f(r.get("REF_PRICE")), "product": r.get("PRODUCT")}
                      for r in fill],
        "net_greeks": ng, "recurrence_blocks": recurrence, "warnings": WARN,
    }

    if args.render:
        print(render(result))
    else:
        print(json.dumps(result, default=str))


def _sk(strike):
    k = int(round(strike))
    return f"{k//1000}k" if k >= 1000 and k % 1000 == 0 else str(k)


def _exp_short(ec):
    """Compact expiry '31JUL26' → '31Jul' (drop year) for terse leg tags."""
    m = re.match(r"(\d+)([A-Za-z]{3})", ec or "")
    return f"{m.group(1)}{m.group(2).title()}" if m else (ec or "")


def _leg_lbl(l, multi_exp):
    """Per-leg label '60500C'; append '·3Jul' only when the structure spans >1 expiry
    (calendars/diagonals) so same-strike legs are distinguishable — else stays clean."""
    base = f"{_sk(l['strike'])}{l['cp']}"
    return f"{base}·{_exp_short(l.get('expiry'))}" if multi_exp and l.get("expiry") else base


def _struct_name(code, legs):
    """Human structure name; condor/fly reflect leg composition (a 4-call block is a
    Call Condor, not an Iron Condor). Perp/future leg noted."""
    opt = [l for l in legs if l["cp"] != "FUT"]
    perp = " + perp" if any(l["cp"] == "FUT" for l in legs) else ""
    allc = bool(opt) and all(l["cp"] == "C" for l in opt)
    allp = bool(opt) and all(l["cp"] == "P" for l in opt)
    if code == "CO":
        base = "Call Condor" if allc else "Put Condor" if allp else "Iron Condor"
    elif code == "BF":
        base = "Call Fly" if allc else "Put Fly" if allp else "Iron Fly"
    elif code == "combo":
        base = ("Risk Reversal" if (any(l["cp"] == "C" for l in opt) and any(l["cp"] == "P" for l in opt)
                                    and len(opt) == 2) else "Combo")
    else:
        base = {"CL": "Call", "PL": "Put", "ST": "Straddle", "SN": "Strangle",
                "CS": "Call Spread", "PS": "Put Spread",
                "RR": "Risk Reversal", "CA": "Calendar", "CM": "Custom"}.get(code, code)
    return base + perp


def _offset_txt(off) -> str:
    """'-6 bps below mark' — the net package offset rendered with its direction word.
    Direction from the sign of (|net_fill| − |net_mark|): above = fill richer than mark,
    below = cheaper, at = equal. Neutral token — above/below is a fact, never an edge/against
    judgement (SKILL Step 7 forbids moralizing)."""
    t = off.get("txt", "n/a")
    if t == "n/a":
        return "n/a vs mark"
    s = off.get("sign", 0)
    return f"{t} {'above' if s > 0 else 'below' if s < 0 else 'at'} mark"


def render(r) -> str:
    a = r["asset"]
    legs = r["legs"]
    verb = "Paid" if r["fill_net"] >= 0 else "Recd"
    fillabs = abs(r["fill_net"])

    # SAFE FALLBACK — structure not mapped AND no legs could be extracted. Do NOT
    # emit a confident empty block: print the authoritative tape rows + recurrence and
    # tell the model to build the analysis from them (correct data, slower).
    if not legs:
        L = [f"⚠ UNMAPPED STRUCTURE — build the block from the raw tape rows below "
             f"(resolved & correct); infer legs from these rows' DESCRIPTION (not the "
             f"user's inline text), fetch each leg on Deribit, net the greeks.",
             "",
             f"**{a} · {r['desc'].strip()} · ×{r['qty']:g} | {r['side']} | "
             f"{verb} {fillabs:g} | {_offset_txt(r['offset'])}** · {r['rfq_kind']}/{r['venue']}",
             "",
             "```yaml",
             f"[Tape]  {len(r['fill_rows'])} fill row(s):"]
        for row in r["fill_rows"]:
            L.append(f"        {row['side']} {row['qty']:g} @ {row['price']} "
                     f"(ref {row['ref']}) · {row['desc']}")
        L.append(f"[Recur] {r['recurrence_blocks']} same-structure block(s) on Paradigm 30d")
        sp = f"{r['spot']:,.0f}" if r.get("spot") else "n/a"
        L.append(f"[Spot]  {sp}")
        L.append("```")
        if r["warnings"]:
            L.append(f"<!-- warnings: {'; '.join(r['warnings'])} -->")
        return "\n".join(L)

    exp = legs[0]["expiry"] if legs else "?"
    strikes = "/".join(_sk(l["strike"]) for l in legs if l["cp"] != "FUT")
    struct = _struct_name(r["structure"], legs)
    L = []
    L.append(f"**{a} {exp} {strikes} {struct} · ×{r['qty']:g} | {r['side']} | "
             f"{verb} {fillabs:g} | {_offset_txt(r['offset'])}**")
    sp = f"{r['spot']:,.0f}" if r.get("spot") else "n/a"
    L.append("")
    note = ("⚠ unmapped structure — verify legs & net signs from the data below"
            if r.get("unmapped") else
            ("signs verified" if r["reliable_signs"] else "net greeks: confirm signs from legs below"))
    L.append(f"Spot {sp} · {struct} · {note} · {r['rfq_kind']}/{r['venue']}")
    L.append("")
    # tag legs with expiry only when the structure spans >1 expiry (calendars/diagonals)
    multi_exp = len({l.get("expiry") for l in legs if l["cp"] != "FUT" and l.get("expiry")}) > 1
    L.append("```yaml")
    # [Greeks]
    ng = r["net_greeks"]
    if ng and r["reliable_signs"]:
        L.append(f"[Greeks]   Δ {ng['delta']:+.2f} {a} · Vega {ng['vega']:+,.0f}/v · "
                 f"Γ {ng['gamma']:+.4f} · Θ {ng['theta']:+,.0f}/d")
    else:
        per = " · ".join(
            f"{_leg_lbl(l, multi_exp)} Δ{(l['tkr'] or {}).get('delta')}"
            for l in legs if l["cp"] != "FUT" and l.get("tkr"))
        L.append(f"[Greeks]   ⚠ net: confirm signs — per-leg: {per}")
    # [Fair]
    ivs = " / ".join(f"{_leg_lbl(l, multi_exp)} {(l['tkr'] or {}).get('iv')}v"
                     for l in legs if l["cp"] != "FUT" and l.get("tkr"))
    L.append(f"[Fair]     {_offset_txt(r['offset'])} · {ivs}")
    # [History]
    d30 = sum((l["trades"] or {}).get("30d", (0, 0, 0))[1] for l in legs if l.get("trades"))
    L.append(f"[History]  {r['recurrence_blocks']} same-structure block(s) on Paradigm 30d · "
             f"Deribit leg blocks 30d: {d30}")
    # [Live]
    live = " · ".join(f"{_leg_lbl(l, multi_exp)} {(l['tkr'] or {}).get('bid')}/{(l['tkr'] or {}).get('ask')}"
                      for l in legs if l["cp"] != "FUT" and l.get("tkr"))
    L.append(f"[Live]     {live}")
    L.append("```")
    if r["warnings"]:
        L.append(f"<!-- warnings: {'; '.join(r['warnings'])} -->")
    return "\n".join(L)


if __name__ == "__main__":
    main()
