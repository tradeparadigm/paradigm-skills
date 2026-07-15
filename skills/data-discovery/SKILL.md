---
name: paradigm-data-discovery
description: >
  Catalog and query-launcher for market data in S3
  (s3://dt-paradigm-data, s3://dt-exchange-venue-data, s3://dt-paradex-data) — both historical and near-real-time
  hot surface. ALWAYS load this skill before concluding any dataset is
  out of scope — do not dismiss based on asset class or venue
  assumptions. Covers: Paradigm RFQ block-trade tape, Paradigm RFQ
  activity tape, on-chain Paradex perp historical trade tape,
  and the hot surface (live 1-minute snapshot of
  spot, ATM IV, DVOL, last-minute volume, and block-trade activity,
  plus trailing-window recaps). Fires for any
  retrospective "what data do we have" question AND for "what's
  happening right now" questions answerable from it — returns S3
  path + ready-to-run DuckDB query. Does NOT cover live Paradex
  markets, positions, vaults, or order placement.
compatibility: Read-only data catalog. No authentication required to view the
  catalog itself. Running the suggested DuckDB/S3 queries requires IRSA
  credentials (AWS_WEB_IDENTITY_TOKEN_FILE, AWS_ROLE_ARN) — see
  references/s3-access.md for the credential bootstrap.
metadata:
  author: tradeparadigm
  version: "1.5"
---

## Hard Rules

1. **Never dismiss a data query without reading this skill first.**
   Domain assumptions ("that's not a Paradigm product", "that venue isn't
   supported") are NOT a valid substitute for checking the catalog. The
   dataset families live in S3 — across `s3://dt-paradigm-data`,
   `s3://dt-exchange-venue-data`, and `s3://dt-paradex-data`
   — regardless of the instrument's native venue or asset class.
2. **Default to this skill for any "what data / latest data / do we have X"
   question.** Even if the answer turns out to be "not in catalog," the
   correct response is to load this skill, check, and report — not to
   assume absence based on prior knowledge.

# Paradigm Data Discovery

Reference catalog **and entry-point** for historical S3-backed datasets the
agent can query through DuckDB. Scope: the market-data buckets
`s3://dt-paradigm-data`, `s3://dt-exchange-venue-data`, and
`s3://dt-paradex-data` — the Paradigm RFQ tapes, the on-chain Paradex
perp trade tape, and the near-real-time hot surface.

Two jobs:

1. **Catalog** — answer "which historical dataset do I need, where does it
   live, what's in it?" without globbing the bucket.
2. **Query launcher** — when the user asks a *retrospective* question that
   the catalog can answer (biggest trades in a window, volume by venue,
   structure mix over time, most-traded options on a date),
   surface the path **and** a ready-to-run DuckDB query so the user (or
   downstream query runner) can execute it. Crucially: never reply
   "I don't have access to historical block trade data" — the tapes on S3
   *are* the historical data.

## Scope — S3-backed market data (historical + near-real-time hot surface)

In scope: anything in the market-data buckets (`s3://dt-paradigm-data`,
`s3://dt-exchange-venue-data`, and `s3://dt-paradex-data`) —
Paradigm block-trade tapes, the on-chain Paradex perp trade tape, and the
near-real-time **hot surface** (`s3://dt-exchange-venue-data/hot/hot__market_signals_1m.parquet`).

Out of scope: anything **live** that isn't in the hot surface — live Paradex
markets, positions, funding, vaults, raw orderbook, order placement,
account state. Those belong to live trading/market tooling.

Rule of thumb:

- If the user's question is anchored to a past date or date range, or
  asks about a tape / snapshot / historical aggregate → historical
  datasets (1–2).
- If the user asks "what's happening right now" / "current ATM IV" /
  "spot move in the last minute" / "DVOL right now" / "any blocks just
  printed" → reach for **Dataset 3 (hot surface)** first. One S3 read
  replaces several `web_fetch` round-trips.
- If the user wants live Paradex account state or order placement →
  stand down (route to live-trading skills).

## Trigger

Fire when the user is asking about, or implicitly needs, any of the
historical S3-backed datasets — the Paradigm tapes or the Paradex DEX
historical trade tape. Three trigger families:

**(A) Catalog questions** — explicit "what data / where / what columns /
what coverage":

- "What Paradigm / S3 / DuckDB data do we have?"
- "Where does the <Paradigm tape | Paradex trade tape | hot surface> live in S3?"
- "What columns does the <Paradigm trade tape | RFQ tape | Paradex trade tape> have?"
- "What's the date range for <Paradigm tape | Paradex trade tape>?"
- "Do we have <Paradex perp trades | a given Paradigm product> in S3?"
- "What's the schema for `paradigm_trade_tape_slim` / `paradex_trade_tape`?"

**(B) Historical Paradigm-flow analysis** — retrospective questions over
a date range / period that the Paradigm tape can answer:

- "What were the biggest RFQs in <month/quarter/date range>?"
- "Rank Paradigm block trades by notional last <period>"
- "Show me <BTC|ETH|SOL|XRP|AVAX> option block trades on <date>"
- "How much Paradigm block volume on Deribit/Paradex/Bybit in <period>?"
- "Most-traded structures (straddles, risk reversals, etc.) in <period>"
- "Top counterparties / largest single trades / unfilled RFQ ratio in <period>"
- "Compare Paradigm flow across DBT/PRDX/BYB for <period>"

**(C) Paradex DEX historical trade tape** — retrospective questions about
*on-chain Paradex perp* trades that the historical tape can answer:

- "Biggest Paradex perp trades in <period>"
- "Paradex BTC-USD-PERP volume on <date>"
- "Show me Paradex trade tape rows from <date range>"
- "How many trades on Paradex `<MARKET>` last month?"
- "Paradex taker buy vs sell breakdown for <period>"

For (B) and (C), the response **must** include both the S3 path
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

1. **Paradigm Block Trade Tape** (`s3://dt-paradigm-data/paradigm_data/`)
   - `paradigm_trade_tape_slim` — executed RFQ block trades
   - `paradigm_rfq_tape_slim` — RFQ activity including unfilled
2. **Paradex DEX Trade Tape** (`s3://dt-paradex-data/paradex_data/`)
   - `paradex_trade_tape.csv.gz` — on-chain Paradex perp trades
     (historical only; live Paradex state is out of scope)
3. **Hot Surface** (`s3://dt-exchange-venue-data/hot/`)
   - `hot__market_signals_1m.parquet` — single-file LLM-shaped live
     snapshot (spot / ATM IV / DVOL / funding / 1-min volume / coverage);
     clobbered every 60 s. The catalog's only near-real-time entry.
   - `hot__recap_aggregates_5m_24h.parquet` — a single rolling file of
     5-min aggregate buckets over the trailing 24h (DVOL+spot OHLC, volume
     by venue, per-contract flow, per-block flow); refreshed every ~5 min.
     Apply the window in-query (`WHERE bucket_at >= now - window`). No
     `surface` rows — the vol surface is in `v_vol_surface` on
     `dt-paradigm-data`. See Dataset 3b for the schema and read pattern.

For each, report: S3 path (flat file for the Paradigm and Paradex tapes,
stable clobbered key for the hot surface), last verified coverage, schema,
notable filters (e.g. `WHERE PRODUCT LIKE '%OPTION%'` for Paradigm,
`WHERE NOT IS_TRADEBUST` for Paradex tape).

## Step 3 — Always Include Verification Hint

The catalog's coverage dates are point-in-time; the tapes grow forward. When
the user asks about a specific or recent date, confirm coverage by reading the
date column directly rather than trusting the last-verified range:

```sql
-- Paradigm tape uses DATE; the Paradex tape uses TRADE_AT
SELECT min(DATE) AS earliest, max(DATE) AS latest, count(*) AS rows
FROM read_csv_auto('s3://dt-paradigm-data/paradigm_data/paradigm_trade_tape_slim.csv.gz');
```

The hot surface is clobbered every ~60 s, so it's always current — no probe needed.

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
FROM read_csv_auto('s3://dt-paradex-data/paradex_data/paradex_trade_tape.csv.gz')
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
  `s3://dt-paradex-data/paradex_data/paradex_trade_tape.csv.gz`. **Always
  filter `WHERE NOT IS_TRADEBUST`**; compute notional as `PRICE * SIZE` —
  there is no precomputed USD notional column.
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

- **Buckets** (all region `ap-northeast-1`, same IRSA creds):
  `s3://dt-paradigm-data` (Paradigm tapes + `v_vol_surface`, keeps the
  `paradigm_data/` prefix), `s3://dt-exchange-venue-data` (hot surface +
  recap aggregates, at bucket root), and `s3://dt-paradex-data` (the
  Paradex DEX trade tape, under `paradex_data/`).
- **Auth:** IRSA (web identity → STS AssumeRoleWithWebIdentity) — see
  `references/s3-access.md`. Tokens expire ~1 hour; refresh on
  HTTP 400 `InvalidToken`.
- **DuckDB:** `INSTALL httpfs; LOAD httpfs;` every new session.
- **Unit gotchas to flag when relevant:**
  - Hot surface carries units explicitly in the `unit` column — read it.
  - Paradigm tape: `NOTIONAL_VOLUME_USD` is USD; `QTY` is contracts.
- **Join keys across Paradigm tapes:** `RFQ_ID`, `BLOCK_TRADE_ID`.
- **Paradigm exchange suffixes:** `DBT` = Deribit, `PRDX` = Paradex,
  `BYB` = Bybit.
- **What is NOT here** (call out when asked): raw per-exchange option/future
  feeds, standalone Greeks/IV (the hot surface carries ATM IV only), and
  Paradex options (everlasting/perpetual style).
- This skill is a catalog and query-launcher. For analysis of a single
  pasted trade JSON, hand off to `paradigm-block-analyst`. For execution of
  the SQL queries this skill emits, use whatever DuckDB tool the agent has
  available.
