---
name: paradigm-data-discovery
description: >
  Catalog and query-launcher for market data in S3 (the dt-* buckets:
  dt-exchange-venue-data, dt-paradigm-data, dt-paradex-data) — historical
  tapes plus the near-real-time hot surface. ALWAYS load this skill before
  concluding a dataset is out of scope — don't dismiss on asset-class or
  venue assumptions. Covers: the exchange venue data pipeline
  (Deribit/OKX/Bybit/Bullish options + perps + spot, raw + normalized
  substrate) and its hot surface (live 1-min snapshot of spot, ATM IV,
  DVOL, funding, volume, blocks, per-stream freshness, plus
  trailing-window recaps and a point-in-time vol surface); Paradigm RFQ
  block-trade + activity tapes; Bullish option chain snapshots; IBIT ETF
  options trades (DO NOT dismiss — equity-side vol, in these buckets); and
  the Paradex perp trade tape. Fires for retrospective "what data do we
  have" AND live "what's happening now" questions answerable from it —
  returns an S3 path + ready-to-run DuckDB query. Does NOT cover live
  Paradex markets, positions, vaults, or order placement.
compatibility: Read-only data catalog. No authentication required to view the
  catalog itself. Running the suggested DuckDB/S3 queries requires IRSA
  credentials (AWS_WEB_IDENTITY_TOKEN_FILE, AWS_ROLE_ARN) — see
  references/s3-access.md for the credential bootstrap.
metadata:
  author: tradeparadigm
  version: "2.0"
---

## Hard Rules

1. **Never dismiss a data query without reading this skill first.**
   Domain assumptions ("IBIT is TradFi", "that's not a Paradigm product",
   "that venue isn't supported") are NOT a valid substitute for checking
   the catalog. Every dataset family lives under the `dt-*` buckets
   (`dt-exchange-venue-data`, `dt-paradigm-data`, `dt-paradex-data`)
   regardless of the instrument's native venue or asset class.
2. **IBIT is in scope.**
   `s3://dt-paradigm-data/paradigm_data/ibit_options_trades/` contains
   IBIT ETF option trades. Used for equity-side BTC vol
   cross-referencing. Always surface it when the user asks about IBIT,
   ETF options, or equity vol vs crypto vol comparisons.
3. **Default to this skill for any "what data / latest data / do we have X"
   question.** Even if the answer turns out to be "not in catalog," the
   correct response is to load this skill, check, and report — not to
   assume absence based on prior knowledge.

# Paradigm Data Discovery

Reference catalog **and entry-point** for the S3-backed datasets the
agent can query through DuckDB. Scope: the `dt-*` buckets — the exchange
venue data pipeline + its hot surface (`dt-exchange-venue-data`),
Paradigm RFQ tapes + Bullish option chain + IBIT (`dt-paradigm-data`),
and the on-chain Paradex perp trade tape (`dt-paradex-data`).

Two jobs:

1. **Catalog** — answer "which historical dataset do I need, where does it
   live, what's in it?" without globbing the bucket.
2. **Query launcher** — when the user asks a *retrospective* question that
   the catalog can answer (biggest trades in a window, volume by venue,
   structure mix over time, IBIT vs crypto vol, Bullish chain on a date),
   surface the path **and** a ready-to-run DuckDB query so the user (or
   downstream query runner) can execute it. Crucially: never reply
   "I don't have access to historical block trade data" — the tapes on S3
   *are* the historical data.

## Scope — S3-backed market data (historical + near-real-time hot surface)

In scope: anything under the `dt-*` buckets — the exchange venue data
substrate (Deribit/OKX/Bybit/Bullish real-time options + perps + spot,
raw + normalized), Bullish option chain snapshots + orderbook history,
Paradigm block-trade + RFQ tapes, IBIT ETF option trades, the historical
Paradex perp trade tape
(`s3://dt-paradex-data/paradex_data/paradex_trade_tape.csv.gz`, bucket
TBC), and the near-real-time **hot surface**
(`s3://dt-exchange-venue-data/hot/hot__market_signals_1m.parquet` +
`hot__recap_<window>` + `hot__vol_surface`).

