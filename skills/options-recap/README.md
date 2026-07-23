# options-recap — maintainer notes

Human/maintainer documentation for the `paradigm-options-recap` skill. This is
**not** loaded into the agent's context — `SKILL.md` is the runbook the agent
follows; everything an operator or contributor needs lives here.

## What it does

`/recap [asset] [window]` produces a fixed four-section options recap (Snapshot,
Biggest Print, Block Flow, Vol Surface) for BTC/ETH over a window (default 24h).
The window can be **any** `Nm`/`Nh`/`Nd` value — see "Windows" below.
The live path renders the output in `scripts/recap.py` (`render_md`) and the
agent relays it verbatim, so the format lives in code there. The exact template
is also written out in `references/output-format.md` — the contract for the
no-tool **injected** and **simulate** modes, where the agent renders it itself.
`SKILL.md` only names the four sections + the guardrails and points to that file,
so the common live path doesn't carry the full template in context.

## Architecture

The live path is **one command** the agent runs, then it relays stdout verbatim:

```
bash scripts/run_recap.sh <ASSET> <WINDOW>
        │
        ├── STS bootstrap (IRSA → temporary S3 creds)
        ├── writes $WORK/recap.sql (one DuckDB session, COPY statements → CSVs:
        │     dvol_spot, volume, surface_now/open, AND blocks from the tape)
        └── uv run scripts/recap.py --duckdb-sql $WORK/recap.sql --csv-dir $WORK --render
                    │
                    ├── runs DuckDB in a thread  ─┐  (concurrent — both are
                    ├── fetches Deribit 7d closes ─┘   network-bound)
                    │     • 7d hourly closes (realized vol) — the ONLY exchange-API call
                    ├── ingests the hot CSVs + blocks.csv (the block tape)
                    ├── vol math via scripts/vol_math.py (incl. tape block ranking/rollup)
                    └── prints the finished four-section markdown
```

- `scripts/run_recap.sh` — the live wrapper (S3 + DuckDB + recap.py).
- `scripts/recap.py` — orchestrator: fetch, ingest, assemble, compute, render.
- `scripts/vol_math.py` — pure vol math (realized-vs-implied, Black-76 flow
  greeks, tape DESCRIPTION parsing + block ranking/rollup, vol-surface skew/term).
  No I/O.
- `scripts/recap.py --no-s3 --render` — offline smoke against live Deribit only
  (7d closes + DVOL/spot); Biggest Print / Block Flow read `No data` (they're
  S3-only now).
- `references/output-format.md` — the fixed four-section template + formatting
  rules. The live path doesn't read it (the script emits the shape); it's the
  rendering contract for the injected/simulate modes and the eval harness.

## Windows

`run_recap.sh` parses the window generically into seconds (`Nm`/`Nh`/`Nd`), so
**any** window renders — there is a single data path. DVOL/spot OHLC and the
volume/`trade_count` rows come from ONE rolling file, `hot__recap_aggregates_5m_24h.parquet`
(5-min buckets over the trailing 24h), windowed at query time by `WHERE bucket_at
>= now - window` + aggregation; the vol surface + ΔATM/ΔRR/ΔFly come from
`v_vol_surface`; and Biggest Print / Block Flow come from the multi-venue Paradigm
block tape (`paradigm_trade_tape_slim`), scanned in the same DuckDB session. The
Deribit public API adds only the 7d realized-vol closes (and a live DVOL/spot
fallback if the S3 read fails).

Notes / non-obvious bits:
- **`PRESET` is just a label now.** The canonical windows (`5m 10m 20m 1h 4h 8h
  24h`) set `PRESET=1`, but since every window reads the same rolling file this no
  longer gates the data path — it's retained for the plan/test hook and as an
  observability signal (canonical vs ad-hoc window).
- **Dollar Volume is Deribit-scoped; Activity/P-C span all venues.** See "Data
  sources" below — `volume_sum` units differ per venue so the `$` Volume line uses
  only the exact-`deribit` rows (1 contract = 1 BTC), while the unit-free
  `trade_count` drives the multi-venue Activity line and the P/C ratio.
- **The old bug:** a preset `case` mapped unknown windows to a silent 8h default,
  so surface deltas were computed against an 8h-old open. Fixed by parsing the
  window into seconds instead of enumerating presets.
- **~24h Snapshot horizon.** The *Snapshot* flow sources reach back only ~24h:
  Volume / Activity / P-C / DVOL / spot come from the rolling recap-aggregates file
  (trailing 24h). Block Flow + Biggest Print now come from the months-deep Paradigm
  block tape, and the vol surface (`v_vol_surface`) retains far longer — so those are
  no longer the constraint. For windows >24h, `build()` sets a `hot_horizon` field and
  `render_md` prepends a one-line banner scoped to the hot Snapshot sections (Block
  Flow + surface span the full window). `run_recap.sh` still caps at 24h until the
  Snapshot sources are wired to the cold store — the follow-up that retires the banner.
