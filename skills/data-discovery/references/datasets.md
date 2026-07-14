# Available Market Data — S3 Catalog

Comprehensive map of market data accessible via DuckDB + S3. Covers crypto
options (Paradigm RFQ flow, exchange option data), crypto futures top-of-book,
and the on-chain Paradex perp trade tape.

> **Date ranges below are point-in-time as of last verification: 2026-05-11.**
> Coverage expands over time — for any "recent date" question, run the glob
> probe in `SKILL.md` Step 3 to confirm.

---

## S3 Buckets

Market data is spread across **three buckets**, all in region
`ap-northeast-1` and reachable with the **same IRSA credentials** (EKS
web identity → STS) — see `s3-access.md`:

- **`s3://dt-paradigm-data`** — the Paradigm datasets, **keeping the
  `paradigm_data/` prefix**: the executed block-trade tape
  (`paradigm_data/paradigm_trade_tape_slim.csv.gz`), the RFQ activity tape
  (`paradigm_data/paradigm_rfq_tape_slim.csv.gz`), and the consolidated
  vol surface (`paradigm_data/v_vol_surface/`).
- **`s3://dt-exchange-venue-data`** — the near-real-time hot surface at the
  **bucket root** (no `paradigm_data/` prefix): the live signals snapshot
  (`hot/hot__market_signals_1m.parquet`) and the rolling recap aggregates
  (`hot/hot__recap_aggregates_5m_24h.parquet`).
- **`s3://terminal-dime-prod`** — **DEPRECATED bucket, being retired.** Do
  **not** build new dependencies on it. Its only remaining contents are the
  Paradex DEX trade tape (`paradex_data/paradex_trade_tape.csv.gz`, itself
  unavailable pending re-home — see Dataset 3) and the Tardis exchange market
  data (`external/tardis/v1/...`, Dataset 2), both awaiting re-home to
  `dt-exchange-venue-data`.

> **Migration note:** the Paradigm tapes + vol surface now live on
> `dt-paradigm-data` (`paradigm_data/` prefix preserved); the hot surface +
> recap aggregates moved to `dt-exchange-venue-data` (prefix dropped). The
> former Bullish (`bullish_*`) and IBIT (`ibit_options_trades`) datasets have
> been **removed** — verified 2026-07-14, they no longer exist in any bucket
> (Bullish still appears as a *venue* inside the hot surface + recap
> aggregates; that is unrelated to the deleted standalone datasets). Anything
> still on `terminal-dime-prod` (Paradex tape, `external/` Tardis tree) is on
> the deprecated bucket pending re-home — do not build dependencies on it.

---

## Dataset 1 — Paradigm Block Trade Tape

Paradigm RFQ block flow. Primary source for options block trades executed via
Paradigm across Deribit, Paradex, and Bybit.

### 1a. `paradigm_trade_tape_slim` — Executed Block Trades

- **Path:** `s3://dt-paradigm-data/paradigm_data/paradigm_trade_tape_slim.csv.gz`
- **Last verified coverage:** 2025-11-09 → 2026-05-09
- **Layout:** Single flat CSV — all dates in one file. Coverage likely extends forward.

| Column | Type | Notes |
|---|---|---|
| `DATE` | date | Trade date |
| `TIME` | time | Trade time (UTC) |
| `AUCTION` | varchar | `RFQ` or `OB` (order book) |
| `PRODUCT` | varchar | See product types below |
| `DESCRIPTION` | varchar | Human-readable strategy description |
| `QTY` | double | Quantity (contracts) |
| `PRICE` | double | Execution price |
| `REF_PRICE` | double | Reference/mark price at time of trade |
| `SIDE` | varchar | `BUY` / `SELL` (taker side) |
| `QUOTE_CURRENCY` | varchar | `BTC`, `ETH`, `USD`, etc. |
| `NOTIONAL_VOLUME_USD` | double | USD notional |
| `RFQ_ID` | varchar | Links to RFQ tape |
| `TRADE_ID` | varchar | Unique trade identifier |
| `BLOCK_TRADE_ID` | varchar | Block trade group identifier |

