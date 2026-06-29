---
name: paradigm-options-recap
description: >
  Options market recap for a user-specified window, invoked via /recap. Parses
  "/recap [asset] [options] [window]" (e.g. "/recap btc options 8h") and produces
  a fixed-format recap with four sections: snapshot, biggest print, block flow,
  and vol surface. Use when the user types /recap or asks for a market recap,
  options flow summary, "what happened in BTC options", or "last Xh of flow".
  The output format is fixed — always the same four sections in the same order.
compatibility: Deribit public API (curl), Paradigm hot surface (DuckDB+S3 via IRSA),
  OKX/Bullish/IBIT public APIs. No authentication required for public APIs;
  S3 hot surface requires the IRSA bootstrap (see paradigm-data-discovery skill).
metadata:
  author: tradeparadigm
  version: "1.8"
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

Target: **≤30s, every run.** The path is exactly **two tool calls**, then you
render:

1. **One bash block** — bootstrap S3 creds, run the single DuckDB session into
   CSVs, then call `recap.py` once. The orchestrator does *all* the Deribit
   fetching (with concurrent, time-sliced pagination), CSV ingest, snapshot
   assembly, and vol math, and prints one JSON object.
2. **Render** the four sections from that JSON.

This is a copy-paste runbook. **Do not** improvise extra steps — no per-page
trade backfill, no `DESCRIBE`, no instrument/ticker fan-out, no hand-building a
snapshot from CSVs. Every one of those was a multi-second model round-trip and
a source of run-to-run drift. `recap.py` owns them now; you only set `WIN`,
`ASSET`, `CUR` and read the result.

## Modes — pick one, then render the same four sections

- **Live** (real `/recap`, tools available): run the bash block below, render
  from `recap.py`'s JSON. This is the normal path.
- **Injected data** (a `<market_data>` block with `derived` is in context):
  that block is the sole source of truth. Render directly from
  `derived.realized_vol` (RV/VRP), `derived.top_blocks` (Biggest Print + Block
  Flow), and `derived.vol_surface` (skew/term + per-expiry ATM/RR/Fly), reading
  DVOL open/close and the spot range from the raw `dvol`/`spot` tape. **Do not**
  run the script, recompute those figures, or add any estimated-data
  disclaimer.
- **Simulate** (no tools and no injected data): produce the four sections with
  plausible example values following the templates exactly, and prepend one
  line: `⚠ Data estimated — no live feed available.`

## The run — one bash block

```bash
mkdir -p /tmp/recap
ASSET=BTC; CUR=BTC; WIN=8h            # from the command; 1d → 24h
RWIN=${WIN/1d/24h}

# 1. STS bootstrap (idempotent; see paradigm-data-discovery skill)
TOKEN=$(cat "$AWS_WEB_IDENTITY_TOKEN_FILE")
CREDS=$(curl -s "https://sts.ap-northeast-1.amazonaws.com/?Action=AssumeRoleWithWebIdentity&Version=2011-06-15&RoleArn=${AWS_ROLE_ARN}&RoleSessionName=duckdb&WebIdentityToken=${TOKEN}")
AK=$(echo "$CREDS" | grep -o '<AccessKeyId>[^<]*'     | cut -d'>' -f2)
SK=$(echo "$CREDS" | grep -o '<SecretAccessKey>[^<]*' | cut -d'>' -f2)
ST=$(echo "$CREDS" | grep -o '<SessionToken>[^<]*'    | cut -d'>' -f2)

# 2. One DuckDB session → CSVs (one statement per line; alias the reserved word `at`)
SIG=s3://terminal-dime-prod/paradigm_data/hot/hot__market_signals_1m.parquet
REC=s3://terminal-dime-prod/paradigm_data/hot/hot__recap_${RWIN}.parquet
cat > /tmp/recap.sql <<SQL
INSTALL httpfs;
LOAD httpfs;
SET s3_region='ap-northeast-1';
SET s3_access_key_id='${AK}';
SET s3_secret_access_key='${SK}';
SET s3_session_token='${ST}';
COPY (SELECT signal_type, exchange, expiry, value, atm_call_iv, atm_put_iv, underlying_price, call_volume, put_volume, buy_volume, sell_volume, notional, trade_count, "at" AS at_ms FROM read_parquet('${SIG}') WHERE asset='${CUR}') TO '/tmp/recap/snapshot.csv' (HEADER, DELIMITER ',');
COPY (SELECT exchange, metric, open, close, high, low FROM read_parquet('${REC}') WHERE asset='${CUR}' AND row_type='dvol_spot') TO '/tmp/recap/dvol_spot.csv' (HEADER, DELIMITER ',');
COPY (SELECT exchange, optionType, volume_sum, notional, buy_volume, sell_volume, trade_count FROM read_parquet('${REC}') WHERE asset='${CUR}' AND row_type='volume') TO '/tmp/recap/volume.csv' (HEADER, DELIMITER ',');
COPY (SELECT block_id, notional, volume_sum, leg_count, avg_iv FROM read_parquet('${REC}') WHERE asset='${CUR}' AND row_type='block') TO '/tmp/recap/block.csv' (HEADER, DELIMITER ',');
COPY (SELECT expiry, strike, optionType, markIV_close, delta, openInterest, underlying_price FROM read_parquet('${REC}') WHERE asset='${CUR}' AND row_type='surface' AND exchange='deribit') TO '/tmp/recap/surface.csv' (HEADER, DELIMITER ',');
SQL
duckdb < /tmp/recap.sql > /tmp/recap/duck.log 2>&1 || echo "duckdb failed (see duck.log) — recap.py will mark hot sections No data"

# 3. One orchestrator call — fetches Deribit, ingests CSVs, computes, prints JSON
uv run scripts/recap.py --asset "$ASSET" --window "$WIN" --csv-dir /tmp/recap --pretty
```

