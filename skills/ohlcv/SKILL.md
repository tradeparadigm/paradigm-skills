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
bash scripts/run_ohlcv.sh BTC deribit 1h 24h --component paradex-ohlcv_chart
```

`--component paradex-ohlcv_chart` is part of the normal command in Dime
Terminal, whose browser advertises that component on connect. It makes the
script print a chart the user can read instead of a table they have to parse.
Drop it only for a client that advertised no chart component.

Windows are bounded by what the read can serve. Two bounds, and a request over
either is refused before any listing: 2000 bars, and 7 calendar days.

The day bound is about MEMORY, not time. The window is fast — about 4s over a
day and 6s over a week against the real bucket — but the reader holds every
object's rows at once, and in the agent container that memory is shared with
the agent process itself. A 30-day window OOM-killed the container, which does
not merely fail the query: it takes the agent down and drops the session. Short
intervals hit the bar bound first, so 7d at 5m is refused as 2,016 bars.

The refusal names which bound was crossed. Report it and let the user choose;
do not re-run at the limit and do not coarsen the interval to squeeze under it.

The script reads non-hot partitions, builds the bars, and prints the finished
output. **Relay its stdout as the entire answer — the whole reply, nothing
else.** No preamble naming the parsed arguments, no caveats section after it,
no restating the coverage lines in prose: the script already prints them and
repeating them makes the answer twice as long as it needs to be.

With `--component`, stdout is a single JSON object. Print exactly that object
and stop. Wrapping it in a fence, summarising it, or describing the chart in
words all prevent the terminal from drawing it.

Do not recalculate, reformat, or make extra reads to fill fields the script
left out. If it fails, report the error and stop.

## Hard rules

1. **Do not use `s3://dt-exchange-venue-data/hot/` or any `hot__*` object.**
2. Run `bash scripts/run_ohlcv.sh <ASSET> <VENUE> <INTERVAL> <WINDOW>
   --component paradex-ohlcv_chart` once and relay its output. The script owns
   the bar construction. Keep `--component` in Dime Terminal: without it the
   user gets a table to read instead of the chart they asked for.
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

The `__agg__` file beside each `__rows__` file already carries the candle —
the raw exchange catalog lists its fields under "Aggregate fields" — and one
row per period instead of several hundred trades would cut this skill's memory
use sharply.

It is deliberately not used, and the catalog says why: an aggregate has already
collapsed whatever was null underneath it, so it cannot say whether it is
complete. A period whose rows lacked applicable metadata reads as a smaller
number rather than as a gap. This skill's whole contract is the opposite — a
trade with no reported size is counted and declared, never quietly absorbed
into a smaller volume. Reading aggregates would buy memory with the one
guarantee the output makes.

If that trade is ever worth taking, it has to be visible: the aggregate path
would have to say in a coverage line that its volume cannot be proven
complete.

## Output

Follow [references/output-format.md](references/output-format.md).

**Dime Terminal draws a chart, so `--component paradex-ohlcv_chart` belongs on
every invocation there.** Its browser advertises that component on connect.
Omitting the flag is not a neutral choice: it prints a table of numbers where
the user asked to see a chart.

The mono table is the fallback for a client that advertised no chart
component. The id always comes from the client's catalog and is never invented;
with no id, the table is what prints.

**In Dime Terminal**, the browser advertises `paradex-ohlcv_chart` on connect,
so use `--component paradex-ohlcv_chart` and relay the JSON it prints exactly
as printed. The terminal parses that object out of the reply and draws the
candles. Do not wrap it in a code fence, describe it, or add prose around it —
the spec must be the reply.

Work silently while reading. The final response is the script's output, with no
process narration.