**Product types in trade tape:**

| PRODUCT | Description |
|---|---|
| `BTC OPTION - DBT` | BTC options on Deribit (~21k trades) |
| `ETH OPTION - DBT` | ETH options on Deribit (~11k trades) |
| `SOL OPTION - DBT` | SOL options on Deribit |
| `XRP OPTION - DBT` | XRP options on Deribit |
| `BTC OPTION - PRDX` | BTC options on Paradex |
| `BTC PERPETUAL - DBT` | BTC perps on Deribit |
| `ETH PERPETUAL - DBT` | ETH perps on Deribit |
| `BTC FUTURE - DBT` | BTC futures on Deribit |
| `ETH FUTURE - DBT` | ETH futures on Deribit |

**`DESCRIPTION` examples (options strategies):**

- Outright: `Call 26 Dec 25 104000`, `Put 23 Jan 26 95000`
- Straddle: `Straddle 19 Nov 25 3050`
- Strangle: `Strangle 27 Mar 26 90000/95000`
- Call Spread: `CSpd 27 Mar 26 85000/110000`
- Put Spread: `PSpd 16 Jan 26 95000/93000`
- Risk Reversal: `RRCall 30 Jan 26 70000/108000`
- Iron Fly: `IFly 26 Jun 26 75000/85000/95000`
- Put Fly: `PFly 27 Mar 26 60000/50000/40000`
- Call Calendar: `CCal 27 Feb 26 75000 / 26 Jun 26 75000`
- Put Calendar: `PCal 26 Dec 25 86000 / 27 Mar 26 85000`
- Custom multi-leg: `Cstm +1.00 Call 24 Apr 26 78000 -2.00 Call 24 Apr 26 85000`

**Filter for options only:** `WHERE PRODUCT LIKE '%OPTION%'`

---

### 1b. `paradigm_rfq_tape_slim` — RFQ Activity (including unfilled)

- **Path:** `s3://dt-paradigm-data/paradigm_data/paradigm_rfq_tape_slim.csv.gz`
- **Last verified coverage:** 2025-11-09 → 2026-05-09
- **Layout:** Single flat CSV. Includes both completed and expired/uncompleted RFQs.

| Column | Type | Notes |
|---|---|---|
| `DATE` | date | RFQ date |
| `TIME` | time | RFQ creation time (UTC) |
| `AUCTION` | varchar | `RFQ` or `OB` |
| `PRODUCT` | varchar | Same product taxonomy as trade tape |
| `DESCRIPTION` | varchar | Strategy description |
| `QTY` | double | Requested quantity |
| `QUOTE_CURRENCY` | varchar | |
| `NOTIONAL_VOLUME_USD` | double | USD notional |
| `NUMBER_OF_QUOTES` | bigint | Quotes received from MMs |
| `NUMBER_OF_BLOCK_TRADES` | bigint | Trades executed (0 = unfilled) |
| `COMPLETED_STATUS` | varchar | e.g. `COMPLETE`, `EXPIRED` |
| `LIFESPAN_SECONDS` | bigint | How long the RFQ was live |
| `RFQ_ID` | varchar | Links to trade tape |

**Additional product types present in RFQ tape only:**

| PRODUCT | Description |
|---|---|
| `BTC OPTION_FUTURE - DBT` | BTC option+future combo on Deribit (~1,100 RFQs) |
| `ETH OPTION_FUTURE - DBT` | ETH option+future combo on Deribit |
| `AVAX OPTION - DBT` | AVAX options on Deribit |
| `BTC OPTION - BYB` | BTC options on Bybit |

---

## Dataset 2 — Deribit & OKX Options

Raw exchange market data. Useful for vol surface
construction, execution benchmarking, and Greeks calculation.

> **⚠ Availability caveat:** these datasets live under `external/` on
> `s3://terminal-dime-prod` and were **not** part of the venue-data
> migration to `dt-exchange-venue-data`. The `external/tardis/v1/` tree is
> either static historical data loaded directly into the bucket or
> unavailable — **probe with a `glob()` before relying on it**, and don't
> assume it refreshes. For live/recent IV and flow, prefer the hot surface
> (Dataset 4) on `s3://dt-exchange-venue-data` and `v_vol_surface` on
> `s3://dt-paradigm-data`.

