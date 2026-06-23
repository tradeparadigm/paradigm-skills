# Available Market Data — S3 Catalog

Comprehensive map of market data accessible via DuckDB + S3. Covers crypto
options (Paradigm RFQ flow, exchange option data, Bullish), ETF
options (IBIT), crypto futures top-of-book, and the on-chain Paradex perp
trade tape.

> **Date ranges below are point-in-time as of last verification: 2026-05-11.**
> Coverage expands over time — for any "recent date" question, run the glob
> probe in `SKILL.md` Step 3 to confirm.

---

## S3 Bucket

- **Bucket:** `s3://terminal-dime-prod`
- **Region:** `ap-northeast-1`
- **Auth:** IRSA (EKS web identity → STS) — see `s3-access.md`.

---

## Dataset 1 — Paradigm Block Trade Tape

Paradigm RFQ block flow. Primary source for options block trades executed via
Paradigm across Deribit, Paradex, and Bybit.

### 1a. `paradigm_trade_tape_slim` — Executed Block Trades

- **Path:** `s3://terminal-dime-prod/paradigm_data/paradigm_trade_tape_slim.csv.gz`
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

- **Path:** `s3://terminal-dime-prod/paradigm_data/paradigm_rfq_tape_slim.csv.gz`
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

## Dataset 3 — Bullish (Options)

Live snapshots of option chain and order book from the Bullish exchange.
Unlike the option/future feed data, the chain snapshot dataset includes **greeks and IV directly**.

### 3a. Bullish — Option Chain Snapshots

- **Path:** `s3://terminal-dime-prod/paradigm_data/bullish_option_chain_snapshots/date=YYYY-MM-DD/*.csv.gz`
- **Last verified coverage:** 2026-05-07 → 2026-05-11 (very recent, ongoing)
- **Layout:** Hive-style date partition (`date=YYYY-MM-DD`).

| Column | Type | Notes |
|---|---|---|
| `SNAPSHOT_AT` | timestamp | Snapshot time (UTC) |
| `SYMBOL` | varchar | Bullish option symbol |
| `BASE_SYMBOL` | varchar | Underlying (e.g. `BTC`) |
| `EXPIRY` | timestamp/date | Option expiry |
| `STRIKE` | double | |
| `OPTION_TYPE` | varchar | `CALL` / `PUT` |
| `BID` | double | |
| `BID_QTY` | double | |
| `ASK` | double | |
| `ASK_QTY` | double | |
| `BID_IV_PCT` | double | Bid IV (percent) |
| `ASK_IV_PCT` | double | Ask IV (percent) |
| `IV` | double | Mid/mark IV |
| `MARK_PRICE` | double | |
| `LAST` | double | Last trade price |
| `OPEN_INTEREST` | double | |
| `OPEN_INTEREST_USD` | double | |
| `UNDERLYING_PRICE` | double | |
| `SETTLEMENT_ASSET` | varchar | |
| `DELTA` | double | |
| `GAMMA` | double | |
| `THETA` | double | |
| `VEGA` | double | |

This is the **only dataset in the catalog with native greeks/IV** — preferred
over computing them from raw option trades when Bullish coverage applies.

---

### 3b. Bullish — Options Order Book (Historical, Top-2 Levels)

- **Path:** `s3://terminal-dime-prod/paradigm_data/bullish_options_orderbook_historical/date=YYYY-MM-DD/*.csv.gz`
- **Last verified coverage:** 2026-02-01 → 2026-04-25 (~84 files)
- **Layout:** Hive-style date partition (`date=YYYY-MM-DD`).
- **Depth:** Top-2 bid + top-2 ask levels.

| Column | Type | Notes |
|---|---|---|
| `SNAPSHOT_TIME` | timestamp | When snapshot was taken |
| `EXCHANGE_TIMESTAMP` | timestamp | Bullish-side timestamp |
| `MARKET_PAIR` | varchar | Bullish market pair identifier |
| `ASK1_PRICE` | double | Best ask price |
| `ASK1_QTY` | double | Best ask size |
| `ASK2_PRICE` | double | Second-best ask price |
| `ASK2_QTY` | double | Second-best ask size |
| `BID1_PRICE` | double | Best bid price |
| `BID1_QTY` | double | Best bid size |
| `BID2_PRICE` | double | Second-best bid price |
| `BID2_QTY` | double | Second-best bid size |

---

## Dataset 4 — IBIT ETF Options Trades

Equity-side vol data: trades on options for the IBIT Bitcoin ETF.
Not crypto-native — useful as a cross-asset reference against Deribit
BTC option flow.

- **Path:** `s3://terminal-dime-prod/paradigm_data/ibit_options_trades/date=YYYY-MM-DD/*.csv.gz`
- **Last verified coverage:** 2025-12-01 → 2026-05-08 (~110 files, trading-day partitioned)
- **Layout:** Hive-style date partition. Weekends/holidays absent (US equity calendar).

