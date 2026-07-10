# Available Market Data — S3 Catalog

Comprehensive map of market data accessible via DuckDB + S3. Covers the
exchange venue data pipeline (Deribit / OKX / Bybit / Bullish real-time
options + perps + spot, plus its LLM-shaped hot surface), Paradigm RFQ
flow, Bullish option chain snapshots, IBIT ETF options, and the on-chain
Paradex perp trade tape.

> **Date ranges below are point-in-time as of last verification:
> 2026-07-10** (checked against the source datalake). Coverage expands
> over time — for any "recent date" question, run the glob probe in
> `SKILL.md` Step 3 to confirm.

---

## S3 Buckets

Data is split across per-domain buckets (all read the same way — DuckDB
`httpfs` + IRSA/STS, see `s3-access.md`):

| Bucket | Holds | Region |
|---|---|---|
| `s3://dt-exchange-venue-data` | Exchange venue data — the Deribit/OKX/Bybit/Bullish pipeline: raw + normalized per-period substrate, instrument meta, the retained 5m bucket series, and the hot surface | ap-northeast-1 **(verify)** |
| `s3://dt-paradigm-data` | Paradigm-domain datasets under `paradigm_data/`: RFQ block-trade tape, RFQ activity tape, Bullish option chain snapshots, IBIT ETF option trades | ap-northeast-1 **(verify)** |
| `s3://dt-paradex-data` **(bucket TBC)** | On-chain Paradex perp trade tape | ap-northeast-1 **(verify)** |

- **`dt-exchange-venue-data`** is a replica of the source
  `s3://exchange-venue-data` (us-east-1), landing **at the same keys** —
  its root prefixes (`hot/`, `raw/`, `normalized/`, `meta/`,
  `market_aggregates_5m/`) mirror the source layout exactly.
- **`dt-paradigm-data`** replicates `paradigm_data/` from the source
  datalake `s3://paradigm-datalake-public-data-us-east-1/paradigm_data/`.
  Source production verified 2026-07-10: the two slim Paradigm tapes are
  **live (hourly)**; the Bullish and IBIT datasets are **static/stalled**
  — per-dataset status is noted on each entry below.
- Buckets/regions marked **(verify)** are working assumptions from the
  prior `terminal-dime-prod` (ap-northeast-1) setup; confirm the actual
  region per bucket before first use (set `s3_region` accordingly).

---

## Dataset 1 — Exchange Venue Data (Deribit / OKX / Bybit / Bullish)

The real-time exchange market-data pipeline. Every venue connects
**raw-direct to its own WebSocket**; each message lands **twice** — a
`normalized` record (one cross-venue schema) and a `raw` record
(venue-native superset). This is the primary source for options trades,
option chains (IV/greeks/OI), perp/spot trades, funding, and DVOL.

- **Bucket:** `s3://dt-exchange-venue-data`
- **Venues:** `deribit`, `okex-options`, `bybit-options`, `bullish`
  (bullish is perp + spot only — no options on this feed).
- **Coverage:** near-real-time, always-current. Per-period files land
  every minute (1m), rolled to 5m and 1h.

### 1a. Per-period substrate — `raw/` and `normalized/`

Hive-partitioned, one file per (venue, data_type, currency, level,
period). **Path:**

```text
s3://dt-exchange-venue-data/<source>/exchange=<venue>/data_type=<type>/currency=<ccy>/level=<level>/year=YYYY/month=MM/day=DD/hour=HH[/minute=MM|start_minute=MM]/<source>__<venue>__<type>__<ccy>__<level>__<kind>__<YYYYMMDDTHHMMSSZ>.parquet
```

Path/filename identifiers (the filename repeats every dimension — a file
read out of its directory still names its own contents):

| Segment | Values |
|---|---|
| `<source>` | `raw` (venue-native superset) · `normalized` (cross-venue LCD schema) |
| `exchange=` | `deribit` · `okex-options` · `bybit-options` · `bullish` |
| `data_type=` | `option_trade` · `option_summary` · `perp_trade` · `spot_trade` · `dvol` (raw deribit) · `perp_summary` (raw deribit) |
| `currency=` | `btc` · `eth` |
| `level=` | `1m` · `5m` · `1h` |
| `<kind>` | `rows` (un-aggregated) · `agg` (per-period aggregate). **normalized only.** `1h` is `agg`-only (no rows). |
| time | `1m` → `minute=MM`; `5m` → `start_minute=MM`; `1h` → `hour=HH` leaf |

