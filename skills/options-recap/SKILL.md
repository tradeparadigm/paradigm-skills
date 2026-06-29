---
name: paradigm-options-recap
description: >
  Options market recap for a user-specified window, invoked via /recap. Parses
  "/recap [asset] [options] [window]" (e.g. "/recap btc options 8h") and produces
  a fixed-format recap: snapshot block, biggest print, block flow table, themes,
  vol surface, and bottom line. Use when the user types /recap or asks for
  a market recap, options flow summary, "what happened in BTC options", or "last Xh of flow".
  The output format is fixed — always the same sections in the same order.
compatibility: Deribit public API (web_fetch), Paradigm block tape (if injected),
  OKX/Bullish/IBIT public APIs (web_fetch). No authentication required.
metadata:
  author: tradeparadigm
  version: "1.3"
---

# Options Recap

## Command Syntax

`/recap [asset] [window]` — order-independent, all optional.

| Token | Examples | Default |
|---|---|---|
| `asset` | `btc`, `eth` | `btc` |
| `window` | `1h`, `4h`, `8h`, `24h`, `1d` | `24h` |

`/recap` alone = BTC options, last 24h.

## Data Fetches

**Two hot reads cover the whole recap. They are authoritative — never
`web_fetch` anything they already carry** (DVOL, spot, window volume,
ATM IV, per-contract flow, vol surface). Mixing live-API values into a
hot-file recap is the #1 cause of inconsistent reports. `web_fetch` is
for the two things hot never has, plus stale-file recovery.

**Read 1 — snapshot (the "now" anchor).** One DuckDB read of
`s3://terminal-dime-prod/paradigm_data/hot/hot__market_signals_1m.parquet`:
current DVOL, spot per venue, last-minute volume + call/put split, current
ATM IV per expiry (`atm_call_iv`/`atm_put_iv` for skew), recent block
activity. Filter by `signal_type`.

**Read 2 — recap window (everything over the window).** One DuckDB read of
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

**`web_fetch` — only these two (hot never carries them):**

| Data | Endpoint | Used for |
|---|---|---|
| Spot 7d | `/api/v2/public/get_tradingview_chart_data?instrument_name=BTC-PERPETUAL&resolution=60&start_timestamp=<7d-ago>&end_timestamp=<now>` → `result.close[]` | realized vol (hot maxes at 24h) |
| Window option trades | `/api/v2/public/get_last_trades_by_currency?currency=BTC&kind=option&count=1000&start_timestamp=<ms>&sorting=desc` | block leg detail (biggest print) + flow-greeks clustering by `block_trade_id` |

**Hot-only — no live reconstruction of these metrics.** The hot files are
the single source for DVOL/spot/volume/flow/surface; never rebuild them
from exchange APIs (a Deribit-only, unharmonized rebuild is a different,
lower-quality report wearing the same format). Handle freshness by the
`at` timestamp, not by falling back:
- **Stale** (file present, `at` behind wall-clock): proceed from it and
  prepend one line — `⚠ hot data ~N min old`. A few-minute-old window file
  is fine for an hours-long recap.
- **Absent** (read 404s): emit the fixed format with the affected sections
  marked `No data` and prepend `⚠ hot surface unavailable`. Do not
  fabricate from live APIs.

The only `web_fetch`es in a recap are the two required pulls above (7d
spot, window trades) — those are data hot never carries, not a fallback.

## Computing the numbers

Realized vol, flow greeks (Black-76), and surface skew are math that LLMs get wrong by estimating. **Always use the bundled script; never hand-compute these.**

```bash
uv run scripts/paradigm_options_recap.py --data snapshot.json
```

Build `snapshot.json` **mostly from the hot rows** — only `spot_closes_7d`
and `trades` come from `web_fetch` (omit any field to skip that section):

| Field | Source |
|---|---|
| `dvol_close` | snapshot `dvol` row (or recap `dvol_spot`/`dvol` `close`) |
| `spot` | snapshot `spot` row |
| `tickers` (`{sym: {mark_iv, delta}}`) | recap **`surface` rows** — `mark_iv`=`markIV_close`, `delta`=`delta`. Build `sym` as the Deribit instrument name `{asset}-{expiry}-{int(strike)}-{C\|P}` (the script parses expiry/type from this key, so it must be exact; `surface.expiry` is already Deribit-native, e.g. `3JUL26`). Filter to `exchange='deribit'`. No instrument-list/ticker fan-out. |
| `spot_closes_7d` | `web_fetch` 7d spot `close[]` |
| `trades` | `web_fetch` window option trades (block clustering for `flow_greeks`/`top_blocks`) |

Returns `realized_vol`, `flow_greeks`, `top_blocks`, `vol_surface`. If a `derived` block is already injected into context, read it and skip the script. **Themes need no script** — group the `flow` rows directly.

## Analysis

**Snapshot** — pull spot, DVOL open→close, RV(7d) vs implied. VRP = implied − realized. Label:
- spot↑ vol↓ → "vol sold through rally"
- spot↓ vol↑ → "vol bid into weakness"
- spot↑ vol↑ → "vol bought through rally"

RV must be 7-day trailing window. Read from `derived.realized_vol` or the script — never estimate.

**Block Flow** — rank from the recap `block` rows (sort by `notional`); the `$XM / N blocks` header is their sum/count. For the biggest few, pull leg geometry (strikes × size × side × IV) from the one `web_fetch` trades pull, clustered by `block_trade_id` → feed to the script's `top_blocks`. Mark `two-way`/one-sided from the field — do not infer.

Dealer positioning from `flow_greeks.positioning_label`:
- short gamma → chase spot, amplify moves
- long gamma → fade moves
- balanced → no decisive positioning

**Themes** — group the recap `flow` rows (screen, non-block) by expiry/strike/direction; size is `volume_sum`, IV is `avg_iv`. 2–4 bullets. Named, factual, no intent inference.

**Vol Surface** — built from the recap `surface` rows, **not** a fetch:
1. Feed the `surface` rows as the script's `tickers` (`mark_iv`=`markIV_close`, `delta`=`delta`) plus `spot`.
2. Read `atm_iv`, `rr_25d`, `butterfly_25d`, `term_structure` back. Note `wings_extrapolated` if set.

## Output Format — FIXED

Six sections, this exact order, every recap. Never reorder, add, or drop sections.

Work silently — no narration. If the hot files are stale or absent, prepend the matching banner from Data Fetches (`⚠ hot data ~N min old` / `⚠ hot surface unavailable`).

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
