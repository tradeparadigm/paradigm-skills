# Exchange venue files

Use these files when a question needs exchange market data. They are the
per-message landing data, not an LLM-shaped read surface.

## Read policy

- Do not read `s3://dt-exchange-venue-data/hot/`.
- Prefer `raw/` when venue-native fields matter. Use `normalized/` only when a
  common cross-venue schema materially simplifies the question; it is still
  per-message data, not a hot aggregate.
- Read only the requested venues, data types, currencies, dates, and levels.
  Do not list or scan the whole bucket to discover data for each request.
- Use `rows` files for event-level questions. `rows` exist at `1m` and `5m`;
  `1h` contains aggregates only.
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

Take the newest `captured_at` row per `(exchange, currency, symbol)`. It carries
`instType`, `contract_size`, `contract_ccy`, `base_ccy`, `quote_ccy`, `iv_unit`,
`oi_unit`, `price_unit`, and `tick_size`.

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

## Normalized per-message fields

The `normalized/` layer harmonises the raw per-venue names. The fields the
Dime collectors select (and follow-up normalized queries should use):

- `option_trade`: `exchange`, `timestamp`, `symbol`, `side` (`buy`/`sell`),
  `amount`, `price`, `iv`, `index_price`, `turnover_usd`, `block_id`, `id`
- `option_summary`: `exchange`, `timestamp`, `symbol`, `expirationDate`,
  `strikePrice`, `optionType`, `markIV`, `bestBidIV`, `bestAskIV`,
  `markPrice`, `bestBidPrice`, `bestAskPrice`, `delta`, `gamma`, `vega`,
  `theta`, `openInterest`, `underlyingPrice`
- `perp_summary`: `exchange`, `timestamp`, `symbol`, `funding_rate`,
  `funding_interval_hours`, `index_price`, `mark_price`,
  `open_interest_coin`, `open_interest_usd`

These names differ from the raw per-venue names below (`mark_iv`, `markVol`,
`markPriceIv`, …) — do not mix the two vocabularies in one query. Before
combining normalized IV or OI across venues, verify the normalized units
against a small sample partition for each venue; do not assume the
normalization layer converted decimal-IV venues to vol points.

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