- **Bad windows** (`3x`, `0h`, …) exit `2` with a clear message before any network.
- The raw per-venue tapes under `external/tardis/` are **not** a source here —
  they don't replicate into the pod's bucket and are stale; Deribit's public API
  covers the dynamic path instead.

Why one command: an instrumented run showed ~86% of wall time was the model
*generating* a ~50-line inline bootstrap+SQL block. Moving it into a wrapper
script (agent types one short line) and pre-rendering the markdown in `recap.py`
(agent relays verbatim) cut end-to-end from ~17s to ~6s; the mechanical path is
~1.3s. See the "Performance" notes below.

## Data sources

Three S3 sources in one DuckDB session: the recap aggregates file (DVOL/spot,
volume, activity/P-C), the `v_vol_surface` store (surface + Δ), and the Paradigm
block tape (Biggest Print + Block Flow). The Deribit public API adds only the 7d
realized-vol closes. `recap.py` reads the `dvol_spot` + `volume` rows from the
recap file. The `row_type` map in `hot__recap_aggregates_5m_24h.parquet` (a single
rolling file of 5-min buckets over the trailing 24h; windowed at query time via
`WHERE bucket_at >= now - window` + aggregation):

| Section | `row_type` | Key columns |
|---|---|---|
| Snapshot DVOL/spot | `dvol_spot` | `metric`, `open`, `close`, `high`, `low` (OHLC: `arg_min(open,bucket_at)` / `arg_max(close,bucket_at)` / `max(high)` / `min(low)`) |
| Snapshot volume/P-C/$Volume | `volume` | `exchange`, `optionType`, `volume_sum`, `notional_usd`, `trade_count` |

There is **no `surface` and no `block` `row_type`** — the vol surface lives in
`v_vol_surface`, and Biggest Print / Block Flow come from the block tape (both
below). `notional` is `notional_usd`.

**Block tape (Biggest Print + Block Flow).** `s3://dt-paradigm-data/paradigm_data/paradigm_trade_tape_slim.csv.gz`
— one flat ~1.5MB csv.gz (all dates; a full scan is sub-second, so it's read fresh
per recap, windowed by the `DATE`+`TIME` filter in `run_recap.sh`). It spans every
venue Paradigm brokers (`DBT`/`PRDX`/`BLSH`/…) with USD notional **per leg**
(`NOTIONAL_VOLUME_USD`) and the structure named in `DESCRIPTION`, so `vol_math`
does no cross-venue $ normalization and no instrument-name inference. `vol_math`
groups it two ways: by `BLOCK_TRADE_ID` (a block; Σ per-leg notional → the Biggest
Print is the single largest) and by `RFQ_ID` (a worked order; its blocks roll into
one Block Flow row with a `Blocks` count). Columns used: `DATE`, `TIME`, `PRODUCT`
(→ asset + venue), `DESCRIPTION`, `QTY`, `SIDE`, `NOTIONAL_VOLUME_USD`, `RFQ_ID`,
`BLOCK_TRADE_ID`. The tape carries **no IV** — the top blocks' IV is looked up from
`v_vol_surface` (Deribit legs only). See the `paradigm-data-discovery` skill for the
tape schema and the `paradigm-block-analyst` skill for the `DESCRIPTION` grammar.

**Multi-venue representation (truthful + consistent).** The `volume` rows span
Deribit, OKX, Bybit, Bullish, but their units aren't comparable: `volume_sum` is
each venue's native contract unit and `notional_usd` isn't yet cross-venue
normalized. So the recap only aggregates across venues on a **unit-free** basis —
**`trade_count`** — which drives the multi-venue **Activity** line (total trades +
per-venue share) and the **P/C** ratio (put vs call trades, all venues). The
dollar **Volume** line stays **Deribit-scoped** (the one venue we can price in USD
reliably: 1 contract = 1 BTC) and is labeled as such — we never sum `volume_sum`
or `notional_usd` across venues. When the pipeline later emits a normalized
cross-venue USD field, the Volume line upgrades to a true market total; until then
nothing is overstated. **No venue contract multipliers are hardcoded anywhere.**

The "now" values (latest DVOL/spot close, current surface) come from the newest
`bucket_at` in the recap file and the latest `v_vol_surface/_hot` snapshot.
(`hot__market_signals_1m.parquet` is the live signals heartbeat used by
`paradigm-block-analyst`; `/recap` no longer reads it.)
S3 access (IRSA STS bootstrap) is documented in the `paradigm-data-discovery` skill.

