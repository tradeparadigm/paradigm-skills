---
name: paradigm-options-recap
description: >
  Options market recap for a user-specified window, invoked via /recap. Parses
  "/recap [asset] [options] [window]" (e.g. "/recap btc options 8h") and produces
  a fixed-format recap: snapshot block, biggest print, block flow table, themes,
  vol surface, and bottom line. Use when the user types /recap or asks for
  a market recap, options flow summary, "what happened in BTC options", or "last Xh of flow".
  The output format is fixed — always the same sections in the same order.
compatibility: Deribit public API (curl), Paradigm hot surface (DuckDB+S3 via IRSA),
  OKX/Bullish/IBIT public APIs. No authentication required for public APIs;
  S3 hot surface requires the IRSA bootstrap (see paradigm-data-discovery skill).
metadata:
  author: tradeparadigm
  version: "1.4"
---

# Options Recap

## Command Syntax

`/recap [asset] [window]` — order-independent, all optional.

| Token | Examples | Default |
|---|---|---|
| `asset` | `btc`, `eth` | `btc` |
| `window` | `1h`, `4h`, `8h`, `24h`, `1d` | `24h` |

`/recap` alone = BTC options, last 24h.

## Performance Contract — read first

Cold target: **≤45s end-to-end**. Warm (second recap in <5 min): **≤20s**.
The whole recap is **four independent I/O operations** plus a single Python
analysis pass. Run the I/O in parallel. Anything sequential here is a bug.

The four ops:

| # | Op | What it produces |
|---|---|---|
| 1 | DuckDB read of `hot__market_signals_1m.parquet` | Snapshot rows (DVOL, spot, vol_last_min, per-expiry ATM IV) |
| 2 | DuckDB read of `hot__recap_<window>.parquet` | All `row_type` slices (dvol_spot, volume, flow, block, surface) |
| 3 | `curl` Deribit `get_tradingview_chart_data` (7d hourly closes) | Realized vol input |
| 4 | `curl` Deribit `get_last_trades_by_currency` (window, paginated) | Block leg geometry + flow greeks input |

Fire all four with `&` then `wait`. Do not start Python until `wait` returns.

## Data Fetches

**Two hot reads cover the whole recap. They are authoritative — never
`curl`/`web_fetch` anything they already carry** (DVOL, spot, window volume,
ATM IV, per-contract flow, vol surface). Mixing live-API values into a
hot-file recap is the #1 cause of inconsistent reports.

**Read 1 — snapshot (the "now" anchor).** Single DuckDB read of
`s3://terminal-dime-prod/paradigm_data/hot/hot__market_signals_1m.parquet`:
current DVOL, spot per venue, last-minute volume + call/put split, current
ATM IV per expiry (`atm_call_iv`/`atm_put_iv` for skew), recent block
activity. Filter by `signal_type`.

**Read 2 — recap window.** Single DuckDB read of
`s3://terminal-dime-prod/paradigm_data/hot/hot__recap_<window>.parquet` for
the recap `window` (`1h`/`4h`/`8h`/`24h`; `1d`→`24h`; `5m`/`10m`/`20m` also
exist). Pick rows by `row_type` — this map is the contract:

| Recap section | `row_type` | Columns to read |
|---|---|---|
| Snapshot — DVOL/spot OHLC | `dvol_spot` | `metric` (`dvol`\|`spot`), `open`, `close`, `high`, `low` |
| Snapshot — Volume + P/C | `volume` | `volume_sum`, `notional`, `buy_volume`, `sell_volume`, `trade_count` (P/C via `optionType`) |
| Themes (screen flow) | `flow` | `expiry`, `strike`, `optionType`, `side`, `volume_sum`, `avg_iv` |
| Block Flow (ranking) | `block` | `block_id`, `notional`, `volume_sum`, `leg_count`, `avg_iv` |
| Vol Surface | `surface` | `expiry`, `strike`, `optionType`, `markIV_close`, `delta`, `openInterest`, `underlying_price` |

