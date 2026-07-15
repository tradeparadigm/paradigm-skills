---
name: paradigm-options-recap
description: >
  Options market recap for a user-specified window, invoked via /recap. Parses
  "/recap [asset] [options] [window]" (e.g. "/recap btc options 8h") and produces
  a fixed-format recap with four sections: snapshot, biggest print, block flow,
  and vol surface. Use when the user types /recap or asks for a market recap,
  options flow summary, "what happened in BTC options", or "last Xh of flow".
  The output format is fixed — always the same four sections in the same order.
compatibility: Deribit public API (curl) for the tape (7d closes, block flow, and the
  Volume line's $ figure); Paradigm hot data (DuckDB+S3 via IRSA) for DVOL/spot,
  multi-venue activity/P-C, and the vol surface. No authentication required for the
  public API; the S3 reads require the IRSA bootstrap (see paradigm-data-discovery skill).
metadata:
  author: tradeparadigm
  version: "1.9"
---

# Options Recap

## Command Syntax

`/recap [asset] [options] [window]` — order-independent, all optional.

| Token | Examples | Default |
|---|---|---|
| `asset` | `btc`, `eth` | `btc` |
| `window` | any `Nm`/`Nh`/`Nd` up to 24h — `30m`, `3h`, `8h` (`1d`→`24h`) | `24h` |
| `options` | the literal word `options` | ignored — a no-op keyword (this skill is always options); `run_recap.sh` strips it |

Any `Nm`/`Nh`/`Nd` window up to 24h works and all render identically: DVOL/spot
and the multi-venue activity/P-C come from one rolling hot aggregates file sliced
to the window at query time, the surface (and its Δ columns) from `v_vol_surface`,
and the Volume ($) line and block flow both from the Deribit tape — the hot
aggregates file head-lags the live prints by ~10-15 min, so on a thin window it
under-reports $ volume (potentially below the block-flow total); the tape carries
every print with its index price and is authoritative for the Volume figure. A
malformed window exits with a clear error.

**Windows beyond 24h:** every flow source (the rolling hot aggregates file, the
Deribit public tape) retains only ~24h, so `run_recap.sh` caps any longer window
(e.g. `2d`) at 24h and prepends a one-line `⚠ window capped at 24h — …` banner
as the first line of its output — **relay it verbatim** (don't drop or reword
it). The cap lifts once >24h flow is wired to the cold store.

`/recap` alone = BTC options, last 24h. Still pass just `<ASSET> <WINDOW>` to
`run_recap.sh` — it drops a stray `options`/`option` token, so `/recap btc
options 8h` and `/recap btc 8h` resolve identically.

## How to run it — pick the mode, then emit the four sections

**Live (real `/recap`, tools available) — this is the normal path.** Run ONE
command and relay its stdout **verbatim** as your entire reply:

```bash
bash scripts/run_recap.sh BTC 8h      # <ASSET> <WINDOW>; any Nm/Nh/Nd works; 1d→24h
```

That script does everything — STS bootstrap, the single DuckDB session over the
hot surface, the Deribit tape (7d closes + window trades via concurrent,
time-sliced pagination), the vol math, and final formatting — and prints the
finished four-section recap. **Do not** add commentary, reformat it, re-fetch
anything, or run extra steps. Its output already is the recap. Your reply must
BEGIN with the script's first output line (the `⚠ …` banner when present, else
the bold header) — no preamble like "I'll run the recap", no trailing notes or
follow-up offers. If the script exits non-zero (e.g. `recap: bad window '5x'`),
**relay that error message verbatim and stop** — do not substitute a different
window, retry with defaults, or render a recap anyway. Target: well under 30s;
the heavy lifting is ~2.5s and the rest is just this one round-trip.

**Injected data (a `<market_data>` block with `derived` is in context).** No
tools — render the four sections yourself from `derived.realized_vol` (RV/VRP),
`derived.top_blocks` (Biggest Print + Block Flow), and `derived.vol_surface`
(skew/term + per-expiry ATM/RR/Fly, plus ΔATM/ΔRR/ΔFly when present — else `n/a`),
reading DVOL open/close and the spot range from the raw `dvol`/`spot` tape.
Follow the exact template in `references/output-format.md`. Report those figures
directly; do not recompute them and do not add a disclaimer.

**Simulate (no tools and no injected data).** Produce the four sections with
plausible example values following `references/output-format.md` exactly, and
prepend one line: `⚠ Data estimated — no live feed available.`

## Output Format

Four sections, this exact order, every recap: **Snapshot · Biggest Print ·
Block Flow · Vol Surface**. Never reorder, add, or drop sections. **Do not emit
Themes, Dealer positioning, or a Bottom Line.** Work silently — no narration.

In the live path `run_recap.sh` already prints exactly this shape, so you just
relay it. The full template, per-field formatting, the Vol Surface delta columns,
and the thin-window (`< 2h`) rules are the contract the script implements — see
`references/output-format.md`. Read it only when **you** render (the injected and
simulate modes); the live path never needs it.