| Column | Type | Notes |
|---|---|---|
| `TRADE_DATE` | date | |
| `TS_RECV` | timestamp | Receipt timestamp |
| `SYMBOL` | varchar | IBIT option symbol (OCC-style) |
| `PRICE` | double | Trade price (USD) |
| `SIZE` | bigint | Contracts (1 contract = 100 shares) |
| `SIDE` | varchar | `BUY` / `SELL` |

**Use cases:** ETF vs crypto-native skew comparison, weekend-gap analysis,
spot-ETF flow vs crypto-spot flow.

---

## Dataset 5 — Paradex DEX Trade Tape

On-chain Paradex perpetual trades — the *executed-trade* tape of the
Paradex DEX. This is **historical Paradex trade data** and is in scope for
this catalog; live Paradex markets, positions, orderbook, funding, and
account data remain out of scope — those belong to live trading/market
tooling, not this historical catalog.

- **Path:** `s3://terminal-dime-prod/paradex_data/paradex_trade_tape.csv.gz`
  (plus Parquet parts in the same prefix)
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

## Dataset 6 — Hot Pulse (Live Snapshot)

Single-file LLM-shaped market snapshot. The fastest way to answer
"what's happening right now" without scanning per-period data —
clobbered every 60 seconds at a stable key. Use this **before** any
exchange `web_fetch` for spot price, ATM IV, DVOL, last-minute volume,
or live block-trade activity.

This is the only dataset in this catalog that is **near-real-time**
(1-minute cadence). All other datasets are historical-only.

### 6a. `hot_pulse` — Live Market Heartbeat

- **Path:** `s3://terminal-dime-prod/paradigm_data/realtime/hot/hot__snapshot.parquet`
- **Refresh:** every 60 s (clobbered in place; bucket versioning retains
  prior pulses recoverably for ~24 h)
- **Row count:** ~35–50 (bounded by design; fits in any LLM context)
- **Schema:** 19 cols, polymorphic via `signal_type`

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
| `block_total_notional`, `block_largest_notional` | double | `block_summary` rows only (Deribit raw only today) |

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
FROM read_parquet('s3://terminal-dime-prod/paradigm_data/realtime/hot/hot__snapshot.parquet')
WHERE signal_type = 'atm_iv' AND asset = 'BTC'
ORDER BY expiry;

-- What just printed in the last minute?
SELECT exchange, asset, value AS total_volume,
       call_volume, put_volume, buy_volume, sell_volume,
       notional, trade_count
FROM read_parquet('s3://terminal-dime-prod/paradigm_data/realtime/hot/hot__snapshot.parquet')
WHERE signal_type = 'volume_last_min';

-- Block activity in the last minute (Deribit only today)
SELECT exchange, asset, value AS block_count,
       block_total_notional, block_largest_notional
FROM read_parquet('s3://terminal-dime-prod/paradigm_data/realtime/hot/hot__snapshot.parquet')
WHERE signal_type = 'block_summary';
```

**When to use:** front-month ATM IV across venues, current spot,
last-minute volume + call/put split, DVOL, block activity — any "right
now" question. Reach for pulse **before** doing exchange `web_fetch`
for the same data; one S3 read replaces several round-trips.

**Caveats:**

- `at` is **Unix milliseconds**, not an ISO string. Convert via
  `to_timestamp(at / 1000)`.
- IV columns are uniformly in **vol points** — `atm_call_iv` from OKX
  reads `38.03`, not `0.38`, despite OKX's wire format using decimals.
  Pulse pre-scales them at materialisation time. (The per-period
  source files keep venue-native units — don't cross-join pulse with
  those without conversion.)
- `expiry` is venue-native (`20JUN26` from Deribit, `260620` from OKX).
  Parse per-venue for canonical dates.
- `block_summary` covers **Deribit, OKX, and Bullish** (each venue's
  native block/OTC id). Bybit is excluded — it exposes only an
  is-block flag with no group id, so its blocks can't be de-legged.
- Pulse does **not** carry the full vol surface — only ATM. For the
  full surface across strikes/expiries use
  `paradigm_data/v_vol_surface/` (separate dataset, computed by the IV
  pipeline; different shape and cadence).

### 6b. Hot Windows — Trailing Aggregates

Alongside the snapshot, the hot surface publishes pre-aggregated
**trailing windows** — same prefix, one file per window:

- **Path:** `s3://terminal-dime-prod/paradigm_data/realtime/hot/hot__<window>.parquet`
- **Windows:** `5m`, `10m`, `20m`, `1h`, `4h`, `8h`, `24h`
- **Refresh:** every 5 minutes (clobbered in place)