IV on `flow`/`block` rows is `avg_iv`; `markIV_close`/`delta` are on
`surface` rows only (null on `flow`). `at` is Unix ms.

**`curl` — only these two (hot never carries them):**

| Data | Endpoint | Used for |
|---|---|---|
| Spot 7d | `/api/v2/public/get_tradingview_chart_data?instrument_name=BTC-PERPETUAL&resolution=60&start_timestamp=<7d-ago>&end_timestamp=<now>` → `result.close[]` | realized vol (hot maxes at 24h) |
| Window option trades | `/api/v2/public/get_last_trades_by_currency?currency=BTC&kind=option&count=1000&start_timestamp=<ms>&sorting=desc` | block leg detail (biggest print) + flow-greeks clustering by `block_trade_id` |

**Use `curl` directly, not `web_fetch`.** `web_fetch` truncates `get_last_trades_by_currency` responses at ~20KB, which is below the size of a 1000-trade reply. You will silently lose blocks. Always go to disk:

```bash
curl -s "<url>" -o /tmp/trades.json
```

For windows >2h you usually need pagination. Fetch desc + asc in parallel and dedupe by `trade_id`. If `asc[-1].timestamp < window_end - 30min`, fetch one more page anchored at that cursor:

```bash
( curl -s "...&sorting=desc&start_timestamp=$WIN_START&end_timestamp=$NOW" -o /tmp/trades_desc.json ) &
( curl -s "...&sorting=asc&start_timestamp=$WIN_START&end_timestamp=$NOW" -o /tmp/trades_asc.json ) &
```

**Hot-only — no live reconstruction of these metrics.** The hot files are
the single source for DVOL/spot/volume/flow/surface; never rebuild them
from exchange APIs. Handle freshness by the `at` timestamp, not by falling back:
- **Stale** (file present, `at` behind wall-clock): proceed and prepend
  `⚠ hot data ~N min old`. A few-minute-old window file is fine.
- **Absent** (read 404s): emit the fixed format with affected sections marked
  `No data` and prepend `⚠ hot surface unavailable`. Do not fabricate.

## DuckDB — single-session, single-file query

Run **one** DuckDB invocation per recap. Bootstrap STS creds once, emit every dataset via `COPY (…) TO` into `/tmp/recap/*.csv`, then read those in Python. Eliminates per-invocation `INSTALL httpfs; LOAD httpfs;` and cred-set overhead.

**Two SQL gotchas — both must be honored or queries fail silently or with cryptic errors:**

1. **`at` is a reserved keyword.** Always alias: `"at" AS at_ms`.
2. **One statement per line in the file.** Heredocs assembled with `$(cat …)` collapse newlines and DuckDB parses the lot as a single malformed statement. Write the .sql file with literal newlines (here-doc with `EOF`, or `printf`), never via `$(cat oneline.sql) other_stmt`.

Reference layout for the combined query file (`/tmp/recap.sql`):

```sql
INSTALL httpfs;
LOAD httpfs;
SET s3_region='ap-northeast-1';
SET s3_access_key_id='<AK>';
SET s3_secret_access_key='<SK>';
SET s3_session_token='<ST>';

COPY (
  SELECT signal_type, exchange, expiry, value, atm_call_iv, atm_put_iv,
         underlying_price, call_volume, put_volume, buy_volume, sell_volume,
         notional, trade_count, "at" AS at_ms
  FROM read_parquet('s3://terminal-dime-prod/paradigm_data/hot/hot__market_signals_1m.parquet')
  WHERE asset='BTC'
) TO '/tmp/recap/snapshot.csv' (HEADER, DELIMITER ',');

COPY (
  SELECT exchange, metric, open, close, high, low
  FROM read_parquet('s3://terminal-dime-prod/paradigm_data/hot/hot__recap_8h.parquet')
  WHERE asset='BTC' AND row_type='dvol_spot'
) TO '/tmp/recap/dvol_spot.csv' (HEADER, DELIMITER ',');

COPY (… row_type='volume' …)  TO '/tmp/recap/volume.csv'  (HEADER, DELIMITER ',');
COPY (… row_type='flow'   …)  TO '/tmp/recap/flow.csv'    (HEADER, DELIMITER ',');
COPY (… row_type='block'  …)  TO '/tmp/recap/block.csv'   (HEADER, DELIMITER ',');
COPY (… row_type='surface' AND exchange='deribit' …) TO '/tmp/recap/surface.csv' (HEADER, DELIMITER ',');
```

