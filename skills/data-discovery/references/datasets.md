# Available Market Data — S3 Catalog

Comprehensive map of market data accessible via DuckDB + S3. Covers crypto
options (Paradigm RFQ flow) and the on-chain Paradex perp trade tape.

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
- **`s3://dt-paradex-data`** — the on-chain Paradex DEX trade tape
  (`paradex_data/paradex_trade_tape.csv.gz` + Parquet parts).

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
- Iron Butterfly: `IFly 26 Jun 26 75000/85000/95000`
- Put Butterfly: `PFly 27 Mar 26 60000/50000/40000`
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

## Dataset 2 — Paradex DEX Trade Tape

On-chain Paradex perpetual trades — the *executed-trade* tape of the
Paradex DEX. This is **historical Paradex trade data**; live Paradex markets,
positions, orderbook, funding, and account data remain out of scope — those
belong to live trading/market tooling, not this historical catalog.

- **Path:** `s3://dt-paradex-data/paradex_data/paradex_trade_tape.csv.gz`
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

## Dataset 3 — Hot Surface (Live Snapshot + Trailing Windows)

Single-file LLM-shaped market snapshot. The fastest way to answer
"what's happening right now" without scanning per-period data —
clobbered every 60 seconds at a stable key. Use this **before** any
exchange `web_fetch` for spot price, ATM IV, DVOL, last-minute volume,
or live block-trade activity.

This is the only dataset in this catalog that is **near-real-time**
(1-minute cadence). All other datasets are historical-only.

### 3a. `hot__market_signals_1m` — Live Market Heartbeat

- **Path:** `s3://dt-exchange-venue-data/hot/hot__market_signals_1m.parquet`
- **Refresh:** every 60 s (clobbered in place; bucket versioning retains
  prior snapshots recoverably for ~24 h)
- **Row count:** ~50–70 (bounded by design; fits in any LLM context)
- **Schema:** ~30 cols, polymorphic via `signal_type`; most columns are
  populated only on the rows that need them (see per-type notes)

Common columns (every row): `signal_type`, `exchange`, `value`, `unit`
(**unit of `value`, carried explicitly**), `delta_1m`, `at` (Unix **ms**),
`at_iso` (ISO-8601), `generated_at` (Unix ms), `instrument_kind`.

| Column | Type | Notes |
|---|---|---|
| `signal_type` | varchar | Discriminator: `spot` \| `atm_iv` \| `dvol` \| `funding` \| `volume_last_min` \| `coverage` |
| `exchange` | varchar | Source venue (`deribit`, `deribit-usdc`, `okex-options`, `bybit-options`, `bullish`) |
| `asset` | varchar | e.g. `BTC`, `ETH` (null on `coverage` rows) |
| `instrument_kind` | varchar | `spot` \| `option` \| `index` \| `perp` |
| `unit` | varchar | Unit of `value` — **read it, don't infer per venue** |
| `expiry`, `atm_call_iv`, `atm_put_iv`, `atm_strike`, `underlying_price`, `open_interest` | | `atm_iv` rows only |
| `underlying_price`, `funding_8h`, `mark_price`, `perp_open_interest`, `symbol` | | `funding` rows only |
| `call_volume`, `put_volume`, `buy_volume`, `sell_volume`, `notional_usd`, `trade_count` | | `volume_last_min` rows only |
| `data_type`, `stream_kind` | varchar | `coverage` rows only (which upstream stream; `continuous`\|`intermittent`) |

**`value` semantics per signal_type (unit is in the `unit` column):**

| `signal_type` | `value` is… | `unit` |
|---|---|---|
| `spot` | Spot/perp close price | `usd` |
| `atm_iv` | ATM strike mark IV (per expiry) | `vol_points` (venue decimal IVs pre-scaled ×100) |
| `dvol` | Latest DVOL index value | `vol_points` |
| `funding` | Current perp funding rate (8h rate in `funding_8h`) | `rate` |
| `volume_last_min` | Total volume in the minute (USD in `notional_usd`) | `coin` |
| `coverage` | **Stream lag — seconds the feed is behind now** (freshness) | `seconds_behind` |

> **`block_summary` is gone** from this file — per-block flow now lives only in
> the recap aggregates (Dataset 3b, `row_type = 'block'`).

**Read pattern (DuckDB):**