Each file holds exactly one trailing window (the `window` column equals
the file's window). Unlike the snapshot's `signal_type` shape, windows
use a **`row_type`** discriminator with three kinds:

| `row_type` | What | Key columns |
|---|---|---|
| `dvol_spot` | DVOL + spot OHLC over the window | `metric` (`dvol`\|`spot`), `open`, `close`, `high`, `low` |
| `volume` | Per (exchange, asset) traded volume | `volume_sum`, `notional`, `buy_volume`, `sell_volume`, `trade_count` |
| `flow` | Per-contract flow | `exchange`, `asset`, `optionType`, `expiry`, `strike`, `side`, `volume_sum`, `trade_count`, `avg_iv` |

Common cols: `row_type`, `window`, `exchange`, `asset`, `window_start`
(Unix ms), `at` (Unix ms, window end); `flow` rows also carry surface
fields (`markIV_close`, `delta`, `openInterest`, `underlying_price`).

**Read pattern (DuckDB):**

```sql
INSTALL httpfs; LOAD httpfs;

-- DVOL + spot OHLC over the last hour
SELECT exchange, asset, metric, open, close, high, low
FROM read_parquet('s3://terminal-dime-prod/paradigm_data/realtime/hot/hot__1h.parquet')
WHERE row_type = 'dvol_spot';

-- Volume by venue over the last 24h
SELECT exchange, asset, volume_sum, notional, buy_volume, sell_volume, trade_count
FROM read_parquet('s3://terminal-dime-prod/paradigm_data/realtime/hot/hot__24h.parquet')
WHERE row_type = 'volume';
```

**When to use:** a "last `<window>`" question (volume / flow / DVOL move
over 5m–24h) — one S3 read of the matching `hot__<window>` file instead
of multiple API round-trips. For anything beyond 24h, use the historical
per-period datasets above.

---

## What Is NOT Here

- **Deribit option quotes beyond 2026-01-01** — only trades are consistently available.
- **OKX combo quotes** — not present.
- **OKX option quotes** — not present in this catalog.
- **Greeks / IV in the option/future feed data** — not present; either compute them from
  trades/underlying, or use Bullish option chain snapshots (Dataset 3a) where
  coverage overlaps.
- **Paradex options data** — Paradex options are everlasting/perpetual style
  with no expiry date and are excluded from this catalog. Paradex options
  *block-trade* flow is visible in the Paradigm trade tape under
  `BTC OPTION - PRDX`. Paradex *perp* trade flow is in Dataset 5.
- **Live exchange streams / raw orderbook / account state** — for raw
  exchange tick streams, live order books, and account state, route to
  the live-data skills. Dataset 6 (hot pulse) is this
  catalog's only near-real-time entry and serves the "what's happening
  right now" use case at 1-minute grain.

---

## Cross-Dataset Notes

- **Timestamp units:**
  - Option/future market data (`timestamp`, `local_timestamp`): **microseconds** since epoch →
    `to_timestamp(timestamp / 1e6)` in DuckDB.
  - Paradigm tapes: split `DATE` + `TIME` columns.
  - Bullish, IBIT, Paradex tape: native `timestamp` types (seconds resolution).
- **Deribit option price units:** **index currency** (BTC for BTC options,
  ETH for ETH options), not USD.
- **Deribit amount units:** contracts (1 BTC contract = 1 BTC notional;
  1 ETH contract = 1 ETH notional).
- **IBIT amount units:** US-equity-option contracts (1 contract = 100 shares).
- **Joining Paradigm + market data:** Use `RFQ_ID` / `BLOCK_TRADE_ID` on the
  Paradigm side; join to the option/future market data by symbol + timestamp
  window for market context. For Bullish or IBIT cross-references, join on
  symbol + snapshot time.
- **Paradigm options filter:** `WHERE PRODUCT LIKE '%OPTION%'`.
- **Paradigm exchange suffix:** `DBT` = Deribit, `PRDX` = Paradex,
  `BYB` = Bybit.
- **Greeks/IV preference:** Bullish chain snapshots (Dataset 3a) are the
  only source of native greeks/IV. Where coverage overlaps with the
  option/future market data, prefer Bullish for delta/gamma/theta/vega/IV
  rather than computing.
- **Hive-style partitions:** IBIT, Bullish chain, Bullish orderbook use
  `date=YYYY-MM-DD/` prefixes (different from the daily `YYYY/MM/DD/`
  partition used for option/future market data). The coverage-probe regex
  in `SKILL.md` Step 3 only matches the daily layout — for Hive-partitioned
  datasets, use:

  ```sql
  SELECT
    MIN(regexp_extract(file, 'date=(\d{4}-\d{2}-\d{2})', 1)) AS earliest,
    MAX(regexp_extract(file, 'date=(\d{4}-\d{2}-\d{2})', 1)) AS latest,
    COUNT(*) AS file_count
  FROM glob('<path-with-**>');
  ```
