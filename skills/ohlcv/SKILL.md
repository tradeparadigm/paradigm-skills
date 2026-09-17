---
name: paradigm-ohlcv
description: >
  OHLCV candles for one market over a requested window, invoked via /ohlcv.
  Parses "/ohlcv [asset] [venue] [interval] [window]" (e.g. "/ohlcv btc 1h 24h")
  and renders open/high/low/close/volume bars built from bounded raw exchange
  venue partitions — never from Dime hot files. Use when the user types /ohlcv
  or asks to chart a market, for candles, a candlestick chart, price history,
  "what has BTC done today", "show me ETH 15m", or how a perp or spot market
  has traded over a period. Covers perpetual futures only — spot is not wired.
  Options
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
BTC, `deribit`, a `24h` window and the `1h` bars that window derives.

Two `Nm`/`Nh`/`Nd` tokens resolve by length — the shorter is the interval, the
longer the window — so `1h 24h` and `24h 1h` mean the same thing. One
such token is the window, and the interval derives from its width: `1m` up to
4h, `1h` up to 3d, `1d` beyond. A bare alphabetic token is the venue when it
names one below, otherwise the asset. A repeated slot — two windows, two
intervals, two venues, two assets — is an error, not a silent choice.

Valid venues are the ones that publish perpetual trade rows: `deribit` (BTC and
ETH) and `bullish`. The other venues in the catalog carry option and summary
feeds only, so they cannot produce candles and are refused up front rather than
after an empty read.

Only `perp_trade` is read. Bullish also publishes `spot_trade`, but nothing
here selects it, so a spot market cannot be charted by this skill today.

State the interval, window and venue actually queried — a derived interval is
stated, not assumed. Never widen a window, never substitute a different
interval for one the user gave, and never quietly cap either.

## Live execution

Run one command from this skill's directory:

```bash
bash scripts/run_ohlcv.sh BTC deribit 1h 24h
```

Windows are bounded by what the read can actually serve, not by what the data
retains: a request spanning more days than the read bound allows is refused
before any listing, because one day-level glob per calendar day lists every
object in that day and a long window spends minutes enumerating keys before it
reads a byte. The refusal names the bound. Report it and let the user choose a
window; do not re-run at the limit and do not fall back to a coarser interval
to squeeze under it.

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

Bars come from normalized `perp_trade` rows, bucketed by event time.
`normalized/` rather than `raw/` because it gives the same column names across
venues; bars are still built one venue at a time, never unioned.

Every interval reads the `5m` rows level and buckets up from there. `level` is
file granularity, not sampling — the catalog is explicit that `1m`, `5m` and
`1h` files carry the same events and must not be combined — and rows exist only
at `1m` and `5m`, so `5m` is the coarsest level that can build a candle at all.
Reading it for every interval also keeps the listing cost flat in the interval.

Whether the `1h` aggregate files already carry open/high/low/close is not
established anywhere in the catalog, so nothing here reads them. If a probe
confirms that schema, a coarse-interval shortcut becomes available.

## Output

Follow [references/output-format.md](references/output-format.md). The default
rendering is a mono table.

A client that advertises a chart component can have its spec instead, by
passing the advertised id: `--component <id>` on the collector. The id always
comes from the caller — the skill never assumes one, and with no id it renders
the table. Nothing in this repository advertises a component today, so the
table is what every current invocation produces.

Work silently while reading. The final response is the script's output, with no
process narration.
