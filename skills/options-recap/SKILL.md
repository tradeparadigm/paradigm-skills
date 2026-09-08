---
name: paradigm-options-recap
description: >
  Build an options market recap for /recap or a user-specified asset/window
  from raw exchange venue files and source tapes. Use for full options-market
  recaps; focused flow, volatility or biggest-print questions stay in data-discovery. The model chooses
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

The collector's trade examples and surface nodes are samples, not the full
market. Read aggregate field coverage and missing partitions before claiming
totals or rankings: null `premium_turnover_usd` means incomplete valuation;
`known_premium_turnover_usd` is only the valued subset. Surface nodes are
nearest available absolute 25/50 delta per expiry/type, with actual deltas;
the same instrument can occupy both nodes. Never sum their OI or describe
them as a full chain. For OI/max-pain, read all instruments once per snapshot.
Opening observations are the first event within the requested opening 5m
bucket, not an exact as-of quote; latest observations use the explicitly
reported stable bucket. Missing opening evidence cannot establish a change.
Exclude expired instruments at the comparison anchor and report observed time.

1. **Do not use `s3://dt-exchange-venue-data/hot/` or any `hot__*` object.**
2. Run `bash scripts/run_recap.sh <ASSET> <WINDOW>` once. It reads bounded
   direct partitions and returns a `dime.recap.evidence.v1` JSON document; it
   does not render or choose the answer.
3. Read
   [the raw exchange catalog](../data-discovery/references/exchange-raw.md)
   before choosing sources. The model owns the query plan.
4. Bound reads to the requested window, asset, venues, data types, and
   partitions. Check `max(timestamp)` in each continuous source used.
5. Normalise IV, amount, premium turnover, and OI with event-time-applicable instrument
   metadata before combining venues. If a conversion cannot be proved, keep
   the result venue-local and label the native unit.
   Follow the catalog's 30-day metadata-history limit; raw availability alone
   does not establish that a historical conversion is supported.
6. A missing or unreadable source is not a quiet market. Name the missing
   section or field; do not estimate, simulate, or fill it from a stale object.
7. When the prompt supplies fixture or injected evidence, treat those values as
   authoritative. Do not add invented "live" observations or replace fixture
   opens/closes with plausible numbers.

## Choose the raw inputs

Start from the requested output and use the smallest direct data set that can
support it. Typical choices are:

- raw `option_trade` rows from Deribit, Deribit USDC, OKX, Bybit, and Bullish
  for volume, put/call activity, screen flow, IV at trade, and venue blocks;
- raw `option_summary` rows for current/window-open mark IV, bid/ask, greeks,
  OI, underlying price, skew, and term structure;
- existing normalized `option_summary` per-period aggregates when a period-end
  observation suffices; use rows for exact event-time selection and apply the
  same metadata-driven unit conversions;
- Deribit raw `dvol` for DVOL open/close/high/low;
- raw `perp_summary` or relevant raw spot/perp trades for spot and funding;
- `meta/instruments/` for contract size and IV/OI/premium units;
- the daily partitioned Paradigm execution tape for brokered option legs
  (`evidence.paradigm_executions`), the current RFQ tape for request activity,
  and the frozen non-hot
  executed tape only for historical trades at or before 2026-08-10;
- public venue APIs when they provide a clearer current observation than the
  latest raw partition.

The collector supplies a general evidence bundle: source-local aggregates,
largest trade observations, window-open/latest surface observations, DVOL,
perpetual snapshots, venue-native block rows, provenance, units, freshness,
and explicit gaps. Inspect that evidence and decide which facts answer the
question. The JSON is evidence, not a response template and not a command to
populate every field.

The model may make additional bounded reads from raw S3, normalized rows or
per-period aggregates, source tapes, or venue APIs when the returned evidence identifies a real
gap. It must not use a hot or pre-shaped recap file.
The source map in the raw exchange catalog covers all former hot-file inputs.
Check `partitioned_paradigm_executions` source status and publication coverage;
do not present exchange-only block flow as complete Paradigm execution coverage.

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
- Build surface deltas from a window-open raw snapshot and the latest raw
  snapshot; show `n/a` when either side is unavailable.
- Derive DVOL open/close from the first/last event-time observations in the
  requested window. Do not substitute a different point from the range.
  Raw S3 `dvol` rows contain point observations (`timestamp`, `volatility`);
  fixture/API candles instead contain `[timestamp_ms, open, high, low, close]`.
  For a supplied candle series, sort by timestamp and use the earliest
  candle's open and latest candle's close, not the earliest candle's close.
  Candle coverage may straddle the requested boundaries; label that coverage
  rather than claiming exact sub-candle endpoints.
- For a mixed-direction biggest block, state each proven leg side (for example,
  `buy put / sell call`); "two-way" alone does not establish the structure.
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