Out of scope: anything **live** that isn't in the hot surface — live Paradex
markets, positions, funding, vaults, raw orderbook, order placement,
account state. Those belong to live trading/market tooling.

Rule of thumb:

- If the user's question is anchored to a past date or date range, or
  asks about a tape / snapshot / historical aggregate → historical
  datasets (1–5).
- If the user asks "what's happening right now" / "current ATM IV" /
  "spot move in the last minute" / "DVOL right now" / "any blocks just
  printed" → reach for **Dataset 6 (hot surface)** first. One S3 read
  replaces several `web_fetch` round-trips.
- If the user wants live Paradex account state or order placement →
  stand down (route to live-trading skills).

## Trigger

Fire when the user is asking about, or implicitly needs, any of the
historical S3-backed datasets — Paradigm tapes, option/future market data,
Bullish, IBIT, or the Paradex DEX historical trade tape. Four trigger
families:

**(A) Catalog questions** — explicit "what data / where / what columns /
what coverage":

- "What Paradigm / S3 / DuckDB data do we have?"
- "Where does the <Paradigm tape | exchange option data | hot surface |
  Paradex trade tape> live in S3?"
- "What columns does the <Paradigm trade tape | RFQ tape | hot snapshot |
  recap window | vol surface | Paradex trade tape | IBIT | Bullish chain>
  have?"
- "What's the date range / latest data for <exchange option_summary |
  Paradex trade tape | IBIT options>?"
- "Do we have <OKX options | Bybit options | Bullish spot | Paradex perp
  trades> in S3?"
- "What's the schema for `hot__market_signals_1m` / the normalized
  `option_summary` agg / `paradex_trade_tape`?"

**(B) Historical Paradigm-flow analysis** — retrospective questions over
a date range / period that the Paradigm tape can answer:

- "What were the biggest RFQs in <month/quarter/date range>?"
- "Rank Paradigm block trades by notional last <period>"
- "Show me <BTC|ETH|SOL|XRP|AVAX> option block trades on <date>"
- "How much Paradigm block volume on Deribit/Paradex/Bybit in <period>?"
- "Most-traded structures (straddles, risk reversals, etc.) in <period>"
- "Top counterparties / largest single trades / unfilled RFQ ratio in <period>"
- "Compare Paradigm flow across DBT/PRDX/BYB for <period>"

**(C) Exchange venue-data analysis** — right-now / recent-window questions
over the live pipeline (options + perps + spot + DVOL + funding):

- "Current ATM IV / DVOL / spot across venues" (hot snapshot)
- "Options volume / flow / biggest blocks in the last <5m–24h>" (hot recap)
- "Full vol surface / skew / term structure right now" (hot__vol_surface)
- "Is any venue's feed stale?" (coverage rows)
- "Most-traded Deribit/OKX/Bybit options over <custom window>" (substrate)

**(D) Paradex DEX historical trade tape** — retrospective questions about
*on-chain Paradex perp* trades that the historical tape can answer:

- "Biggest Paradex perp trades in <period>"
- "Paradex BTC-USD-PERP volume on <date>"
- "Show me Paradex trade tape rows from <date range>"
- "How many trades on Paradex `<MARKET>` last month?"
- "Paradex taker buy vs sell breakdown for <period>"

For (B), (C), and (D), the response **must** include both the S3 path
and a concrete DuckDB query (see Step 5) — don't just describe the
dataset. Filter `WHERE NOT IS_TRADEBUST` on the Paradex tape.

Do **not** fire for:

- Live exchange tickers / mark prices / greeks (use `paradigm-block-analyst`
  or venue-specific skills).