`recap.py` fetches the Deribit tape itself (7d hourly closes for realized vol +
the window's option trades via concurrent, time-sliced pagination — no serial
backfill) and reads the hot CSVs the DuckDB step wrote. Hot files are
authoritative for DVOL/spot/volume/surface; Deribit supplies only the 7d closes
and block-leg geometry (hot carries neither). If a source fails, the affected
fields come back `null` and a line is added to `warnings` — the JSON is always
renderable.

**Freshness / degradation** (read `warnings` in the JSON):
- DuckDB failed / CSVs absent → snapshot volume, P/C, and vol surface are
  `null`. Render those rows as `No data` and prepend `⚠ hot surface unavailable`.
- A `~N min old` note belongs only if you can see the hot `at_ms` lagging
  wall-clock by minutes; a few minutes is fine — proceed.
- Never fabricate a number to fill a `null`.

## Output JSON → sections (field map)

`recap.py` prints one object. Render each section straight from these fields —
no recomputation, no re-fetching.

| JSON path | Renders into |
|---|---|
| `header.{asset,window,start_utc,end_utc}` | Title line |
| `snapshot.{spot,spot_from,spot_low,spot_change_pct,dvol,dvol_open,dvol_close,dvol_label,rv_7d,vrp,vrp_label,volume_usd_m,primary_venue,pc_ratio,pc_dominant,spot_vol_label}` | **Snapshot** |
| `biggest_print.{expiry,structure,size,notional_m,time_utc,venue?,side}` | **Biggest Print** |
| `block_flow.{total_m,n_blocks,rows[]}` — each row `{rank,structure,notl_m,detail}` | **Block Flow** |
| `vol_surface.{skew_line,term_line,rows[]}` — each row `{expiry,atm,rr_25d,fly,extrapolated}` | **Vol Surface** |

The vol math (realized-vs-implied, Black-76 block clustering/ranking, surface
skew/term) is done in `recap.py` via the bundled `vol_math.py`. Never hand-
compute these. To verify the math offline: `python3 scripts/test_vol_math.py`.
To smoke-test the orchestrator without S3 (Deribit-only):
`uv run scripts/recap.py --asset btc --window 8h --no-s3 --pretty`.

## Output Format — FIXED

Four sections, this exact order, every recap. Never reorder, add, or drop
sections. **Do not emit Themes, Dealer positioning, or a Bottom Line — those
have been removed.** Work silently — no narration.

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
P/C       [X.Xx]      [calls/puts] dominant
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

**Vol Surface**
Skew: front 25Δ RR [±X]v → [puts bid / calls bid] · Term: [front]v → [back]v → [contango / flat / backwardation]

```yaml
Expiry     ATM      25d RR    Fly
---------  ------   --------  -----
[DDMMMYY]  [X.X]v   [±X.X]v   [X.X]v
…
```

Formatting rules:
- ATM / RR / Fly columns: current (close) values, `X.Xv` precision.
- Append `*` to any cell whose value is derived from extrapolated wings
  (`extrapolated: true` on that surface row); e.g. `-4.0v*`.

Example:

```
Expiry     ATM     25d RR    Fly
---------  ------  --------  -----
12JUN26    43.2v   -5.6v     1.6v
26JUN26    43.4v   -5.7v     1.0v
31JUL26    43.1v   -4.0v*    0.6v
```

---

## Thin Window

(< 2h, no blocks) — output all four sections; mark empty ones `No data`.
