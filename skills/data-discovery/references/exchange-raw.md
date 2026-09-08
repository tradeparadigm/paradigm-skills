# Exchange venue files

Use these files when a question needs exchange market data. They are the
per-period source data, not an LLM-shaped read surface: event rows and
existing per-instrument aggregates are both available.

## Read policy

- Do not read `s3://dt-exchange-venue-data/hot/`.
- Prefer `raw/` when venue-native fields matter. `normalized/` provides common
  column names, with both `rows` and `agg` files; normalization of names does
  not establish comparable units across venues.
- Read only the requested venues, data types, currencies, dates, and levels.
  Do not list or scan the whole bucket to discover data for each request.
- Use `rows` files for event-level questions. `rows` exist at `1m` and `5m`;
  `1h` contains aggregates only. Existing `1m`/`5m` aggregates can avoid scanning
  every ticker update when the question only needs a per-period observation.
  Choose one level for each interval: overlapping `1m`, `5m`, and `1h` files
  represent the same source events and must not be added together.
- Check freshness from the maximum `timestamp` in the selected data. S3 object
  modification time is not evidence that the records are current.
- Raw schemas and units differ by venue. Never union raw venues and then sum a
  native numeric field without normalising it or keeping the result grouped by
  venue.

## Layout

All paths are under `s3://dt-exchange-venue-data/`:

```text
<source>/exchange=<venue>/data_type=<type>/currency=<currency>/
  level=<1m|5m|1h>/year=YYYY/month=MM/day=DD/hour=HH/
  [minute=MM|start_minute=MM]/
  <source>__<venue>__<type>__<currency>__<level>__<rows|agg>__<time>.parquet
```

`source` is `raw` or `normalized`. Only `exchange` is stored as a physical
column in every file; `currency`, `data_type`, and `level` come from the path.
Read with `hive_partitioning=true` when those virtual columns are useful, and
use `union_by_name=true` only across files that intentionally have compatible
schemas.

Instrument metadata is append-only at:

```text
s3://dt-exchange-venue-data/meta/instruments/
  exchange=<venue>/currency=<currency>/instruments__*.parquet
```

For current questions, take the newest `captured_at` row per
`(exchange, currency, symbol)`. It carries
`instType`, `contract_size`, `contract_ccy`, `base_ccy`, `quote_ccy`, `iv_unit`,
`oi_unit`, `price_unit`, and `tick_size`.

For historical conversion, require metadata applicable at the event time:
use an at-or-before snapshot and check intervening changes, or establish that
the instrument's relevant attributes were unchanged. Do not apply today's
contract size or units to older events merely because it is the latest row.
Metadata snapshots have 30-day current-object retention while raw history can
extend further. The default skill does not support harmonized historical
analysis beyond 30 days; report older results in native units by venue unless
an independently verified historical metadata source establishes the conversion.
Even inside 30 days, retention is not a coverage guarantee: absent or ambiguous
applicable metadata means an explicit conversion gap, never a multiplier of 1.

## Inputs behind the hot files

Use the layout above with these selectors; the hot names below identify the
capability being replaced, not an allowed read path. All exchange inputs also
need `meta/instruments/` when converting venue-native units.