### 2a. Deribit — Option Trades

- **Path:** `s3://terminal-dime-prod/external/tardis/v1/trades/option/deribit/YYYY/MM/DD/deribit-OPTIONS-YYYY-MM-DD.csv.gz`
- **Last verified coverage:** 2026-02-01 → 2026-04-30 (1 file/day)
- **Layout:** Daily partitioned. Most consistently populated options dataset. Likely extends forward.
- **Assets:** BTC + ETH dated options
- **Instrument format:** `BTC-27FEB26-60000-P`, `ETH-6FEB26-3000-C`

| Column | Type | Notes |
|---|---|---|
| `exchange` | varchar | Always `deribit` |
| `symbol` | varchar | e.g. `BTC-27FEB26-95000-C` |
| `timestamp` | bigint | Microseconds since epoch (UTC) |
| `local_timestamp` | bigint | Receipt timestamp (µs) |
| `id` | varchar | Trade ID |
| `side` | varchar | `buy` / `sell` (taker side) |
| `price` | double | In BTC/ETH (index currency, not USD) |
| `amount` | double | Contracts |

**Example query:**

```sql
SELECT symbol, side, price, amount,
  to_timestamp(timestamp / 1e6) AS ts
FROM read_csv_auto('s3://terminal-dime-prod/external/tardis/v1/trades/option/deribit/2026/03/01/deribit-OPTIONS-2026-03-01.csv.gz')
WHERE symbol LIKE 'BTC-%'
ORDER BY timestamp;
```

---

### 2b. Deribit — Option Quotes / Top-of-Book

- **Path:** `s3://terminal-dime-prod/external/tardis/v1/quotes/option/deribit/YYYY/MM/DD/deribit-OPTIONS-YYYY-MM-DD.csv.gz`
- **Last verified coverage:** 2026-01-01 only (1 file — currently sparse)
- **Layout:** Daily partitioned. Limited — check for newer files before assuming unavailable.

| Column | Type | Notes |
|---|---|---|
| `exchange` | varchar | |
| `symbol` | varchar | Same format as trades |
| `timestamp` | bigint | µs epoch |
| `local_timestamp` | bigint | |
| `ask_amount` | double | |
| `ask_price` | double | |
| `bid_price` | double | |
| `bid_amount` | double | |

---

### 2c. Deribit — Combo Quotes

- **Path:** `s3://terminal-dime-prod/external/tardis/v1/quotes/combo/deribit/YYYY/MM/DD/deribit-<ASSET>-<STRATEGY>-<LEGS>-YYYY-MM-DD.csv.gz`
- **Last verified coverage:** 2026-01-01 → 2026-05-09 (~23,055 files)
- **Layout:** Daily partitioned, one file per combo instrument per day. Densest dataset. Ongoing.
- **Assets:** BTC + ETH
- **Schema:** `exchange, symbol, timestamp, local_timestamp, ask_amount, ask_price, bid_price, bid_amount`

**Combo instrument naming:**

| Code | Strategy | Example |
|---|---|---|
| `CS` | Call Spread | `BTC-CS-16JAN26-95000_100000` |
| `CCAL` | Call Calendar Spread | `BTC-CCAL-9JAN26_2JAN26-90000` |
| `CDIAG` | Call Diagonal | `BTC-CDIAG-26JUN26_2JAN26-60000_90000` |
| `FS` | Futures Calendar Spread | `BTC-FS-25DEC26_26JUN26` |
| `CSR12` / `CSR13` / `CSR23` | Ratio Call Spreads | `BTC-CSR12-16JAN26-88000_92000` |

**Glob pattern for all BTC combos on a given day:**

```sql
SELECT * FROM read_csv_auto(
  's3://terminal-dime-prod/external/tardis/v1/quotes/combo/deribit/2026/03/15/deribit-BTC-*.csv.gz'
);
```

---

### 2d. OKX — Option Trades

