# Available market data

All buckets are in `ap-northeast-1` and use the same DuckDB IRSA credential
chain described in `s3-access.md`.

## Exchange venue landing data

- **Bucket:** `s3://dt-exchange-venue-data`
- **Use:** `raw/`, optional per-message `normalized/`, and
  `meta/instruments/`
- **Do not use:** `hot/` or any `hot__*` object
- **Coverage:** near-real-time, partitioned by venue, data type, currency,
  level, and event-time date/hour/minute

Read [exchange-raw.md](exchange-raw.md) for the layout, venue/data-type matrix,
raw field names, units, metadata, block identifiers, freshness rules, and
bounded DuckDB query pattern.

## Paradigm tapes

### RFQ activity tape

- **Path:** `s3://dt-paradigm-data/paradigm_data/paradigm_rfq_tape_slim.csv.gz`
- **Grain:** one RFQ request; includes filled and unfilled requests
- **Freshness:** growing source; verify `max(DATE)` from content

| Column | Meaning |
|---|---|
| `DATE`, `TIME` | RFQ creation time in UTC |
| `AUCTION` | `RFQ` or `OB` |
| `PRODUCT` | Asset, instrument kind, and venue suffix |
| `DESCRIPTION` | Strategy description |
| `QTY` | Requested quantity |
| `QUOTE_CURRENCY` | Premium currency |
| `NOTIONAL_VOLUME_USD` | RFQ notional |
| `NUMBER_OF_QUOTES` | Maker responses |
| `NUMBER_OF_BLOCK_TRADES` | Executions; zero means unfilled |
| `COMPLETED_STATUS` | Completion state |
| `LIFESPAN_SECONDS` | Time live |
| `RFQ_ID` | RFQ identifier |

This tape does not contain execution price, reference mark, taker side,
`TRADE_ID`, or `BLOCK_TRADE_ID`.

### Executed trade tape

- **Path:** `s3://dt-paradigm-data/paradigm_data/paradigm_trade_tape_slim.csv.gz`
- **Grain:** executed RFQ trade leg
- **Freshness:** **frozen at 2026-08-10**; historical evidence only

| Column | Meaning |
|---|---|
| `DATE`, `TIME` | Execution time in UTC |
| `AUCTION` | `RFQ` or `OB` |
| `PRODUCT` | Asset, instrument kind, and venue suffix |
| `DESCRIPTION` | Instrument or strategy description |
| `QTY` | Quantity |
| `PRICE` | Execution price |
| `REF_PRICE` | Reference mark at execution |
| `SIDE` | Taker side |
| `QUOTE_CURRENCY` | Premium currency |
| `NOTIONAL_VOLUME_USD` | USD notional |
| `RFQ_ID` | Join to the RFQ tape |
| `TRADE_ID` | Trade identifier |
| `BLOCK_TRADE_ID` | Block group identifier |

Common product suffixes are `DBT` (Deribit), `PRDX` (Paradex), and `BYB`
(Bybit), but treat the suffix vocabulary as open. Filter options with
`PRODUCT LIKE '%OPTION%'` only when the question is option-specific.

The RFQ tape and executed tape join on `RFQ_ID`, but the executed tape cannot
resolve current post-freeze fills. Raw Deribit option trades expose
`block_rfq_id`; other venue raw feeds may expose only a block id or flag. There
is no guaranteed universal join from Paradigm RFQ id to raw exchange trade.

## Paradex DEX trade tape

- **Paths:** `s3://dt-paradex-data/paradex_data/paradex_trade_tape.csv.gz`
  plus sibling Parquet parts
- **Grain:** one on-chain Paradex perpetual trade
- **Coverage:** starts 2024-12-26; verify the current end from content

| Column | Meaning |
|---|---|
| `IS_TRADEBUST` | Cancelled/busted trade flag |
| `MARKET` | Paradex market symbol |
| `PRICE` | Trade price in USD |
| `SIZE` | Contracts |
| `TAKER_SIDE` | `BUY` or `SELL` |
| `TRADE_AT` | Execution timestamp in UTC |

Always filter `WHERE NOT IS_TRADEBUST`. Compute simple trade notional as
`PRICE * SIZE`; the tape has no precomputed USD notional column.

## Coverage probes

Use record time, not object modification time:

```sql
SELECT min(DATE), max(DATE), count(*)
FROM read_csv_auto('s3://dt-paradigm-data/paradigm_data/paradigm_rfq_tape_slim.csv.gz');

SELECT min(TRADE_AT), max(TRADE_AT), count(*)
FROM read_csv_auto('s3://dt-paradex-data/paradex_data/paradex_trade_tape.csv.gz')
WHERE NOT IS_TRADEBUST;
```

For exchange landing data, select the narrow recent partition and inspect
`max(CAST(timestamp AS TIMESTAMP))`.

## Not available here

- Raw order-book depth or book deltas
- Live Paradex positions, balances, vaults, margin, or order placement
- A current non-hot Paradigm executed-trade tape after 2026-08-10
- A guaranteed cross-venue mapping between every Paradigm RFQ and venue block

Do not turn an unavailable field into a default or simulated value. State the
gap and answer from the fields that are actually present.
