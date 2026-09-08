# Available market data

All buckets are in `ap-northeast-1` and use the same DuckDB IRSA credential
chain described in `s3-access.md`.

## Exchange venue landing data

- **Bucket:** `s3://dt-exchange-venue-data`
- **Use:** `raw/`, `normalized/` rows/per-period aggregates, and
  `meta/instruments/`
- **Do not use:** `hot/` or any `hot__*` object
- **Coverage:** near-real-time, partitioned by venue, data type, currency,
  level, and event-time date/hour/minute

Read [exchange-raw.md](exchange-raw.md) for the layout, venue/data-type matrix,
raw field names, units, metadata, block identifiers, freshness rules, and
bounded DuckDB query pattern.

## Paradigm tapes

### Current partitioned executions

Deployment prerequisite: publish these objects and verify reads under Dime's
runtime identity before switching consumers. A missing key is not an empty day.

```text
s3://dt-exchange-venue-data/paradigm_trade_tape/
  year=YYYY/month=MM/day=DD/paradigm_trade_tape__YYYYMMDD.parquet
```

One row per executed leg, unique `trade_id`; completed DRFQ/GRFQ blocks only.
The same producer refreshes all 31 UTC-day objects covering the trailing 30 days
every 15 minutes, including typed empty days and later venue-block ID updates.
The oldest day is complete; the current day ends at the build time. Read exact
daily keys and filter `start_ms <= traded_at < end_ms`; never scan the prefix
unbounded or infer a historical retention guarantee from old keys remaining.

| Columns | Meaning |
|---|---|
| `traded_at`, `traded_at_iso` | Execution time, epoch milliseconds and UTC ISO text |
| `rfq_id`, `trade_id`, `block_trade_id` | Namespaced Paradigm identity (`DRFQv2-` or `GRFQ-`); keep all legs and group on block ID |
| `venue_block_trade_id` | Nullable venue-native ID for corroboration/deduplication, not a Paradigm RFQ ID |
| `venue`, `asset`, `instrument_kind`, `option_kind`, `instrument_name`, `strike_price`, `expiry_date` | Routing and instrument dimensions; `asset` is the underlying, not premium currency; options-only filter: `instrument_kind == "OPTION"` (case-sensitive) |
| `quantity`, `trade_price`, `mark_price`, `taker_side` | Native leg size, fill price, execution-time reference mark, BUY/SELL; infer conversion only from instrument metadata |
| `notional_volume_usd` | Contract notional in USD, **not premium turnover**; nullable when index price is unavailable |
| `row_type`, `auction`, `trade_source`, `product`, `description` | Trade classification and display fields |
| `generated_at` | Publication time in epoch milliseconds, not source ingestion freshness |

Object metadata `build_window_start_ms`, `build_window_end_ms`, `generated_at_ms`
is present even on empty days. The build bounds describe the whole rebuild,
not the contents of a single object: intersect them with that key's UTC day.
A closed day is temporally covered when build start is at/before its midnight
and build end is at/after the next midnight. The publisher refuses closed-day
row-count shrink; a legitimate downward correction needs operator review.
The shared reader in `scripts/execution_tape.py`
requires each object to have been published within 20 minutes, returns its
build-window end, and fails on missing access, keys or duplicate IDs. Publication
is atomic per object, not across days; replication may expose mixed generations.
The upstream Airbyte sync is hourly: a fresh publication does **not** prove that
an execution from the last few minutes has landed. Report the coverage and gap.

The `/analyze` collector returns all matching legs in
`execution_candidates.paradigm_tape`; `/recap` returns the requested asset's
option legs in `evidence.paradigm_executions`. Both retain explicit source errors
alongside independently available market evidence; neither falls back to hot.

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
| `QUOTE_CURRENCY` | Source currency label; not sufficient to establish premium units |
| `NOTIONAL_VOLUME_USD` | RFQ notional |
| `NUMBER_OF_QUOTES` | Maker responses |
| `NUMBER_OF_BLOCK_TRADES` | Executions; zero means unfilled |
| `COMPLETED_STATUS` | Completion state |
| `LIFESPAN_SECONDS` | Time live |
| `RFQ_ID` | RFQ identifier |

This tape does not contain execution price, reference mark, taker side,
`TRADE_ID`, or `BLOCK_TRADE_ID`.

### Legacy executed CSV tape (frozen)

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
| `QUOTE_CURRENCY` | Underlying asset/routing currency, not necessarily premium denomination |
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
- Guaranteed execution coverage outside the partitioned tape's supported window
  or the frozen legacy CSV's observed coverage
- A guaranteed cross-venue mapping between every Paradigm RFQ and venue block

Do not turn an unavailable field into a default or simulated value. State the
gap and answer from the fields that are actually present.
