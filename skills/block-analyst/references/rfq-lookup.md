# Resolve an RFQ by `rfq_id` — Paradigm trade tape via data-discovery

The block analyst's input is `/analyze <rfq_id> <rfq description>`. The `rfq_id`
is the authoritative key. **Resolve it by searching the Paradigm trade tape** —
this file is the complete, self-contained recipe (the IRSA→STS credential
bootstrap is inlined below; do not open `paradigm-data-discovery`'s docs for it)
for turning the `rfq_id` into the full trade record the analysis needs (the same
fields that used to be pasted as JSON).

---

## How to resolve it

### 1. ONE combined tape read — fill row **and** 30d recurrence in a single scan (primary)

The trade tape is a gzipped CSV on S3. Decompressing it is the dominant cost, so
**scan it exactly once**: materialize the relevant rows into a temp table, then
read both the fill row (Step 0) and the 30d structure recurrence (Step 3a) out of
that temp table. **Do not run a second tape query later** — this one covers both.

This recipe is **self-contained**: the IRSA→STS bootstrap is inlined below, so you
do **not** need to open `paradigm-data-discovery`'s `SKILL.md` or `s3-access.md`
first. The **only** token is `<CORE_ID>` (the `r_…` id with any `DRFQv2-`/`GRFQ-` prefix
stripped) — **nothing from the `<rfq description>`**. The `<rfq_id>` is the sole authoritative
input; the description text is user reference only and must not seed the asset, instrument, or
filter. HIST recurrence self-derives from the FILL row's own `PRODUCT` + normalized
`DESCRIPTION`, so the query needs no strike/expiry/asset tokens.

> **Sanitize `<CORE_ID>` before substituting it** — it lands inside a SQL string
> literal on a session holding live S3 credentials. Accept only `[A-Za-z0-9_-]`
> (reject anything else outright), and backslash-escape `_` (a `LIKE` wildcard)
> as the `ESCAPE '\'` clauses below expect, so `r_3Fvz…` matches literally.

Run it as one `exec`:

```bash
# POST with the token read straight from its file keeps it out of argv/ps.
CREDS=$(curl -s --max-time 20 -X POST "https://sts.ap-northeast-1.amazonaws.com/" \
  --data "Action=AssumeRoleWithWebIdentity&Version=2011-06-15&RoleSessionName=duckdb" \
  --data-urlencode "RoleArn=${AWS_ROLE_ARN}" \
  --data-urlencode "WebIdentityToken@${AWS_WEB_IDENTITY_TOKEN_FILE}")
AK=$(echo "$CREDS" | grep -o '<AccessKeyId>[^<]*' | cut -d'>' -f2)
SK=$(echo "$CREDS" | grep -o '<SecretAccessKey>[^<]*' | cut -d'>' -f2)
ST=$(echo "$CREDS" | grep -o '<SessionToken>[^<]*' | cut -d'>' -f2)
duckdb -c "
INSTALL httpfs; LOAD httpfs;
SET s3_region='ap-northeast-1';
SET s3_access_key_id='$AK'; SET s3_secret_access_key='$SK'; SET s3_session_token='$ST';
-- single decompress → temp table holding the target RFQ + 30d matching structures
CREATE TEMP TABLE tape AS
SELECT DATE, TIME, AUCTION, PRODUCT, DESCRIPTION, QTY, PRICE, REF_PRICE, SIDE,
       QUOTE_CURRENCY, NOTIONAL_VOLUME_USD, RFQ_ID, TRADE_ID, BLOCK_TRADE_ID,
       UPPER(REPLACE(DESCRIPTION,' ','')) AS DESC_N
FROM read_csv_auto('s3://terminal-dime-prod/paradigm_data/paradigm_trade_tape_slim.csv.gz')
WHERE RFQ_ID LIKE '%<CORE_ID>%' ESCAPE '\'
   OR DATE >= (CURRENT_DATE - INTERVAL 30 DAY);
-- (a) the cleared block — authoritative for every field. Asset ← PRODUCT (never assume BTC),
-- structure ← DESCRIPTION. Offsets precomputed: OFFSET_BPS (×10000) for COIN-quoted premiums
-- (BTC/ETH); OFFSET_PCT (% of mark) for USD/USDC-quoted premiums (SOL/alts — dollar prices,
-- where ×10000 bps is meaningless). Pick by QUOTE_CURRENCY. Never hand-compute the offset.
SELECT 'FILL' tag, *,
       ROUND(PRICE - REF_PRICE, 6) AS MARK_OFFSET,
       ROUND((PRICE - REF_PRICE) * 10000, 1) AS OFFSET_BPS,
       ROUND((PRICE - REF_PRICE) / NULLIF(REF_PRICE,0) * 100, 1) AS OFFSET_PCT
FROM tape WHERE RFQ_ID LIKE '%<CORE_ID>%' ESCAPE '\';
-- (b) 30d recurrence (Step 3a): same structure = same PRODUCT + same normalized DESCRIPTION as
-- the FILL, self-derived from the FILL row (no user text). Same-coin match blocks cross-asset leaks.
SELECT 'HIST' tag, DATE, TIME, PRODUCT, DESCRIPTION, QTY, PRICE, REF_PRICE, SIDE, BLOCK_TRADE_ID
FROM tape
WHERE PRODUCT IN (SELECT PRODUCT FROM tape WHERE RFQ_ID LIKE '%<CORE_ID>%' ESCAPE '\')
  AND DESC_N  IN (SELECT DESC_N  FROM tape WHERE RFQ_ID LIKE '%<CORE_ID>%' ESCAPE '\')
ORDER BY DATE DESC, TIME DESC;
"
```

> **The tape prefixes ids with a routing tag (e.g. `DRFQv2-`). The `LIKE '%<CORE_ID>%' ESCAPE '\'`
> match is prefix-tolerant by construction — the `r_…` core is the stable key. Never
> conclude "not on tape" from a miss without having run this suffix-tolerant match.**

Notes:
- A whole structure sits on the matched row(s) — `DESCRIPTION` encodes the full
  strategy (e.g. `Straddle 19 Nov 25 3050`, `RRCall 30 Jan 26 70000/108000`,
  `Cstm +1.00 Call 24 Apr 26 78000 -2.00 Call 24 Apr 26 85000`). Rows sharing a
  `BLOCK_TRADE_ID` are one block — keep them together.
- The `HIST` rows ARE the Step 3a Paradigm-recurrence answer (count, sizes, sides,
  most-recent). Cluster them by `BLOCK_TRADE_ID` and match the full leg set —
  count only rows whose `DESCRIPTION` is the *same structure* as the recurrence;
  note the rest as strike-level context, not prints.
- The tape is the **executed** tape. For RFQ-level context (fill rate, unfilled,
  lifespan) the sibling dataset is `paradigm_rfq_tape_slim` (same `RFQ_ID` key).
- **Auth:** the STS block above assumes the IRSA role directly; no external file
  read needed. If the credentials / DuckDB tool are unavailable, fall back below.

**Self-test (regression guard — bare id must resolve a prefixed row):** given a
tape row whose `RFQ_ID` is `DRFQv2-r_01H8XQ…`, the canonical query above invoked
with the **bare** id `r_01H8XQ…` must return that row (the substring
`LIKE '%<CORE_ID>%' ESCAPE '\'` match is prefix-tolerant). If a bare-id lookup
comes back empty on a tape known to carry the prefixed form, the prefix handling
has regressed — fix the match before reporting "not on tape".

### 2. Fallbacks (when the tape can't be queried)

| Source | When | How |
|---|---|---|
| Injected block-trade context | running inside the Dime/terminal session | the terminal attaches the cleared block (e.g. a `set_block_trade_context` feed) — read it directly |
| Deribit public tape | last resort, no Paradigm tape access | reconstruct the block from `block_trade_id` clusters (SKILL Step 3b) |

**If the id cannot be resolved on any source, do NOT fabricate the record — and
do NOT fall back to the inline `<rfq description>` to build a structure.** With
no resolved row the asset isn't known, so live instruments can't be built either.
Emit only the SKILL.md Step 7 unresolved line (fill, mark, spot, size, side,
structure all *unavailable*) and stop.

---

## Field mapping — trade-tape row → analysis fields

`paradigm_trade_tape_slim` carries the information that used to arrive as pasted
JSON. Map by the tape's actual columns:

| Analysis field (SKILL Step 1) | Trade-tape column |
|---|---|
| `description` / legs | `DESCRIPTION` (structure name + expiry + strikes; parse per the examples above) |
| `action` / taker side | `SIDE` (`BUY` / `SELL`) |
| `quantity` | `QTY` (contracts) |
| `price` (fill) | `PRICE` (execution price, in `QUOTE_CURRENCY`) |
| `mark_price` | `REF_PRICE` (reference/mark at trade time) |
| `displayValues.markOffset` | computed: `PRICE − REF_PRICE` |
| `venue` | from `PRODUCT` suffix — the token after ` - ` (e.g. `DBT` Deribit, `PRDX` Paradex, `BYB` Bybit, `OKX` OKX; **non-exhaustive** — new venues appear over time. Surface an unrecognized suffix verbatim; never fail or guess on one) |
| `product_codes` / asset + kind | from `PRODUCT` — e.g. `BTC OPTION - DBT`, `ETH PERPETUAL - DBT`, `BTC OPTION - PRDX` |
| `quote_currency` | `QUOTE_CURRENCY` (`BTC` / `ETH` / `USD` …) |
| USD notional | `NOTIONAL_VOLUME_USD` |
| `rfqType` (`RFQ`/`OB`) | `AUCTION` |
| ids | `RFQ_ID`, `TRADE_ID`, `BLOCK_TRADE_ID` |

**Not in the tape — pull live or infer (never fabricate):**
- `index_price` / **spot**: not a tape column — pull the live underlying
  (`BTC-PERPETUAL` / `ETH-PERPETUAL`) mark in Step 2, or use the description.
- `strategy_code`: not stored — infer the structure from `DESCRIPTION`
  (see `references/strategy-codes.md`).
- per-leg greeks/IV: not in the tape — fetched live in Step 2 (or via Bullish
  chain snapshots / exchange market data through `paradigm-data-discovery` for historical).

---

## The role of the inline `<rfq description>`

The `<rfq description>` after the `rfq_id` is **user reference only — not an input to the
analysis**. Do not use it to choose the asset, build instrument names, filter the tape, or
decide the structure; every one of those comes from the resolved `FILL` row (`PRODUCT` +
`DESCRIPTION`). It may omit the asset or be outright wrong.

- **The only allowed use:** if the resolved row materially disagrees with what the description
  implied, add a one-line note that the id resolved to a *different* trade — but the resolved
  row still governs every field.
- **Never** let the description seed a live fetch before the tape resolves, and **never**
  fall back to "parse the structure from the description" — if the id doesn't resolve and the
  asset therefore isn't known, report the RFQ unresolved (Step 7); do not fabricate an
  asset/strike/structure or default to BTC.