**Vol-surface deltas (ΔATM/ΔRR/ΔFly).** The recap aggregates file carries no
surface rows, so the full surface and window-over-window deltas read the
consolidated per-strike store `v_vol_surface` (on `dt-paradigm-data`) instead
(columns `symbol`, `type`, `mark_iv`, `delta`, `at`, …;
Deribit basis = `symbol LIKE '<ASSET>-%'`, dropping the `<ASSET>_USDC-` legs):

- **now** = the latest snapshot in the rolling `v_vol_surface/_hot.parquet`.
- **open** = the snapshot nearest window-start — from `_hot.parquet` for windows
  ≤1h (it holds ~2h of 1-min snapshots), else from the cold hour-partition
  `v_vol_surface/base=<ASSET>/year=/month=/day=/hour=/v_vol_surface.parquet` whose
  hour contains window-start.

Both endpoints come from one pipeline, so the deltas carry no inter-feed noise;
the displayed level also comes from this `now` snapshot. Missing/empty either CSV
(`surface_now.csv`/`surface_open.csv`) degrades gracefully — the deltas read `n/a`
and `recap.py` falls back to the hot `surface.csv` for the displayed values. The
table is capped to the front `MAX_SURFACE_ROWS` expiries.

## Known hot-data quirk (important)

`hot__recap_aggregates_5m_24h.parquet` `volume` rows have **inconsistent units**
and **aggregate rows** that, summed naively, produced an absurd Volume (~$9.8T)
in early versions:

- `volume` carries a per-exchange **aggregate row** (blank `optionType`) whose
  `notional` double-counts, and `volume_sum` units differ by venue
  (Deribit/Bullish in BTC, OKX/Bybit in contracts).

`recap.py` defends: Volume/P-C are derived from **Deribit only** (contracts ×
spot), dropping the blank-`optionType` aggregate rows. This is pinned by
regression tests (`test_recap.py`). (The old hot `block` row_type — which had its
own unit-corrupt rows — is no longer read at all: Biggest Print + Block Flow now
come from the Paradigm block tape, where notional is already USD per leg.)

On a hot miss (DuckDB fails / CSVs absent) it degrades: affected sections read
`No data` and the output is prefixed `⚠ hot surface unavailable`. It never fabricates.

## Performance

- Mechanical path (STS + DuckDB ‖ Deribit + compute + render): ~1.3s.
- DuckDB runs in a thread concurrent with the Deribit fetch (both network-bound).
- Trade pagination is concurrent and time-sliced (no serial cursor backfill).
- End-to-end `/recap` is ~6s; the remainder is model per-turn latency, not the skill.

## Testing

Stdlib-only, no network/S3. Run in CI on any change under `skills/options-recap/`
via `.github/workflows/options-recap-tests.yml`:

```bash
python3 tests/test_vol_math.py    # 156 checks — the math formulas + tape parsing
                                    #   (parse_tape_description) and block ranking/
                                    #   rollup (build_tape_blocks: Σ-per-block
                                    #   notional, RFQ clip rollup, IV lookup)
python3 tests/test_recap.py       # 207 checks — orchestrator: window parsing,
                                    #   hot-CSV ingest, the volume-corruption guard,
                                    #   block tape → Biggest Print/Block Flow (multi-
                                    #   venue, venue column, freshness stamp),
                                    #   assembly, vol-surface deltas, rendering
python3 tests/test_run_recap.py   # 39 checks — run_recap.sh arg normalization
                                    #   (asset/window resolution, "options" keyword
                                    #   strip) via the RECAP_PRINT_ARGS hook
```

LLM output-format evals live in `evals/evals.json` and run via `run_evals.py`
(the `evals` CI job, gate ≥0.8). Fixture-backed eval 5 injects
`evals/fixtures/btc_8h_2026-06-05.json`.

## Versioning

`metadata.version` in `SKILL.md` moves once per branch/PR, not per in-branch
commit. The size of the bump follows the change: a **patch** for fixes/tweaks, a
**minor** for new content/behaviour. The ΔATM/ΔRR/ΔFly columns + `v_vol_surface`
open-surface read were the minor bump to `1.4`; relocating the output template to
`references/output-format.md` is a no-behaviour structural cleanup, so it's the
**patch** to `1.4.1` (output is byte-identical). Repointing Biggest Print + Block
Flow off the Deribit public API onto the multi-venue Paradigm block tape (S3-only,
adds `via Paradigm/<venue>` + surface-IV lookup, Volume goes
hot-only) is the **minor** bump to `1.12` — same four sections and trigger, no
removed fields. (See the repo `CLAUDE.md` for the minor/major rules.)