```sql
INSTALL httpfs; LOAD httpfs;

-- FRESHNESS FIRST: coverage.value = seconds each stream is behind now.
-- More than a couple of minutes behind = stale; fall back to a live read.
SELECT exchange, data_type, stream_kind, value AS seconds_behind
FROM read_parquet('s3://dt-exchange-venue-data/hot/hot__market_signals_1m.parquet')
WHERE signal_type = 'coverage'
ORDER BY value DESC;

-- What's BTC ATM IV across venues right now?
SELECT exchange, expiry, atm_strike, value AS atm_iv_vol_points,
       atm_call_iv, atm_put_iv, open_interest, delta_1m
FROM read_parquet('s3://dt-exchange-venue-data/hot/hot__market_signals_1m.parquet')
WHERE signal_type = 'atm_iv' AND asset = 'BTC'
ORDER BY expiry;

-- What just printed in the last minute? (value = coin volume; notional_usd = USD)
SELECT exchange, asset, value AS total_volume,
       call_volume, put_volume, buy_volume, sell_volume,
       notional_usd, trade_count
FROM read_parquet('s3://dt-exchange-venue-data/hot/hot__market_signals_1m.parquet')
WHERE signal_type = 'volume_last_min';
```

**When to use:** front-month ATM IV across venues, current spot, DVOL,
perp funding, last-minute volume + call/put split, and a feed-freshness
check (`coverage`) — any "right now" question. Reach for the snapshot
**before** doing exchange `web_fetch` for the same data; one S3 read
replaces several round-trips. (Per-block flow lives in Dataset 3b.)

**Caveats:**

- **Check freshness first.** The `coverage` rows carry `seconds_behind` per
  upstream stream — if the feed is behind, fall back to a live venue read.
  `at`/`at_iso` give the snapshot time (`at` is Unix ms →
  `to_timestamp(at / 1000)`).
- **Units are explicit — read the `unit` column, don't infer.** IV is
  `vol_points` (venue decimal IVs pre-scaled ×100), spot `usd`, volume `coin`
  with USD alongside in `notional_usd`.
- `expiry` is venue-native (`20JUN26` from Deribit, `260620` from OKX).
  Parse per-venue for canonical dates.
- The snapshot does **not** carry the full vol surface — only ATM. For
  the full surface across strikes/expiries, read the consolidated
  single-GET file
  `s3://dt-paradigm-data/paradigm_data/v_vol_surface/_hot.parquet`
  (stable key, refreshed ~5 min; per-strike `mark_iv`/`delta` keyed by
  instrument `symbol`), plus its hourly cold partitions
  `.../v_vol_surface/base=<ASSET>/year=/month=/day=/hour=/` for historical
  window-open snapshots. (The recap aggregates file, Dataset 3b, no longer
  carries `surface` rows.)

### 3b. Hot Recap Aggregates — 5-min Rolling Window Source

A single rolling file of **5-minute aggregate buckets over the trailing
24h** — the query-time source for "last `<window>`" recaps of an arbitrary
length. The per-window `hot__recap_<window>` files (`5m`/`10m`/`20m`/`1h`/
`4h`/`8h`/`24h`) **also still exist** and are refreshed each cycle (the
`/recap` skill reads those pre-baked windows directly); use this aggregates
file when you need a window the pre-baked set doesn't cover.

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
`surface`** (the vol surface lives in `v_vol_surface`; see the Dataset 3a note):

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
  `s3://dt-paradigm-data/paradigm_data/v_vol_surface/` (Dataset 3a note).

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
beyond 24h, use the historical tapes above (Paradigm, Paradex).

---

## What Is NOT Here

- **Raw per-exchange option/future feeds** — not in the catalog. For a live
  cross-venue ATM-IV / DVOL read use the hot surface (Dataset 3a); for the
  full per-strike vol surface use `v_vol_surface`.
- **Standalone Greeks / IV** — not in the catalog; the hot surface carries
  ATM IV only.
- **Paradex options data** — Paradex options are everlasting/perpetual style
  with no expiry date and are excluded from this catalog. Paradex options
  *block-trade* flow is visible in the Paradigm trade tape under
  `BTC OPTION - PRDX`. Paradex *perp* trade flow is in Dataset 2.
- **Live exchange streams / raw orderbook / account state** — for raw
  exchange tick streams, live order books, and account state, route to
  the live-data skills. Dataset 3 (hot surface) is this
  catalog's only near-real-time entry and serves the "what's happening
  right now" use case at 1-minute grain.

---

## Cross-Dataset Notes

- **Timestamp units:**
  - Paradigm tapes: split `DATE` + `TIME` columns.
  - Paradex tape: native `timestamp` types (seconds resolution).
  - Hot surface: `at` is Unix ms (`at_iso` gives the ISO string).
- **Joining Paradigm tapes:** `RFQ_ID` / `BLOCK_TRADE_ID` link the executed
  trade tape to the RFQ activity tape.
- **Paradigm options filter:** `WHERE PRODUCT LIKE '%OPTION%'`.
- **Paradigm exchange suffix:** `DBT` = Deribit, `PRDX` = Paradex,
  `BYB` = Bybit.