- **Path:** `s3://terminal-dime-prod/external/tardis/v1/trades/option/okex-options/YYYY/MM/DD/okex-options-OPTIONS-YYYY-MM-DD.csv.gz`
- **Last verified coverage:** 2026-02-01 → 2026-04-30 (~87 files)
- **Layout:** Daily partitioned. Parallel to Deribit trades but may diverge — verify independently.
- **Schema:** Same as Deribit option trades (`exchange, symbol, timestamp, local_timestamp, id, side, price, amount`).

---

### 2e. Future Quotes / Top-of-Book (Deribit, Bybit, OKX)

Top-of-book quote snapshots for dated and perpetual futures across three
venues. Same schema as Deribit option quotes.

| Venue | Path | Last verified coverage | Files |
|---|---|---|---|
| Deribit | `s3://terminal-dime-prod/external/tardis/v1/quotes/future/deribit/YYYY/MM/DD/` | 2026-01-01 → 2026-05-09 | ~2,186 |
| Bybit | `s3://terminal-dime-prod/external/tardis/v1/quotes/future/bybit/YYYY/MM/DD/` | 2026-01-01 → 2026-05-09 | ~4,442 |
| OKX | `s3://terminal-dime-prod/external/tardis/v1/quotes/future/okex-futures/YYYY/MM/DD/` | 2026-03-01 → 2026-05-09 | ~1,858 |

**Schema** (identical to Deribit option quotes):

| Column | Type | Notes |
|---|---|---|
| `exchange` | varchar | `deribit` / `bybit` / `okex-futures` |
| `symbol` | varchar | Venue-native future symbol |
| `timestamp` | bigint | Microseconds since epoch (UTC) |
| `local_timestamp` | bigint | Receipt timestamp (µs) |
| `ask_amount` | double | |
| `ask_price` | double | |
| `bid_price` | double | |
| `bid_amount` | double | |

Use these for funding-context, basis, and term-structure analysis alongside
the option datasets.

---

## Dataset 3 — Paradex DEX Trade Tape

On-chain Paradex perpetual trades — the *executed-trade* tape of the
Paradex DEX. This is **historical Paradex trade data**; live Paradex markets,
positions, orderbook, funding, and account data remain out of scope — those
belong to live trading/market tooling, not this historical catalog.

> **⚠ DEPRECATED — on `terminal-dime-prod`, do not build dependencies.** This
> tape is on the deprecated `terminal-dime-prod` bucket and is currently
> **unavailable pending re-home to `dt-exchange-venue-data`**. The entry is
> kept for reference (schema + join semantics) but must not be wired into new
> workflows until the re-home lands and this warning is removed.

- **Path (deprecated):** `s3://terminal-dime-prod/paradex_data/paradex_trade_tape.csv.gz`
  (plus Parquet parts in the same prefix) — pending re-home to `dt-exchange-venue-data`.
- **Last verified coverage:** starts 2024-12-26 (full range TBC — run the
  coverage probe before assuming an end-date).
- **Layout:** Flat CSV + Parquet parts. Confirm with `glob()` before querying.

| Column | Type | Notes |
|---|---|---|
| `IS_TRADEBUST` | boolean | True if trade was busted / cancelled |
| `MARKET` | varchar | Paradex market symbol (e.g. `BTC-USD-PERP`) |
| `PRICE` | double | Trade price (USD) |
| `SIZE` | double | Contracts |
| `TAKER_SIDE` | varchar | `BUY` / `SELL` (taker direction) |
| `TRADE_AT` | timestamp | Trade time (UTC) |

**Filter out busted trades:** `WHERE NOT IS_TRADEBUST`.

---

## Dataset 4 — Hot Surface (Live Snapshot + Trailing Windows)

Single-file LLM-shaped market snapshot. The fastest way to answer
"what's happening right now" without scanning per-period data —
clobbered every 60 seconds at a stable key. Use this **before** any
exchange `web_fetch` for spot price, ATM IV, DVOL, last-minute volume,
or live block-trade activity.

This is the only dataset in this catalog that is **near-real-time**
(1-minute cadence). All other datasets are historical-only.