- **Live** Paradex questions — current positions, current funding rate,
  live orderbook, order placement, vault state, margin (those are
  Paradex-live skills, not this catalog). The *historical* Paradex trade
  tape *is* in scope — see family (D) above.
- Generic "what can you do" / "what skills do I have" — that's a meta
  question, not a data catalog question.
- A trade JSON paste asking for analysis of *that single trade* (use
  `paradigm-block-analyst`).

## Step 1 — Identify Intent

| Intent | Action |
|---|---|
| Inventory ("what do we have?") | List dataset families from `references/datasets.md` |
| Lookup ("where is X?") | Return path + schema for that dataset |
| Coverage ("date range for X?") | Return last verified range + glob probe |
| Schema ("columns of X?") | Return column table |
| Gap ("do we have Y?") | Check catalog; if absent, point to "What Is NOT Here" |
| **Historical analysis** ("biggest RFQs in March 2026") | **Pick the dataset, return path + ready-to-run DuckDB query (Step 5)** |
| Routing (pre-query) | Surface 1–2 candidate datasets and prompt for confirmation |

## Step 2 — Surface the Catalog

Pull from `references/datasets.md`. Grouped into:

1. **Exchange Venue Data** (`s3://dt-exchange-venue-data/`) — the live
   Deribit/OKX/Bybit/Bullish pipeline
   - `raw/` + `normalized/` — per-period substrate (option_trade,
     option_summary, perp_trade, spot_trade, dvol, perp_summary), Hive
     layout `exchange=/data_type=/currency=/level=/year=/…`, kind
     `rows`/`agg`
   - `meta/instruments/` — per-venue contract conventions
   - `market_aggregates_5m/` — retained 5m bucket series
2. **Hot Surface** (`s3://dt-exchange-venue-data/hot/`) — LLM-shaped,
   near-real-time
   - `hot__market_signals_1m.parquet` — live snapshot, 7 `signal_type`s
     (spot / atm_iv / dvol / funding / volume_last_min / block_summary /
     coverage); clobbered every 60 s
   - `hot__recap_<window>.parquet` — trailing windows
     (`5m`/`10m`/`20m`/`1h`/`4h`/`8h`/`24h`): DVOL+spot OHLC, volume,
     per-contract flow, per-block flow; `row_type` discriminator;
     refreshed every 5 min
   - `hot__vol_surface.parquet` — point-in-time per-strike vol surface +
     per-expiry summary (split out of the recaps); every 5 min
   - **Every hot row carries `instrument_kind`** (option/perp/spot/index)
3. **Paradigm Block Trade Tape** (`s3://dt-paradigm-data/paradigm_data/`)
   — **live, hourly rewrite, trailing ~6 months**
   - `paradigm_trade_tape_slim` — executed RFQ block trades
   - `paradigm_rfq_tape_slim` — RFQ activity including unfilled
4. **Bullish Options — static historical load**
   (`s3://dt-paradigm-data/paradigm_data/bullish_*`) — one-shot load
   written 2026-05-11, **not refreshing**
   - `bullish_option_chain_snapshots` — chain with **native greeks + IV**
     (ends 2026-05-09; distinct from the Dataset 1 bullish spot/perp feed)
   - plus static option trades / orderbook / spot siblings (see catalog)
5. **IBIT ETF Options Trades**
   (`s3://dt-paradigm-data/paradigm_data/ibit_options_trades/`) —
   equity-side vol cross-reference; **stalled, data ends 2026-06-01**
6. **Paradex DEX Trade Tape** (`s3://dt-paradex-data/paradex_data/`,
   **bucket TBC**)
   - `paradex_trade_tape.csv.gz` — on-chain Paradex perp trades
     (historical only; live Paradex state is out of scope)

For each, report: S3 path (with the correct pattern — the Hive
`exchange=/…/level=/YYYY/MM/DD/` tree for the exchange substrate,
stable clobbered keys for the hot surface, `date=YYYY-MM-DD/` for
Bullish-chain/IBIT, flat file for Paradigm and Paradex tapes), last
verified coverage, schema,
notable filters (e.g. `WHERE PRODUCT LIKE '%OPTION%'` for Paradigm,
`WHERE NOT IS_TRADEBUST` for Paradex tape).