Which data_types exist per venue: deribit → option_trade, option_summary,
perp_trade, dvol (raw), perp_summary (raw); okex/bybit → option_trade,
option_summary; bullish → perp_trade, spot_trade. `dvol` and
`perp_summary` are **raw-only** (no normalized form; deribit-only).

Normalized `option_summary` `agg` files carry the per-(venue,symbol)
period aggregate — IV/OI/greeks OHLC (`markIV_close`, `openInterest_close`,
`delta_close`, `underlyingPrice_close`, …). Normalized trade `agg` files
carry OHLCV + buy/sell splits + `block_id`. Only `exchange` is a real
column; `currency`/`data_type`/`level` are path-encoded, not columns.

**Glob probe** (confirm latest for a venue/type):

```sql
SELECT MAX(regexp_extract(file, '/(\d{4}/\d{2}/\d{2}/hour=\d{2})/', 1)) AS latest,
       COUNT(*) AS file_count
FROM glob('s3://dt-exchange-venue-data/normalized/exchange=deribit/data_type=option_summary/currency=btc/level=5m/**');
```

### 1b. Instrument metadata — `meta/instruments/`

- **Path:** `s3://dt-exchange-venue-data/meta/instruments/exchange=<venue>/currency=<ccy>/instruments__<venue>__<ccy>__<ts>.parquet`
- Per-startup snapshots of each venue's contract conventions
  (`contract_size`, `iv_unit`, `oi_unit`, `price_unit`). The lexically
  latest file is the newest. Used to harmonize units — rarely queried
  directly, but here for provenance.

### 1c. Retained 5m bucket series — `market_aggregates_5m/`

- **Path:** `s3://dt-exchange-venue-data/market_aggregates_5m/market_aggregates_5m__<ts>.parquet`
- The combinable 5m buckets the hot recaps sum (24h retained). Intermediate
  substrate — prefer the hot recaps (Dataset 2) for windowed reads; use
  this only for custom windows or per-strike tails beyond the recap cap.

---

## Dataset 2 — Hot Surface (Live Snapshot + Trailing Windows + Vol Surface)

LLM-shaped, pre-computed read surface over the exchange venue data —
the fastest way to answer "what's happening right now" / "last <window>"
without scanning the substrate. Three file kinds under
`s3://dt-exchange-venue-data/hot/`, all clobbered in place at stable keys.

**Cross-cutting contract (all three files):**

- **`instrument_kind`** ∈ {`option`, `perp`, `spot`, `index`} on **every
  row** — the explicit options-vs-not label; never infer. (`option` =
  atm_iv / options volume / options flow / option blocks / the vol
  surface; `perp` = funding + bullish perp OTC; `spot` = the Deribit
  perp-as-spot proxy + bullish spot OTC; `index` = DVOL.)
- Every notional column is **USD** (`notional_usd`,
  `block_*_notional_usd`); volume is **underlying coin**; IV is **vol
  points**; each row has a `unit` naming its primary metric + `at`
  (epoch ms, END of the covered period) + `at_iso` + `generated_at`.

### 2a. `hot__market_signals_1m` — Live Heartbeat

- **Path:** `s3://dt-exchange-venue-data/hot/hot__market_signals_1m.parquet`
- **Refresh:** every 60 s (clobbered). ~90 rows.
- Polymorphic by **`signal_type`** ∈ {`spot`, `atm_iv`, `dvol`,
  `funding`, `volume_last_min`, `block_summary`, `coverage`}.

| `signal_type` | `value` is… | `instrument_kind` | Key extras |
|---|---|---|---|
| `spot` | Deribit `<asset>-PERPETUAL` close (spot **proxy**) | `spot` | `underlying_price` |
| `atm_iv` | ATM `markIV_close`, **vol points** (OKX/Bybit decimals pre-scaled ×100) | `option` | `atm_call_iv`, `atm_put_iv`, `atm_strike`, `underlying_price`, `open_interest` (ATM call+put, coin) |
| `dvol` | Deribit DVOL index, vol points | `index` | — |
| `funding` | current funding rate | `perp` | `funding_8h`, `mark_price`, `underlying_price` (index), `perp_open_interest` (**USD** — its own column) |
| `volume_last_min` | total coin volume in the minute | `option` | `call_volume`, `put_volume`, `buy_volume`, `sell_volume`, `notional_usd`, `trade_count` |
| `block_summary` | block count in the minute | `option` (bullish OTC → `perp`/`spot`) | `block_total_notional_usd`, `block_largest_notional_usd` |
| `coverage` | `seconds_behind` for one consumed stream (null = dead/unknown) | kind of the covered stream | `data_type`, `stream_kind` (`continuous`=gap means death · `intermittent`=trade tape, gap is normal) |