### 4a. `hot__market_signals_1m` — Live Market Heartbeat

- **Path:** `s3://dt-exchange-venue-data/hot/hot__market_signals_1m.parquet`
- **Refresh:** every 60 s (clobbered in place; bucket versioning retains
  prior snapshots recoverably for ~24 h)
- **Row count:** ~50–70 (bounded by design; fits in any LLM context)
- **Schema:** 20 cols, polymorphic via `signal_type`

| Column | Type | Notes |
|---|---|---|
| `signal_type` | varchar | Discriminator: `spot` \| `atm_iv` \| `dvol` \| `volume_last_min` \| `block_summary` |
| `exchange` | varchar | Source venue (`deribit`, `okex-options`, `bybit-options`, `bullish`) |
| `asset` | varchar | `BTC` or `ETH` |
| `expiry` | varchar | Venue-native expiry (populated for `atm_iv`; null otherwise) |
| `value` | double | Primary metric — interpretation depends on `signal_type` |
| `delta_1m` | double | Change in `value` vs 1 minute ago (null on first run / after gaps) |
| `at` | bigint | Unix **milliseconds** — source period_start (not ISO string) |
| `atm_call_iv`, `atm_put_iv`, `atm_strike`, `underlying_price`, `open_interest` | double | `atm_iv` rows only; null elsewhere |
| `call_volume`, `put_volume`, `buy_volume`, `sell_volume`, `notional` | double | `volume_last_min` rows only |
| `trade_count` | bigint | `volume_last_min` rows only |
| `block_total_notional`, `block_largest_notional` | double | `block_summary` rows only (deribit + okex + bullish) |

**`value` semantics per signal_type:**

| `signal_type` | `value` is… | Units |
|---|---|---|
| `spot` | Spot/perp close price | Venue-native |
| `atm_iv` | ATM strike's `markIV_close` | **Vol points uniformly** (OKX/Bybit decimal IVs pre-scaled ×100) |
| `dvol` | Latest DVOL index value | Vol points |
| `volume_last_min` | Total `amount` traded in the minute | Venue-native |
| `block_summary` | Block count in the minute | Integer (stored as double) |

**Read pattern (DuckDB):**

```sql
INSTALL httpfs; LOAD httpfs;

-- What's BTC ATM IV across venues right now?
SELECT exchange, expiry, atm_strike, value AS atm_iv_vol_points,
       atm_call_iv, atm_put_iv, open_interest, delta_1m
FROM read_parquet('s3://dt-exchange-venue-data/hot/hot__market_signals_1m.parquet')
WHERE signal_type = 'atm_iv' AND asset = 'BTC'
ORDER BY expiry;

-- What just printed in the last minute?
SELECT exchange, asset, value AS total_volume,
       call_volume, put_volume, buy_volume, sell_volume,
       notional, trade_count
FROM read_parquet('s3://dt-exchange-venue-data/hot/hot__market_signals_1m.parquet')
WHERE signal_type = 'volume_last_min';

-- Block activity in the last minute (Deribit only today)
SELECT exchange, asset, value AS block_count,
       block_total_notional, block_largest_notional
FROM read_parquet('s3://dt-exchange-venue-data/hot/hot__market_signals_1m.parquet')
WHERE signal_type = 'block_summary';
```

**When to use:** front-month ATM IV across venues, current spot,
last-minute volume + call/put split, DVOL, block activity — any "right
now" question. Reach for the snapshot **before** doing exchange `web_fetch`
for the same data; one S3 read replaces several round-trips.

**Caveats:**

- `at` is **Unix milliseconds**, not an ISO string. Convert via
  `to_timestamp(at / 1000)`.
- IV columns are uniformly in **vol points** — `atm_call_iv` from OKX
  reads `38.03`, not `0.38`, despite OKX's wire format using decimals.
  The snapshot pre-scales them at materialisation time. (The per-period
  source files keep venue-native units — don't cross-join the snapshot
  with those without conversion.)
- `expiry` is venue-native (`20JUN26` from Deribit, `260620` from OKX).
  Parse per-venue for canonical dates.
