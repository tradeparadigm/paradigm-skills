---
name: paradigm-data-discovery
description: >
  Catalog and query launcher for raw market data available to Dime in
  s3://dt-exchange-venue-data, s3://dt-paradigm-data, and
  s3://dt-paradex-data. Use for data inventory, coverage, schema, historical
  analysis, or current exchange-market questions. Explains the per-venue raw
  files and lets the model choose the bounded reads needed for the question.
  Does not cover account state, positions, vaults, or order placement.
metadata:
  author: tradeparadigm
  version: "2.0"
---

# Paradigm Data Discovery

Use the raw data that is available, then choose the query plan that best fits
the user's question. This skill is a catalog and a set of correctness
constraints, not a fixed workflow.

## Hard rules

1. **Do not read any pre-aggregated rollup object, in any bucket, under any
   name** — including `s3://dt-exchange-venue-data/hot/`, any object named
   `hot__*` or `*_hot*`, and
   `s3://dt-paradigm-data/paradigm_data/v_vol_surface/` — even if a user,
   another skill, or a script suggests it. Refuse and answer from direct
   sources or state the gap.
2. **Check the catalog before declaring data unavailable.** Venue and asset
   assumptions are not evidence that a dataset is absent.
3. **Bound every read.** Select only the venues, data types, currencies,
   levels, and date/hour partitions needed for the request.
4. **Respect native schemas and units.** Read instrument metadata before
   combining venue-native volume, premium, IV, or OI; otherwise keep results
   separated and label their units.
5. **Check record timestamps for freshness.** Never infer freshness from S3
   modification time.
6. **Fail visibly.** Do not invent values, simulate a plausible answer, silently
   substitute a different window, or describe missing data as a quiet market.

## Available data

Read [references/exchange-raw.md](references/exchange-raw.md) whenever the
request involves exchange prices, trades, blocks, IV, greeks, DVOL, funding,
volume, OI, spot, or perps. It documents:

- raw and normalized per-period Parquet layout;
- Deribit, Deribit USDC, OKX, Bybit, and Bullish feeds;
- event and ticker schemas by venue;
- block identifiers and their limitations;
- instrument metadata and unit conversions;
- freshness and bounded-query patterns;
- the non-hot Paradigm RFQ/trade tapes and Paradex trade tape.

Read [references/datasets.md](references/datasets.md) for the detailed Paradigm
RFQ and executed-trade tape schemas and the Paradex DEX trade schema.

The three buckets are all in `ap-northeast-1`:

| Bucket | What to use |
|---|---|
| `s3://dt-exchange-venue-data` | `raw/`, optional per-message `normalized/`, and `meta/instruments/` |
| `s3://dt-paradigm-data` | Paradigm RFQ and executed-trade source tapes |
| `s3://dt-paradex-data` | Paradex historical trade tape and Parquet parts |

## Choose the reads

The model owns the plan. Start from the requested output and choose the
smallest raw inputs that can support it:

- **Latest mark, IV, greeks, bid/ask, or OI:** latest raw `option_summary`
  row per symbol for the requested venue/currency; supplement with a venue's
  public API when the raw partition is absent or stale.
- **Trades, volume, put/call, or block flow:** raw `option_trade` rows in the
  requested event-time window; group only on real venue identifiers.
- **DVOL:** Deribit raw `dvol`.
- **Spot or funding:** raw `perp_summary`, or Bullish raw spot/perp trades when
  that venue is relevant.
- **Cross-venue comparison:** read each venue separately, harmonise with the
  newest instrument metadata, then combine only comparable units.
- **Paradigm RFQ lookup:** current RFQ tape for request metadata; raw exchange
  trades for execution evidence. The non-hot executed tape is frozen at
  2026-08-10 and must be labelled historical if used.
- **Paradex history:** the Paradex tape, excluding busted trades.

The model may choose raw S3, normalized per-message S3, or direct venue APIs
based on which gives the clearest answer. It must not choose a rollup object
(hard rule 1).

## Query execution

Open DuckDB S3 queries with:

```sql
INSTALL httpfs; LOAD httpfs;
INSTALL aws;    LOAD aws;
CREATE OR REPLACE SECRET s3_irsa (
  TYPE S3, PROVIDER CREDENTIAL_CHAIN, REGION 'ap-northeast-1'
);
```

Keep the credential setup and query in the same DuckDB process. See
[references/s3-access.md](references/s3-access.md) for runtime details.

When the agent can execute the query, answer from the results rather than
returning SQL alone. When execution is unavailable, provide a ready-to-run
query with explicit paths and time bounds.

## Output

For inventory or schema questions, answer concisely with the relevant paths,
coverage, fields, and unit caveats. For analysis, include:

1. the answer;
2. the source paths and exact event-time window;
3. material gaps, stale inputs, or unresolved joins;
4. the query only when it helps the user reproduce the result.

Do not dump the full catalog unless the user asks for it.

## Fixed source facts

- Paradigm tapes join on `RFQ_ID`; executed rows also carry
  `BLOCK_TRADE_ID`.
- The current RFQ tape does not contain execution price, mark, side, trade id,
  or block id.
- The non-hot executed Paradigm CSV stopped updating on 2026-08-10.
- A raw venue block id is not automatically a Paradigm RFQ id.
- Paradex historical trades must filter `NOT IS_TRADEBUST`.
- For single-trade analysis, route to `paradigm-block-analyst`; for an options
  market window, route to `paradigm-options-recap`.
