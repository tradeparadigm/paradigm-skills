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
  version: "1.4.2"
---

# Options Recap

## Command Syntax

`/recap [asset] [options] [window]` — order-independent, all optional.

| Token | Examples | Default |
|---|---|---|
| `asset` | `btc`, `eth` | `btc` |
| `window` | `1h`, `4h`, `8h`, `24h`, `1d` | `24h` |
| `options` | the literal word `options` | ignored — a no-op keyword (this skill is always options); `run_recap.sh` strips it |

`/recap` alone = BTC options, last 24h. Still pass just `<ASSET> <WINDOW>` to
`run_recap.sh` — it drops a stray `options`/`option` token, so `/recap btc
options 8h` and `/recap btc 8h` resolve identically.

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