| Former output | Direct inputs | Agent responsibility |
|---|---|---|
| `hot__market_signals_1m` | Normalized `option_summary`, `option_trade`, and Deribit `perp_trade` aggregates at `1m`; raw Deribit `dvol` rows; normalized `perp_summary` rows; raw venue block-trade rows | Compute ATM IV, volume, DVOL, funding and block measures; report coverage separately per stream. The old spot signal uses a Deribit perpetual-price proxy, not the index price. |
| `hot__vol_surface` | Normalized `option_summary` aggregates at `5m`, or summary rows for exact event-time snapshots | Select per-symbol observations, harmonize IV/OI, then derive ATM, skew, term structure or max pain as needed. Do not sum repeated OI snapshots. |
| `hot__recap_aggregates_5m_24h` and all seven `hot__recap_<window>` presets | Normalized `option_trade` rows/aggregates, Deribit `perp_trade` aggregates, raw Deribit `dvol` rows, Bullish `perp_trade` and `spot_trade` rows, plus normalized `option_summary` aggregates for underlying prices | Filter the requested event window; compute volume, flow and real block groups with period-appropriate prices. No new consolidated recap dataset is required. |
| `hot__paradigm_trade_tape_30d` | Daily `paradigm_trade_tape/year=YYYY/month=MM/day=DD/paradigm_trade_tape__YYYYMMDD.parquet`; see [the execution contract](datasets.md#current-partitioned-executions). | Keep every matching leg and its real IDs; check per-object publication metadata. Deployment and Dime-role verification are required before cutover. |

The market-signal producer compares against its previous successfully published
snapshot. A new event-time delta computed from source partitions is useful, but
is not necessarily identical after a missed publication; label the actual times.
Likewise, empty trade partitions alone do not prove a healthy quiet market:
check a continuous companion feed and distinguish missing access from no events.

The Paradigm execution producer reads private, append-versioned Unified Markets
Airbyte streams (DRFQ/GRFQ legs, blocks, RFQs, orders and instrument metadata).
Do not point agents at that entire private namespace. The required replacement
is a scoped, current execution dataset retaining RFQ, trade, block and venue-block
IDs, execution time, price, mark, side, quantity and instrument dimensions.
Its notional USD measure is not option premium turnover.

Before declaring migration complete, verify bounded reads of every required
prefix with the **Dime runtime identity**, including metadata and current
execution partitions. Source-bucket existence or a replication configuration
does not prove that the consumer can read the destination objects.

## Available feeds

| Venue | Raw data types | Currencies | Important coverage |
|---|---|---|---|
| `deribit` | `option_trade`, `perp_trade`, `option_summary`, `dvol`, `perp_summary` | `btc`, `eth` | Option trades and tickers, native block/RFQ ids, DVOL, perp spot/funding |
| `deribit-usdc` | `option_trade`, `option_summary` | venue-configured assets | USDC-linear options, including supported alts |
| `okex-options` | `option_trade`, `option_summary`, `perp_summary` | `btc`, `eth` | Screen and block option trades, option chain, perp funding/OI |
| `bybit-options` | `option_trade`, `option_summary`, `perp_summary` | `btc`, `eth` | Option trades with a block flag, option chain, perp funding/OI |
| `bullish` | `option_trade`, `option_summary`, `perp_trade`, `spot_trade` | venue-configured assets | Options plus spot/perp trades and OTC identifiers |

The venue id describes the producing connection. For example,
`okex-options/perp_summary` is valid because the options connection also emits
the perp funding snapshot. Route on both `exchange` and `data_type`.

## Raw fields by question

### Option trades and blocks

Common concepts are `timestamp`, `localTimestamp`, `symbol`, `amount`, `price`,
side/direction, trade id, trade IV where published, underlying/index price, and
block identity where the venue supplies it.

| Venue | Side | IV / mark | Block identity |
|---|---|---|---|
| Deribit | `direction` | `iv`, `mark_price`, `index_price` | `block_trade_id`, `block_rfq_id`, `block_trade_leg_count`, `combo_id`, `combo_trade_id` |
| OKX | `side` | `fillVol`, `markPx`, `idxPx`, `fwdPx` | `block_trade_id` |
| Bybit | `side` | `iv`, `markIv`, `markPrice`, `indexPrice` | `is_block_trade` only; there is no group id |
| Bullish | `side` | not on trade rows | `otc_trade_id`, `otc_match_id`, `client_otc_trade_id` |

Rows sharing a real venue block id are one block. Do not invent grouping for
Bybit rows or join a venue block id to a Paradigm RFQ id unless the values
actually match.

### Option summaries

These are ticker/chain snapshots, often many observations per instrument per
minute. Choose the latest row at or before the time needed rather than summing
snapshots.

- Deribit: `mark_iv`, `bid_iv`, `ask_iv`, `mark_price`, bid/ask, `delta`,
  `gamma`, `theta`, `vega`, `rho`, `open_interest`, `underlying_price`.
- OKX: `markVol`, `bidVol`, `askVol`, `markPx`, bid/ask, cash greeks and
  Black-Scholes `deltaBS`/`gammaBS`/`thetaBS`/`vegaBS`, contract/coin/USD OI,
  `idxPx`, `fwdPx`, and `volLv`.
- Bybit: `markPriceIv`, `bidIv`, `askIv`, `markPrice`, bid/ask, greeks,
  `openInterest`, `underlyingPrice`, and 24-hour volume/turnover fields.
- Bullish: `impliedVolatility`, `markPrice`, `bestBid`, `bestAsk`, greeks,
  coin/USD OI, `underlyingPrice`, and screen/OTC volume fields.

### DVOL, funding, spot, and perps

- Deribit `dvol`: `asset`, `index_name`, `volatility`, `timestamp`.
- Deribit `perp_summary`: `current_funding`, `funding_8h`, `index_price`,
  `mark_price`, `open_interest`.
- OKX `perp_summary`: `fundingRate`, `nextFundingRate`, funding times,
  `idxPx`, `markPx`, and OI in contracts/coin/USD.
- Bybit `perp_summary`: `fundingRate`, `fundingIntervalHour`, `indexPrice`,
  `markPrice`, and OI in coin/USD.
- Bullish `spot_trade` / `perp_trade`: event-level `price`, `amount`, `side`,
  timestamp, and OTC ids when present.

## Units

Use instrument metadata rather than hardcoded multipliers when it is available.

| Venue | IV | Option amount / OI | Option premium price |
|---|---|---|---|
| Deribit | vol points (`62.0`) | coin | coin |
| Deribit USDC | vol points | coin | USD quote |
| OKX | decimal (`0.62`) | contracts; multiply by `contract_size` for coin | coin |
| Bybit | decimal | coin | USD quote |
| Bullish | decimal | coin | USD quote |

For comparable IV, multiply decimal IV by 100. For option volume, convert
contracts to coin only when `oi_unit='contracts'`. For premium turnover:

```text
coin-quoted: amount_coin * price * index_price
USD-quoted:  amount_coin * price
```

If required metadata or index price is absent, keep the value venue-native and
label its unit; do not default a multiplier to 1.

## Query pattern

Construct explicit date/hour globs for the requested interval. This example
reads Deribit BTC option-trade rows for one UTC hour:

```sql
INSTALL httpfs; LOAD httpfs;
INSTALL aws;    LOAD aws;
CREATE OR REPLACE SECRET s3_irsa (
  TYPE S3, PROVIDER CREDENTIAL_CHAIN, REGION 'ap-northeast-1'
);

SELECT *
FROM read_parquet(
  's3://dt-exchange-venue-data/raw/exchange=deribit/data_type=option_trade/currency=btc/level=1m/year=2026/month=08/day=30/hour=10/**/*__rows__*.parquet',
  hive_partitioning=true,
  union_by_name=true
)
WHERE CAST(timestamp AS TIMESTAMP) >= TIMESTAMP '2026-08-30 10:00:00'
  AND CAST(timestamp AS TIMESTAMP) <  TIMESTAMP '2026-08-30 11:00:00';
```

For a latest snapshot, select a narrow recent partition and use
`row_number() over (partition by symbol order by timestamp desc)=1`. For a
window spanning several days, pass DuckDB an explicit list of daily/hourly
globs rather than widening to the bucket root.

## Paradigm and Paradex source tapes

These non-hot files are also available:

- `s3://dt-paradigm-data/paradigm_data/paradigm_rfq_tape_slim.csv.gz`:
  RFQ requests, including `RFQ_ID`, product, description, quantity, quote
  currency, quote count, block count, status, and lifespan. It does not carry
  execution price, mark, side, trade id, or block id.
- `s3://dt-paradigm-data/paradigm_data/paradigm_trade_tape_slim.csv.gz`:
  executed RFQ trades with price, mark, side, and block ids, but its producer
  stopped and the public object is frozen at 2026-08-10. It is historical
  evidence only, never a current source.
- `s3://dt-paradex-data/paradex_data/paradex_trade_tape.csv.gz` and sibling
  Parquet parts: Paradex perp trades; always exclude `IS_TRADEBUST=true`.

There is no guaranteed cross-venue key from a Paradigm `RFQ_ID` to every raw
exchange trade. Deribit publishes `block_rfq_id`; try an exact/suffix match only
when the values support it. Otherwise use time, product, structure, size, and
real block ids as evidence and state when an execution cannot be resolved.