## Step 3 — Always Include Verification Hint

When the user asks about a specific date or recent data, include the glob
date-range probe. Use the regex that matches the dataset's partition
layout:

**Daily-partitioned (`YYYY/MM/DD/`) — exchange venue substrate:**

```sql
SELECT
  MIN(regexp_extract(file, '/(\d{4}/\d{2}/\d{2})/', 1)) AS earliest,
  MAX(regexp_extract(file, '/(\d{4}/\d{2}/\d{2})/', 1)) AS latest,
  COUNT(*) AS file_count
FROM glob('<path-with-**>');
```

**Hive-style (`date=YYYY-MM-DD/`) — Bullish, IBIT:**

```sql
SELECT
  MIN(regexp_extract(file, 'date=(\d{4}-\d{2}-\d{2})', 1)) AS earliest,
  MAX(regexp_extract(file, 'date=(\d{4}-\d{2}-\d{2})', 1)) AS latest,
  COUNT(*) AS file_count
FROM glob('<hive-path-with-**>');
```

…so they can confirm latest availability before concluding data is missing.

## Step 4 — Output Format

1. **Direct answer** — name the dataset(s) that fit, in one or two sentences.
2. **Path + coverage** — S3 URI, last verified date range, partitioning.
3. **Schema** — column table only if user asked for columns or is about to
   query (omit for pure inventory questions).
4. **Caveats** — coverage gaps, unit quirks, join keys.
5. **Next step** — verification glob query, or for historical-analysis
   intent, a ready-to-run DuckDB query (Step 5).

## Step 5 — Ready-to-Run DuckDB Query (for historical-analysis intent)

When the user's question is analytical and answerable from the catalog,
always include a runnable DuckDB query. Pattern:

**Paradigm tape — biggest RFQ block trades in a window:**

```sql
-- Credential bootstrap assumed (see references/s3-access.md)
INSTALL httpfs; LOAD httpfs;

SELECT
  DATE, TIME, PRODUCT, DESCRIPTION, QTY, PRICE,
  NOTIONAL_VOLUME_USD, SIDE, RFQ_ID
FROM read_csv_auto('s3://dt-paradigm-data/paradigm_data/paradigm_trade_tape_slim.csv.gz')
WHERE DATE BETWEEN DATE '2026-03-01' AND DATE '2026-03-31'
  AND PRODUCT LIKE '%OPTION%'   -- or drop this filter for all products
ORDER BY NOTIONAL_VOLUME_USD DESC
LIMIT 25;
```

**Paradex DEX historical trade tape — biggest trades by notional in a window:**

```sql
INSTALL httpfs; LOAD httpfs;

SELECT
  TRADE_AT, MARKET, PRICE, SIZE, TAKER_SIDE,
  PRICE * SIZE AS NOTIONAL_USD
FROM read_csv_auto('s3://dt-paradex-data/paradex_data/paradex_trade_tape.csv.gz')  -- bucket TBC
WHERE NOT IS_TRADEBUST
  AND TRADE_AT >= TIMESTAMP '2026-04-01'
  AND TRADE_AT <  TIMESTAMP '2026-05-01'
  -- AND MARKET = 'BTC-USD-PERP'   -- optional: filter to a single market
ORDER BY NOTIONAL_USD DESC
LIMIT 25;
```

Query-template guidelines:

- Always include `INSTALL httpfs; LOAD httpfs;` at top.
- For **Paradigm** tape questions, use `paradigm_trade_tape_slim.csv.gz` for
  *executed* block trades; use `paradigm_rfq_tape_slim.csv.gz` if they want
  RFQ-level stats (fill rate, unfilled, lifespan).
