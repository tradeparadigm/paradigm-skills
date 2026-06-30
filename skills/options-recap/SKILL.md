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
  version: "1.9"
---

# Options Recap

## Command Syntax

`/recap [asset] [window]` — order-independent, all optional.

| Token | Examples | Default |
|---|---|---|
| `asset` | `btc`, `eth` | `btc` |
| `window` | `1h`, `4h`, `8h`, `24h`, `1d` | `24h` |

`/recap` alone = BTC options, last 24h.

## How to run it — pick the mode, then emit the four sections

**Live (real `/recap`, tools available) — this is the normal path.** Run ONE
command and relay its stdout **verbatim** as your entire reply:

```bash
bash scripts/run_recap.sh BTC 8h      # <ASSET> <WINDOW> from the command; 1d→24h
```

That script does everything — STS bootstrap, the single DuckDB session over the
hot surface, the Deribit tape (7d closes + window trades via concurrent,
time-sliced pagination), the vol math, and final formatting — and prints the
finished four-section recap. **Do not** add commentary, reformat it, re-fetch
anything, or run extra steps. Its output already is the recap. If the first
line is a `⚠ …` banner, keep it. Target: well under 30s; the heavy lifting is
~2.5s and the rest is just this one round-trip.

**Injected data (a `<market_data>` block with `derived` is in context).** No
tools — render the four sections yourself from `derived.realized_vol` (RV/VRP),
`derived.top_blocks` (Biggest Print + Block Flow), and `derived.vol_surface`
(skew/term + per-expiry ATM/RR/Fly), reading DVOL open/close and the spot range
from the raw `dvol`/`spot` tape. Report those figures directly; do not recompute
them and do not add a disclaimer.

**Simulate (no tools and no injected data).** Produce the four sections with
plausible example values following the template exactly, and prepend one line:
`⚠ Data estimated — no live feed available.`

## Output Format — FIXED

Four sections, this exact order, every recap. Never reorder, add, or drop
sections. **Do not emit Themes, Dealer positioning, or a Bottom Line.** Work
silently — no narration. `run_recap.sh` already emits exactly this shape; the
template below is the contract it implements and what the injected/simulate
modes must reproduce.

---

**[ASSET] Options · [WINDOW] Recap · [HH:MM]–[HH:MM] UTC**

**Snapshot**

```yaml
Spot      $[X]        [up/down X%] (from $[Y], low $[Z])
DVOL      [X]v        [flat/rising/falling] ([open] -> [close])
RV 7d     [X]v        implied [CHEAP/RICH/IN LINE] vs realized
VRP       [±X]v       vol [underpriced/overpriced] vs delivered
Volume    $[X]M       [primary venue] (incl. Paradigm)
P/C       [X.Xx]      [calls/puts] dominant
```

**Biggest Print**

```yaml
[DDMMMYY] [structure]   [Nx]   $[X]M   [HH:MM] UTC   via [Venue] ([side], [IV]v avg)
```

**Block Flow — $[X]M / [N] blocks**

```yaml
#  Structure            Notl     Detail
-  -------------------  -------  ------------------------------------------
1  [structure]          $[X]M    [strikes] x[size] - [side] [IV]v [two-way/one-sided]
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

Formatting rules: ATM/RR/Fly are current (close) values, `X.Xv` precision.
Append `*` to any cell derived from extrapolated wings (e.g. `-4.0v*`).

---

## Internals (maintenance only — the agent does not run these directly)

`run_recap.sh` → `scripts/recap.py` own the whole pipeline; the math lives in
`scripts/vol_math.py`. Tests (stdlib-only, no network — run in CI):
`python3 scripts/test_vol_math.py` (math) and `python3 scripts/test_recap.py`
(orchestrator: hot-CSV ingest, the volume/block corruption guards, assembly,
rendering). Smoke-test against live Deribit without S3:
`uv run scripts/recap.py --asset btc --window 8h --no-s3 --render`.

Two authoritative hot reads (one DuckDB session) plus the Deribit tape cover the
recap. Hot files are authoritative for DVOL/spot/volume/surface; Deribit supplies
the 7d realized-vol closes and block-leg geometry. `row_type` map in
`hot__recap_<window>.parquet`:

| Section | `row_type` | Key columns |
|---|---|---|
| Snapshot DVOL/spot | `dvol_spot` | `metric`, `open`, `close`, `high`, `low` |
| Snapshot volume/P-C | `volume` | `exchange`, `optionType`, `volume_sum`, `notional` |
| Block Flow | `block` | `block_id`, `notional`, `volume_sum`, `leg_count`, `avg_iv` |
| Vol Surface | `surface` | `expiry`, `strike`, `optionType`, `markIV_close`, `delta` |

Known hot-data quirk recap.py defends against: the `volume`/`block` rows carry
per-exchange aggregate rows and cross-venue unit inconsistencies (Deribit/Bullish
`volume_sum` in BTC, OKX/Bybit in contracts) plus the occasional unit-corrupt
row. recap.py derives Volume/P-C from Deribit only (contracts × spot) and block
totals from the Deribit clustering that produces the displayed rows. On a hot
miss it degrades (sections read `No data`, output is prefixed `⚠ hot surface
unavailable`) rather than fabricating.

## Thin Window

(< 2h, no blocks) — output all four sections; mark empty ones `No data`.