- `block_summary` covers **Deribit, OKX, and Bullish** (each venue's
  native block/OTC id). Bybit is excluded — it exposes only an
  is-block flag with no group id, so its blocks can't be de-legged.
- The snapshot does **not** carry the full vol surface — only ATM. For
  the full surface across strikes/expiries, read the consolidated
  single-GET file
  `s3://dt-paradigm-data/paradigm_data/v_vol_surface/_hot.parquet`
  (stable key, refreshed ~5 min; per-strike `mark_iv`/`delta` keyed by
  instrument `symbol`), plus its hourly cold partitions
  `.../v_vol_surface/base=<ASSET>/year=/month=/day=/hour=/` for historical
  window-open snapshots. (The recap aggregates file, Dataset 4b, no longer
  carries `surface` rows.)

### 4b. Hot Recap Aggregates — 5-min Rolling Window Source

A single rolling file of **5-minute aggregate buckets over the trailing
24h** — the query-time source for "last `<window>`" recaps (it replaces
the old per-window `hot__recap_<window>` files, which no longer exist).

- **Path:** `s3://dt-exchange-venue-data/hot/hot__recap_aggregates_5m_24h.parquet`
- **Granularity:** one row-set per 5-min bucket (`bucket_at`, Unix ms); ~289 buckets ≈ 24h
- **Refresh:** every ~5 minutes (clobbered in place at a stable key)
- **Windowing:** apply the window **yourself** — filter
  `bucket_at >= <now_ms> - <window_ms>` then aggregate. Any window ≤24h,
  arbitrary lengths supported.

**Venues (5).** `exchange` is one of `deribit`, `deribit-usdc`, `okex-options`,
`bybit-options`, `bullish`. `deribit` is the BTC-inverse venue; `deribit-usdc`
is its USDC-linear sibling (a *distinct* venue with a different contract unit)
and is the **only** source of alt options here — **AVAX, HYPE, SOL, TRX, XRP**
appear only under `deribit-usdc`. `dvol_spot` rows are **Deribit-only** (no
other venue emits DVOL/spot), so key them off `exchange = 'deribit'` explicitly
rather than assuming a single row per metric.

Rows use a **`row_type`** discriminator with **four** kinds — **no
`surface`** (the vol surface lives in `v_vol_surface`; see the Dataset 4a note):

| `row_type` | What | Key columns |
|---|---|---|
| `dvol_spot` | DVOL + spot OHLC per 5-min bucket | `metric` (`dvol`\|`spot`), `open`, `close`, `high`, `low` |
| `volume` | Per (exchange, optionType) traded volume | `volume_sum`, `notional_usd`, `buy_volume`, `sell_volume`, `trade_count` |
| `flow` | Per-contract screen flow | `exchange`, `optionType`, `expiry`, `strike`, `side`, `volume_sum`, `trade_count`, `iv_sum`, `iv_count` |
| `block` | Per-block trade flow | `exchange`, `block_id`, `notional_usd`, `volume_sum`, `leg_count`, `iv_sum`, `iv_count` |

Common cols: `row_type`, `exchange`, `asset`, `bucket_at` (Unix ms),
`instrument_kind`, `underlying_price`.

**Gotchas:**
- Timestamp is **`bucket_at`** (Unix ms) — filter/aggregate on it.
- **Never sum `volume_sum` OR `notional_usd` across venues or across assets.**
  `volume_sum` is in each venue's **native contract unit** (OKX/Bybit in
  contracts, Deribit in coin, deribit-usdc USDC-linear). `notional_usd` is the
  option **premium** in USD (NOT underlying notional), and its per-contract
  basis differs by venue and by asset — so a cross-venue or cross-asset sum of
  either column is meaningless. Aggregate only *within* a single
  `(exchange, asset)`; for a cross-venue total use the unit-free `trade_count`.
- IV is `iv_sum`/`iv_count` on `flow`/`block` rows — compute
  `iv_sum/iv_count`; there is no `avg_iv` column.
- OHLC over a window: `arg_min(open, bucket_at)` = window open,
  `arg_max(close, bucket_at)` = window close, `max(high)`, `min(low)`.