Common cols: `signal_type`, `exchange`, `asset`, `instrument_kind`,
`expiry` (ISO `YYYY-MM-DD`, atm_iv only), `value`, `unit`, `delta_1m`,
`at`, `at_iso`, `generated_at`.

```sql
INSTALL httpfs; LOAD httpfs;
-- BTC ATM IV across venues, right now
SELECT exchange, expiry, atm_strike, value AS atm_iv_vol_points,
       atm_call_iv, atm_put_iv, open_interest, delta_1m
FROM read_parquet('s3://dt-exchange-venue-data/hot/hot__market_signals_1m.parquet')
WHERE signal_type = 'atm_iv' AND asset = 'BTC' ORDER BY expiry;

-- Is any venue's feed stale? (per-stream freshness)
SELECT exchange, data_type, stream_kind, value AS seconds_behind
FROM read_parquet('s3://dt-exchange-venue-data/hot/hot__market_signals_1m.parquet')
WHERE signal_type = 'coverage' ORDER BY value DESC NULLS FIRST;
```

### 2b. `hot__recap_<window>` — Trailing Windows

- **Path:** `s3://dt-exchange-venue-data/hot/hot__recap_<window>.parquet`
- **Windows:** `5m`, `10m`, `20m`, `1h`, `4h`, `8h`, `24h`. **Refresh:** every 5 min.
- Rows by **`row_type`** ∈ {`dvol_spot`, `volume`, `flow`, `block`}
  (the vol surface is **no longer embedded here** — see 2c).

| `row_type` | What | Key columns |
|---|---|---|
| `dvol_spot` | DVOL + spot OHLC | `metric` (`dvol`\|`spot`), `open`, `close`, `high`, `low` |
| `volume` | per (exchange, asset, optionType) volume | `volume_sum`, `notional_usd`, `buy_volume`, `sell_volume`, `trade_count` |
| `flow` | per-contract screen flow (top 150 per exchange+asset; tail folded into `expiry='OTHER'` remainder rows) | `optionType`, `expiry`, `strike`, `side`, `volume_sum`, `notional_usd`, `trade_count`, `avg_iv` |
| `block` | per-block flow (one row per `block_id`) | `block_id`, `notional_usd`, `volume_sum`, `leg_count`, `avg_iv` |

Common cols: `row_type`, `window`, `exchange`, `asset`, `instrument_kind`,
`window_start`/`at` (epoch ms), `window_start_iso`/`at_iso`,
`buckets_expected`/`buckets_present` (equal = complete window), `unit`,
`generated_at`. `avg_iv` = `iv_sum`/`iv_count` (trade-count-weighted).
Filter block rows to `instrument_kind='option'` for option-only block
notional (bullish OTC blocks carry `perp`/`spot`).

### 2c. `hot__vol_surface` — Point-in-Time Vol Surface

- **Path:** `s3://dt-exchange-venue-data/hot/hot__vol_surface.parquet`
- **Refresh:** every 5 min (clobbered). Split out of the recap windows.
- Rows by `row_type`: `strike` (one per exchange/asset/expiry/strike/
  optionType) and `expiry` (per-expiry summary). All `instrument_kind='option'`.

| `row_type` | Columns |
|---|---|
| `strike` | `mark_iv` (vol points), `greek_delta`, `open_interest` (coin), `underlying_price` (USD) |
| `expiry` | `call_oi`, `put_oi`, `total_oi` (coin), `max_pain_strike`, `dte` (days to 08:00-UTC settlement, ≥0) |

```sql
INSTALL httpfs; LOAD httpfs;
-- Full vol surface (skew/term structure) — one read, no fan-out
SELECT exchange, asset, expiry, strike, optionType, mark_iv, greek_delta,
       open_interest, underlying_price
FROM read_parquet('s3://dt-exchange-venue-data/hot/hot__vol_surface.parquet')
WHERE row_type = 'strike' AND asset = 'BTC' AND exchange = 'deribit'
ORDER BY expiry, strike;
```

---

## Dataset 3 — Paradigm Block Trade Tape

Paradigm RFQ block flow (Deribit / Paradex / Bybit). **LIVE — refreshed
hourly** (a Snowflake→S3 egress rewrites both files on the hour, from
`ANALYTICS.TRADE`/`ANALYTICS.RFQ`, `trade_source ∈ {DRFQ, GRFQ, VRFQ}`).

