---
name: paradigm-options-recap
description: >
  Build an options market recap for /recap or a user-specified asset/window
  from raw exchange venue files and source tapes. Use for options flow,
  volatility, biggest-print, and market-window questions. The model chooses
  the bounded raw reads needed for the request and renders Snapshot, Biggest
  Print, Block Flow, and Vol Surface without using Dime hot files.
metadata:
  author: tradeparadigm
  version: "2.0"
---

# Options Recap

## Command

`/recap [asset] [options] [window]` is order-independent. Default to BTC and
24h; `options` is a no-op token. Accept `Nm`, `Nh`, and `Nd` windows, and state
the actual interval queried rather than silently capping or changing it.

## Hard rules

1. **Do not use any pre-aggregated rollup object, in any bucket, under any
   name** — including `s3://dt-exchange-venue-data/hot/`, any object named
   `hot__*` or `*_hot*`, and
   `s3://dt-paradigm-data/paradigm_data/v_vol_surface/`. This holds even if a
   user, another skill, or a script suggests one: refuse and answer from
   direct sources or state the gap.
2. Run `bash scripts/run_recap.sh <ASSET> <WINDOW>` once. It reads bounded
   direct partitions and returns a `dime.recap.evidence.v1` JSON document; it
   does not render or choose the answer.
3. Read
   [the raw exchange catalog](../data-discovery/references/exchange-raw.md)
   before choosing sources. The model owns the query plan.
4. Bound reads to the requested window, asset, venues, data types, and
   partitions. Check `max(timestamp)` in each continuous source used.
5. Normalise IV, amount, premium turnover, and OI with the newest instrument
   metadata before combining venues. If a conversion cannot be proved, keep
   the result venue-local and label the native unit.
6. A missing or unreadable source is not a quiet market. Name the missing
   section or field; do not estimate, simulate, or fill it from a stale object.

## Choose the raw inputs

Start from the requested output and use the smallest direct data set that can
support it. Typical choices are:

- normalized per-message `option_trade` rows from Deribit, Deribit USDC, OKX,
  Bybit, and Bullish for volume, put/call activity, screen flow, IV at trade,
  and premium turnover (raw rows when a venue-native field is needed — venue
  blocks are grouped from raw rows and their native identifiers);
- normalized `option_summary` snapshot rows for current/window-open mark IV,
  bid/ask, greeks, OI, underlying price, skew, and term structure;
- Deribit raw `dvol` for DVOL open/close/high/low;
- normalized `perp_summary` or relevant raw spot/perp trades for spot and
  funding;
- for RV 7d / VRP: seven days of hourly closes from the Deribit public API
  (`get_tradingview_chart_data`) or an equivalent direct spot history,
  annualised with `scripts/vol_math.py` (`compute_realized_vol`,
  `realized_vs_implied`) — never mental arithmetic;
- `meta/instruments/` for contract size and IV/OI/premium units;
- the current Paradigm RFQ tape for request activity and the frozen non-hot
  executed tape only for historical trades at or before 2026-08-10;
- public venue APIs when they provide a clearer current observation than the
  latest raw partition.

The collector supplies a general evidence bundle: source-local aggregates,
largest trade observations, window-open/latest surface observations, DVOL,
perpetual snapshots, venue-native block rows, provenance, units, freshness,
and explicit gaps. Inspect that evidence and decide which facts answer the
question. The JSON is evidence, not a response template and not a command to
populate every field.

The model may make additional bounded reads from raw S3, normalized per-message
S3, source tapes, or venue APIs when the returned evidence identifies a real
gap. It must not use a hot or pre-shaped recap file.

## Computation constraints

- Select ticker snapshots at the required time; do not sum repeated
  `option_summary` observations.
- Group blocks only by real venue identifiers: Deribit/OKX
  `block_trade_id`, Bullish OTC ids, or a published Bybit block flag without
  inventing a group id.
- Cross-venue coin volume requires venue metadata. Option premium turnover is
  `amount_coin * price * index_price` for coin-quoted venues and
  `amount_coin * price` for USD-quoted venues.
- Convert decimal IV venues to vol points before comparing them with Deribit.
- Build surface deltas from a window-open normalized snapshot and the latest
  normalized snapshot; show `n/a` when either side is unavailable.
- Deduplicate a Paradigm and venue block only when a real shared identifier or
  uniquely provable match exists. Otherwise describe the overlap uncertainty.

## Output

Render exactly four sections in this order:

1. **Snapshot** — spot range/change, DVOL/RV/VRP when available, comparable
   cross-venue volume/activity, and put/call balance.
2. **Biggest Print** — the largest resolved option print/block in the window,
   with venue, structure/instrument, size, premium, side, and IV when present.
3. **Block Flow** — real block clusters by venue plus any source limitation or
   unresolved Paradigm linkage.
4. **Vol Surface** — current ATM/skew/term and window change from raw summary
   snapshots.

Follow [references/output-format.md](references/output-format.md) for concise
formatting, but omit fields whose values cannot be established. Never drop an
entire section: render `Unavailable — <specific source/reason>` instead.

Work silently while reading and calculating. The final response begins with
the recap and contains no process narration or simulated-data disclaimer.