- No `surface` rows — for the vol surface read
  `s3://dt-paradigm-data/paradigm_data/v_vol_surface/` (Dataset 4a note).

**Read pattern (DuckDB) — roll the window up in-query:**

```sql
INSTALL httpfs; LOAD httpfs;

-- DVOL + spot OHLC over the last hour
SELECT exchange, metric,
  arg_min(open, bucket_at) AS open, arg_max(close, bucket_at) AS close,
  max(high) AS high, min(low) AS low
FROM read_parquet('s3://dt-exchange-venue-data/hot/hot__recap_aggregates_5m_24h.parquet')
WHERE row_type = 'dvol_spot' AND asset = 'BTC'
  AND bucket_at >= (SELECT max(bucket_at) - 3600*1000
                    FROM read_parquet('s3://dt-exchange-venue-data/hot/hot__recap_aggregates_5m_24h.parquet'))
GROUP BY exchange, metric;

-- Volume by venue over the last 24h. GROUP BY exchange keeps each venue's
-- native-unit sums separate — do NOT collapse volume_sum/notional_usd across
-- venues (units differ; notional_usd is premium). trade_count is the only
-- column safe to total across venues.
SELECT exchange, optionType, sum(volume_sum) AS volume_sum,
       sum(notional_usd) AS notional_usd_premium, sum(trade_count) AS trade_count
FROM read_parquet('s3://dt-exchange-venue-data/hot/hot__recap_aggregates_5m_24h.parquet')
WHERE row_type = 'volume' AND asset = 'BTC'
  AND bucket_at >= (SELECT max(bucket_at) - 24*3600*1000
                    FROM read_parquet('s3://dt-exchange-venue-data/hot/hot__recap_aggregates_5m_24h.parquet'))
GROUP BY exchange, optionType;
```

**When to use:** a "last `<window>`" question (volume / flow / blocks /
DVOL move over 5m–24h) — one S3 read of this file, filtered + aggregated
to the window. For the vol surface use `v_vol_surface`; for anything
beyond 24h, use the historical per-period datasets above.

---

## What Is NOT Here

- **Deribit option quotes beyond 2026-01-01** — only trades are consistently available.
- **OKX combo quotes** — not present.
- **OKX option quotes** — not present in this catalog.
- **Greeks / IV in the option/future feed data** — not present; compute them from
  trades/underlying. (For a live ATM-IV / DVOL read across venues use the hot
  surface, Dataset 4a; for the full per-strike surface use `v_vol_surface`.)
- **Paradex options data** — Paradex options are everlasting/perpetual style
  with no expiry date and are excluded from this catalog. Paradex options
  *block-trade* flow is visible in the Paradigm trade tape under
  `BTC OPTION - PRDX`. Paradex *perp* trade flow is in Dataset 3.
- **Live exchange streams / raw orderbook / account state** — for raw
  exchange tick streams, live order books, and account state, route to
  the live-data skills. Dataset 4 (hot surface) is this
  catalog's only near-real-time entry and serves the "what's happening
  right now" use case at 1-minute grain.

---

## Cross-Dataset Notes

- **Timestamp units:**
  - Option/future market data (`timestamp`, `local_timestamp`): **microseconds** since epoch →
    `to_timestamp(timestamp / 1e6)` in DuckDB.
  - Paradigm tapes: split `DATE` + `TIME` columns.
  - Paradex tape: native `timestamp` types (seconds resolution).
- **Deribit option price units:** **index currency** (BTC for BTC options,
  ETH for ETH options), not USD.
- **Deribit amount units:** contracts (1 BTC contract = 1 BTC notional;
  1 ETH contract = 1 ETH notional).
- **Joining Paradigm + market data:** Use `RFQ_ID` / `BLOCK_TRADE_ID` on the
  Paradigm side; join to the option/future market data by symbol + timestamp
  window for market context.
- **Paradigm options filter:** `WHERE PRODUCT LIKE '%OPTION%'`.
- **Paradigm exchange suffix:** `DBT` = Deribit, `PRDX` = Paradex,
  `BYB` = Bybit.