Schema is documented above and in the data-discovery skill. **Do not `DESCRIBE` the parquets at runtime** — only fall back to `DESCRIBE` if a column-not-found error fires.

## Parallel I/O harness

```bash
mkdir -p /tmp/recap
WIN=8h; NOW=$(date -u +%s%3N); WIN_START=$((NOW - 8*3600*1000)); SEVEN_D=$((NOW - 7*24*3600*1000))

# Build /tmp/recap.sql once (creds + COPY blocks above)

( duckdb < /tmp/recap.sql > /tmp/recap/duck.log 2>&1 ) &
( curl -s "https://www.deribit.com/api/v2/public/get_tradingview_chart_data?instrument_name=BTC-PERPETUAL&resolution=60&start_timestamp=$SEVEN_D&end_timestamp=$NOW" -o /tmp/recap/chart.json ) &
( curl -s "https://www.deribit.com/api/v2/public/get_last_trades_by_currency?currency=BTC&kind=option&count=1000&start_timestamp=$WIN_START&end_timestamp=$NOW&sorting=desc" -o /tmp/recap/trades_desc.json ) &
( curl -s "https://www.deribit.com/api/v2/public/get_last_trades_by_currency?currency=BTC&kind=option&count=1000&start_timestamp=$WIN_START&end_timestamp=$NOW&sorting=asc"  -o /tmp/recap/trades_asc.json  ) &
wait
```

Only after `wait` returns, run the analysis script.

## Session cache

If `/tmp/recap/cache.json` exists with `cached_at < 5min` ago, reuse:

- `realized_vol_7d` — 7d closes barely move in 5 min; skip the chart fetch
- `vol_surface` — front-month skew is stable on 5-min scale; skip the surface CSV ingest

Cache invalidation: any time `window`, `asset`, or the snapshot `at` advances >5 min, refresh both.

For a `/recap` issued within 5 min of the prior one this turns the run into: 1 DuckDB read + 1 trades curl + 1 Python pass. Target ≤20s.

## Computing the numbers

Realized vol, flow greeks (Black-76), and surface skew are math that LLMs get wrong by estimating. **Always use the bundled script; never hand-compute these.**

```bash
uv run scripts/paradigm_options_recap.py --data snapshot.json
```

Build `snapshot.json` **from the CSV outputs** of the single DuckDB run, plus the two `curl` outputs:

| Field | Source |
|---|---|
| `dvol_close` | `snapshot.csv` row where `signal_type='dvol'` (or `dvol_spot.csv` `close`) |
| `spot` | `snapshot.csv` row where `signal_type='spot'` |
| `tickers` (`{sym: {mark_iv, delta}}`) | `surface.csv` — `mark_iv`=`markIV_close`, `delta`=`delta`. Build `sym` as the Deribit instrument name `{asset}-{expiry}-{int(strike)}-{C\|P}` (expiry already Deribit-native, e.g. `3JUL26`). Filter to deribit only. No instrument-list/ticker fan-out. |
| `spot_closes_7d` | `chart.json` → `result.close[]` |
| `trades` | merge `trades_desc.json` + `trades_asc.json`, dedupe on `trade_id`, group by `block_trade_id` |