### 3a. `paradigm_trade_tape_slim` — Executed Block Trades

- **Path:** `s3://dt-paradigm-data/paradigm_data/paradigm_trade_tape_slim.csv.gz`
- **Coverage:** **trailing ~6 months** (the hourly rewrite drops rows
  older than 6 months — for anything older, escalate to the warehouse).
- **Layout:** single flat CSV, fully rewritten each hour.

| Column | Type | Notes |
|---|---|---|
| `DATE` | date | Trade date |
| `TIME` | time | Trade time (UTC) |
| `AUCTION` | varchar | `RFQ` or `OB` |
| `PRODUCT` | varchar | e.g. `BTC OPTION - DBT`, `ETH OPTION - DBT`, `BTC OPTION - PRDX`, `BTC PERPETUAL - DBT` |
| `DESCRIPTION` | varchar | Strategy description (e.g. `Straddle 19 Nov 25 3050`) |
| `QTY` | double | Contracts |
| `PRICE` | double | Execution price |
| `REF_PRICE` | double | Reference/mark at trade time |
| `SIDE` | varchar | `BUY` / `SELL` (taker) |
| `QUOTE_CURRENCY` | varchar | `BTC`, `ETH`, `USD`, … |
| `NOTIONAL_VOLUME_USD` | double | USD notional |
| `RFQ_ID` | varchar | Links to RFQ tape |
| `TRADE_ID` | varchar | Unique trade id |
| `BLOCK_TRADE_ID` | varchar | Block group id |

**Options only:** `WHERE PRODUCT LIKE '%OPTION%'`. Exchange suffix: `- DBT`
(Deribit) · `- PRDX` (Paradex) · `- BYB` (Bybit).

### 3b. `paradigm_rfq_tape_slim` — RFQ Activity (incl. unfilled)

- **Path:** `s3://dt-paradigm-data/paradigm_data/paradigm_rfq_tape_slim.csv.gz`
- Same cadence/coverage as 3a (hourly rewrite, trailing ~6 months). Same
  taxonomy plus `NUMBER_OF_QUOTES`, `NUMBER_OF_BLOCK_TRADES`
  (0 = unfilled), `COMPLETED_STATUS`, `LIFESPAN_SECONDS`. Use for fill
  rate / unfilled / RFQ lifespan stats.

> Sibling prefixes under `paradigm_data/` (`rfq_tape_v2/`,
> `trade_tape_v2/`, `trade_tape_anon/`, `rfq_tape/`, `trade_tape_slim/`)
> are **stale one-off exports** (last written 2026-05-10) — ignore them;
> the two slim tapes above are the live surface.

---

## Dataset 4 — Bullish Options (Static Historical Load)

**Distinct from the Dataset 1 bullish feed** (which is spot/perp only):
these are Bullish **options** datasets with **native greeks + IV**.
**STATIC — a one-shot bulk load written 2026-05-11, not refreshed
since.** Fine for historical analysis; do not present as current.

- **Path:** `s3://dt-paradigm-data/paradigm_data/bullish_option_chain_snapshots/date=YYYY-MM-DD/*.csv.gz`
- **Coverage:** chain snapshots end **2026-05-09**.
- **Layout:** Hive-style `date=YYYY-MM-DD/`.
- **Key columns:** `SNAPSHOT_AT`, `SYMBOL`, `BASE_SYMBOL`, `EXPIRY`,
  `STRIKE`, `OPTION_TYPE` (`CALL`/`PUT`), `BID`/`ASK` (+ qty), `BID_IV_PCT`,
  `ASK_IV_PCT`, `IV`, `MARK_PRICE`, `OPEN_INTEREST`, `OPEN_INTEREST_USD`,
  `UNDERLYING_PRICE`, `DELTA`, `GAMMA`, `THETA`, `VEGA`.
- Sibling static datasets from the same load:
  `bullish_options_trades/` (option trades, ends 2026-05-07),
  `bullish_options_orderbook_historical/` (top-2-level book, ends
  2026-03-14), `bullish_orderbook_snapshots/` (ends 2026-05-11),
  `bullish_trades_spot_historical/` (spot tape, ends ~2026-04-25) — all
  Hive `date=YYYY-MM-DD/`.

---

## Dataset 5 — IBIT ETF Options Trades

