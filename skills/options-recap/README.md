# options-recap — maintainer notes

Human/maintainer documentation for the `paradigm-options-recap` skill. This is
**not** loaded into the agent's context — `SKILL.md` is the runbook the agent
follows; everything an operator or contributor needs lives here.

## What it does

`/recap [asset] [window]` produces a fixed four-section options recap (Snapshot,
Biggest Print, Block Flow, Vol Surface) for BTC/ETH over a window (default 24h).
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
        ├── writes /tmp/recap.sql (one DuckDB session, 7 COPY statements → CSVs)
        └── uv run scripts/recap.py --duckdb-sql /tmp/recap.sql --csv-dir /tmp/recap --render
                    │
                    ├── runs DuckDB in a thread  ─┐  (concurrent — both are
                    ├── fetches Deribit tape      ─┘   network-bound)
                    │     • 7d hourly closes (realized vol)
                    │     • window option trades, concurrent time-sliced pagination
                    ├── ingests the hot CSVs
                    ├── vol math via scripts/vol_math.py
                    └── prints the finished four-section markdown
```

- `scripts/run_recap.sh` — the live wrapper (S3 + DuckDB + recap.py).
- `scripts/recap.py` — orchestrator: fetch, ingest, assemble, compute, render.
- `scripts/vol_math.py` — pure vol math (realized-vs-implied, Black-76 flow
  greeks, block clustering/ranking, vol-surface skew/term). No I/O.
- `scripts/recap.py --no-s3 --render` — offline smoke against live Deribit only.
- `references/output-format.md` — the fixed four-section template + formatting
  rules. The live path doesn't read it (the script emits the shape); it's the
  rendering contract for the injected/simulate modes and the eval harness.

Why one command: an instrumented run showed ~86% of wall time was the model
*generating* a ~50-line inline bootstrap+SQL block. Moving it into a wrapper
script (agent types one short line) and pre-rendering the markdown in `recap.py`
(agent relays verbatim) cut end-to-end from ~17s to ~6s; the mechanical path is
~1.3s. See the "Performance" notes below.

## Data sources

Two authoritative hot reads (one DuckDB session) plus the Deribit tape. **Hot
files are authoritative for DVOL/spot/volume/surface;** Deribit supplies only the
7d realized-vol closes and block-leg geometry (hot carries neither). The
`row_type` map in `hot__recap_<window>.parquet`:

| Section | `row_type` | Key columns |
|---|---|---|
| Snapshot DVOL/spot | `dvol_spot` | `metric`, `open`, `close`, `high`, `low` |
| Snapshot volume/P-C | `volume` | `exchange`, `optionType`, `volume_sum`, `notional` |
| Block Flow | `block` | `block_id`, `notional`, `volume_sum`, `leg_count`, `avg_iv` |
| Vol Surface | `surface` | `expiry`, `strike`, `optionType`, `markIV_close`, `delta` |

`hot__market_signals_1m.parquet` is the "now" anchor (current DVOL/spot, ATM IV).
S3 access (IRSA STS bootstrap) is documented in the `paradigm-data-discovery` skill.

**Vol-surface deltas (ΔATM/ΔRR/ΔFly).** The hot recap parquet's `surface` rows are
close-only, so window-over-window deltas read the consolidated per-strike store
`v_vol_surface` instead (columns `symbol`, `type`, `mark_iv`, `delta`, `at`, …;
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

`hot__recap_<window>.parquet` `volume`/`block` rows have **inconsistent units**
and **aggregate/corrupt rows** that, summed naively, produced absurd numbers
(Volume ~$9.8T, a single $5.1B block) in early versions:

- `volume` carries a per-exchange **aggregate row** (blank `optionType`) whose
  `notional` double-counts, and `volume_sum` units differ by venue
  (Deribit/Bullish in BTC, OKX/Bybit in contracts).
- `block` occasionally has a **unit-corrupt row** (`notional` = `volume_sum` ×
  spot on a `volume_sum` that isn't BTC).

`recap.py` defends against both: Volume/P-C are derived from **Deribit only**
(contracts × spot), dropping the blank-`optionType` aggregate rows; Block Flow
totals are derived from the **Deribit tape clustering** that produces the
displayed rows, not from the hot `block` rows. These are pinned by regression
tests (`test_recap.py`). The upstream producer should ideally emit consistent
units — until then, recap.py is the guard.

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
python3 tests/test_vol_math.py    # 54 checks — the math formulas
python3 tests/test_recap.py       # 107 checks — orchestrator: window parsing,
                                    #   hot-CSV ingest, the volume/block corruption
                                    #   guards, assembly, vol-surface deltas,
                                    #   rendering, run_duckdb
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
**patch** to `1.4.1` (output is byte-identical). (See the repo `CLAUDE.md` for the
minor/major rules.)