- For **Paradex DEX** tape questions, use
  `paradex_data/paradex_trade_tape.csv.gz`. **Always filter
  `WHERE NOT IS_TRADEBUST`**. Compute notional as `PRICE * SIZE` — there
  is no precomputed USD notional column.
- For **live / recent exchange** questions (right-now or last-window),
  reach for the **hot surface** first (Dataset 2) — one read of
  `hot__market_signals_1m` / `hot__recap_<window>` / `hot__vol_surface`.
  Filter on `instrument_kind` to separate options from perp/spot/index.
- For **deeper / custom-window exchange** questions, glob the
  `dt-exchange-venue-data` substrate over the Hive
  `exchange=/data_type=/currency=/level=/YYYY/MM/DD/` tree; the
  `normalized` `agg` files are the cross-venue aggregates.
- For **Bullish chain greeks/IV** questions, use the Dataset 4 chain
  snapshots (distinct from the Dataset 1 bullish spot/perp feed).
- For **IBIT** questions, expect the equity calendar (no weekends).
- Filter by `PRODUCT LIKE '%OPTION%'` only if the user specifically asked
  about options; otherwise leave it open so perps/futures are included.
- Use `NOTIONAL_VOLUME_USD` (Paradigm) or `PRICE * SIZE` (Paradex DEX) for
  "biggest" / "largest" / ranking queries.
- Use the exchange suffix in Paradigm `PRODUCT` (`- DBT`, `- PRDX`,
  `- BYB`) to filter by venue. For the Paradex DEX tape, filter by `MARKET`.

If the agent has a DuckDB-execution tool available, hand the query off to
it and present the results. If not, return the query with a note that
the user can run it themselves once IRSA credentials are loaded.

Keep responses short for inventory questions; for analysis-intent
questions, give the path + query + a one-line interpretation.

## Notes

- **Buckets:** `s3://dt-exchange-venue-data` (exchange data + hot),
  `s3://dt-paradigm-data` (Paradigm tapes, Bullish chain, IBIT),
  `s3://dt-paradex-data` (Paradex DEX tape, **bucket TBC**). Region
  assumed `ap-northeast-1` — **verify per bucket** and `SET s3_region`
  accordingly.
- **Auth:** IRSA (web identity → STS AssumeRoleWithWebIdentity) — see
  `references/s3-access.md`. Tokens expire ~1 hour; refresh on
  HTTP 400 `InvalidToken`.
- **DuckDB:** `INSTALL httpfs; LOAD httpfs;` every new session.
- **Unit gotchas to flag when relevant:**
  - Hot-surface `at` / `window_start` are epoch **ms**
    (`to_timestamp(at / 1000)`); each row also has an `_iso` string.
  - The hot surface is harmonized — notional USD, volume coin, IV vol
    points. The `dt-exchange-venue-data` substrate is closer to
    venue-native (deribit prices in index currency, OKX size in
    contracts) — prefer the hot surface for comparable numbers.
  - `instrument_kind` (option/perp/spot/index) marks options vs not on
    every hot row — use it, don't infer.
- **Join keys across Paradigm tapes:** `RFQ_ID`, `BLOCK_TRADE_ID`.
- **Paradigm exchange suffixes:** `DBT` = Deribit, `PRDX` = Paradex,
  `BYB` = Bybit.
- **What is NOT here** (call out when asked): the legacy Tardis.dev CSV
  exchange feed (`external/tardis/v1/` combo/future quotes) is **gone** —
  superseded by the live exchange venue data; Bullish **options** are not
  on the exchange feed yet (spot/perp only — use the Bullish chain
  snapshots for greeks/IV); Bybit blocks can't be de-legged (no group id);
  Paradex options excluded (everlasting/perpetual style).
- This skill is a catalog and query-launcher. For analysis of a single
  pasted trade JSON, hand off to `paradigm-block-analyst`. For execution of
  the SQL queries this skill emits, use whatever DuckDB tool the agent has
  available.