Equity-side BTC vol cross-reference (IBIT Bitcoin ETF options).
**STALLED — last refreshed 2026-06-02; data ends 2026-06-01.** Treat as
historical until the daily loader is restored; run the glob probe if a
newer date matters.

- **Path:** `s3://dt-paradigm-data/paradigm_data/ibit_options_trades/date=YYYY-MM-DD/*.csv.gz`
- **Coverage:** 2025-12-01 → **2026-06-01**.
- **Layout:** Hive-style `date=YYYY-MM-DD/`. US-equity calendar (no weekends).
- **Columns:** `TRADE_DATE`, `TS_RECV`, `SYMBOL` (OCC-style), `PRICE` (USD),
  `SIZE` (contracts; 1 = 100 shares), `SIDE`.

---

## Dataset 6 — Paradex DEX Trade Tape

On-chain Paradex perpetual trades (historical). Live Paradex state
(positions, funding, orderbook, account) is out of scope.

- **Path (bucket TBC):** `s3://dt-paradex-data/paradex_data/paradex_trade_tape.csv.gz`
  (+ Parquet parts). **Confirm the bucket name and run a `glob()` before
  querying** — this bucket wasn't confirmable at write time.
- **Columns:** `IS_TRADEBUST` (bool), `MARKET` (e.g. `BTC-USD-PERP`),
  `PRICE` (USD), `SIZE`, `TAKER_SIDE`, `TRADE_AT` (UTC).
- **Always filter `WHERE NOT IS_TRADEBUST`.** No precomputed USD notional —
  use `PRICE * SIZE`.

---

## Also under `paradigm_data/` — legacy but live (prefer Dataset 2)

Two prefixes in `dt-paradigm-data/paradigm_data/` still update but are
**superseded by the exchange hot surface** — don't reach for them first:

- `v_vol_surface/` — the older **Deribit-only** vol surface
  (`base=<BTC|ETH>/year=/month=/day=/hour=/v_vol_surface.parquet` hourly
  partitions + a `_hot.parquet` stable key refreshed ~5 min). Live, but
  single-venue; the multi-venue surface is
  `dt-exchange-venue-data/hot/hot__vol_surface.parquet` (Dataset 2c).
- `market_aggregates_5m/` — output of the **old pre-rename pipeline**,
  still running until its cutover teardown. The canonical series is
  `dt-exchange-venue-data/market_aggregates_5m/` (Dataset 1c); this copy
  will stop without notice.

---

## What Is NOT Here

- **The legacy Tardis.dev exchange CSV feed** (`external/tardis/v1/`
  Deribit/OKX option trades, quotes, combo quotes, future quotes) — **gone**.
  Its coverage is superseded by Dataset 1 (the live exchange venue data).
- **Live Bullish options** — the exchange feed carries bullish
  **spot/perp only**, and the Dataset 4 chain snapshots are a static load
  ending 2026-05-09. There is currently **no live Bullish options
  source** in this catalog.
- **Bybit blocks** — Bybit exposes only an is-block flag (no group id), so
  its blocks can't be de-legged; they appear in `flow`, not `block`.
- **Paradex options** — everlasting/perpetual style, excluded; Paradex
  option *block* flow shows in the Paradigm tape as `BTC OPTION - PRDX`.
- **Live exchange streams / raw orderbook / account state** — route to
  live-trading skills. The hot surface (Dataset 2) is the only
  near-real-time entry here.

---

## Cross-Dataset Notes

- **instrument_kind** (Dataset 2) is the explicit options-vs-not label —
  filter on it rather than guessing from `signal_type`/`row_type`.
- **Units:** hot surface is normalized (USD notional / coin volume / vol
  points). The Dataset 1 substrate is closer to venue-native — deribit
  option prices are in index currency (BTC/ETH), OKX size is contracts
  (×`contract_size`); the hot surface already harmonizes these.
- **Timestamps:** hot surface `at`/`window_start` are epoch **ms**
  (`to_timestamp(at/1000)`); each row also has an `_iso` string. Paradigm
  tapes split `DATE`+`TIME`; Paradex tape native timestamp.
- **Join keys:** Paradigm side `RFQ_ID` / `BLOCK_TRADE_ID`; the exchange
  substrate's normalized trades also carry `block_id` (deribit/okex
  `block_trade_id`, bullish `otcTradeId`; bybit null).
- **Coverage probe:** substrate is daily `YYYY/MM/DD/`; Bullish-chain and
  IBIT are Hive `date=YYYY-MM-DD/`. Use the matching regex from `SKILL.md`
  Step 3.
