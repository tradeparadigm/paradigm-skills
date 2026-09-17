---
name: paradigm-ohlcv
description: >
  OHLCV candles for one market over a requested window, invoked via /ohlcv.
  Parses "/ohlcv [asset] [venue] [interval] [window]" (e.g. "/ohlcv btc 1h 24h")
  and renders open/high/low/close/volume bars built from bounded raw exchange
  venue partitions — never from Dime hot files. Use when the user types /ohlcv
  or asks to chart a market, for candles, a candlestick chart, price history,
  "what has BTC done today", "show me ETH 15m", or how a perp or spot market
  has traded over a period. Covers perpetual and spot markets only. Options
  flow, block structure and the vol surface belong to options-recap; dataset
  inventory, schema questions and ad-hoc historical queries belong to
  data-discovery. Periods with no data are reported as gaps, never filled.
metadata:
  author: tradeparadigm
  version: "0.1"
---

# OHLCV Candles

## Command

`/ohlcv [asset] [venue] [interval] [window]` is order-independent. Defaults are
BTC, the venue named below, `1h` bars and a `24h` window.

Two `Nm`/`Nh`/`Nd` tokens are read as interval then window, in that order. One
such token is the window, and the interval is chosen from its width. A bare
alphabetic token is the venue when it names a known venue, otherwise the asset.

State the interval, window and venue actually queried. Never widen a window,
never silently substitute a different interval, and never quietly cap either.

## Live execution

Run one command from this skill's directory:

```bash
bash scripts/run_ohlcv.sh BTC deribit 1h 24h
```

The script reads non-hot partitions, builds the bars, and prints the finished
table. Relay stdout verbatim as the entire answer, including the coverage and
gap lines. Do not recalculate, reformat, or make additional reads to fill
fields the script left out. If it fails, report the error and stop.

## Hard rules

1. **Do not use `s3://dt-exchange-venue-data/hot/` or any `hot__*` object.**
2. Run `bash scripts/run_ohlcv.sh <ASSET> <VENUE> <INTERVAL> <WINDOW>` once and
   relay its output. The script owns the bar construction.
3. A period with no trades and a period whose partition could not be read are
   different facts. Neither becomes a zero-volume bar: both are reported as
   gaps, with the reason. An unreadable source is not a quiet market.
4. Bars are venue-local. Never combine trades across venues into one series —
   sizes and price conventions differ, and a merged candle is not a real one.
   Name the venue in the output.
5. Bound reads to the requested asset, venue, interval and window. Check
   `max(timestamp)` in the selected partitions before treating the last bar as
   current; a partially written current period is labelled, not presented as
   closed.
6. Volume is in the venue's own base units unless instrument metadata proves a
   conversion. If it cannot be proved, keep it native and say so.

## Sources

Read [the raw exchange catalog](../data-discovery/references/exchange-raw.md)
for the partition layout, venue matrix, field names and units, and
[s3-access.md](../data-discovery/references/s3-access.md) for the DuckDB
credential preamble and the pinned regional endpoint.

Bars come from trade rows — `perp_trade` and `spot_trade` under
`raw/`/`normalized/` — bucketed by event time. Where a per-period aggregate
already carries open/high/low/close for the requested level, read it instead of
rebuilding from rows, and resample only downward.

## Output

Follow [references/output-format.md](references/output-format.md). The default
rendering is a mono table; when the client has advertised a chart component,
the script emits that component's spec instead. Both carry the same bars, the
same summary and the same gap lines.

Work silently while reading. The final response is the script's output, with no
process narration.