Returns `realized_vol`, `flow_greeks`, `top_blocks`, `vol_surface`. If a `derived` block is already injected into context, read it and skip the script. **Themes need no script** — group `flow.csv` rows directly.

## Analysis

**Snapshot** — pull spot, DVOL open→close, RV(7d) vs implied. VRP = implied − realized. Label:
- spot↑ vol↓ → "vol sold through rally"
- spot↓ vol↑ → "vol bid into weakness"
- spot↑ vol↑ → "vol bought through rally"

RV must be 7-day trailing window. Read from `derived.realized_vol` or the script — never estimate.

**Block Flow** — rank from `block.csv` (sort by `notional`); the `$XM / N blocks` header is `sum(volume_sum) * spot` and `count(block_id)`. For the biggest few, pull leg geometry from the merged trades file (cluster by `block_trade_id`) → feed to the script's `top_blocks`. Mark `two-way`/one-sided from the field — do not infer.

Dealer positioning from `flow_greeks.positioning_label`:
- short gamma → chase spot, amplify moves
- long gamma → fade moves
- balanced → no decisive positioning

**Themes** — group `flow.csv` rows (screen, non-block) by expiry/strike/direction; size is `volume_sum`, IV is `avg_iv`. 2–4 bullets. Named, factual, no intent inference.

**Vol Surface** — built from `surface.csv`, **not** a fetch:
1. Feed the surface rows as the script's `tickers` plus `spot`.
2. Read `atm_iv`, `rr_25d`, `butterfly_25d`, `term_structure` back. Note `wings_extrapolated` if set.

## Output Format — FIXED

Six sections, this exact order, every recap. Never reorder, add, or drop sections.

Work silently — no narration. If hot files are stale or absent, prepend the matching banner from Data Fetches (`⚠ hot data ~N min old` / `⚠ hot surface unavailable`).

---

**Shape to mirror:**

**[ASSET] Options · [WINDOW] Recap · [HH:MM]–[HH:MM] UTC**

**Snapshot**

```yaml
Spot      $[X]        [up/down X%] (from $[Y])
DVOL      [X]v        [flat/rising/falling] ([open] -> [close])
RV 7d     [X]v        implied [CHEAP/RICH/IN LINE] vs realized
VRP       [±X]v       vol [underpriced/overpriced] vs delivered
Volume    $[X]M       [primary venue] (incl. Paradigm)
P/C       [X.Xx]x     [calls/puts] dominant
```

**Biggest Print**

```yaml
[DDMMMYY] [structure]   [Nx]   $[X]M   [HH:MM] UTC   via [Venue]
```

**Block Flow — $[X]M / [N] blocks**

```yaml
#  Structure            Notl     Detail
-  -------------------  -------  ------------------------------------------
1  [structure]          $[X]M    [strikes] x[size] - [SIDE] [IV]v [two-way/one-sided]
2  …
```

Dealer positioning: [short/long] vega + [short/long] gamma → [mechanical read]; [implication].

**Themes**
1. [Theme name] — [structure, strikes, size, IV, side]. [One factual line.]
2. …

[2–4 themes. Facts only — no intent inference.]

**Vol Surface**
Skew: front 25Δ RR [±X]v → [puts bid / calls bid] · Term: [front]v → [back]v → [contango / flat / backwardation]

```yaml
Expiry     ATM                  25d RR               Fly
---------  -------------------  -------------------  -------------------
[DDMMMYY]  [X.X] → [Y.Y]v      [±X.X] → [±Y.Y]v    [X.X] → [Y.Y]v
…
```

`* wings extrapolated` (if applicable)

**Bottom Line**
[1–2 sentences. Calls/puts lead, volume, dominant block structure. Dealer positioning vs vol surface vs RV/IV gap. No forecasts.]

---

## Thin Window

(< 2h, no blocks, < 20 screen trades) — output all six sections; mark empty ones `No data`.
