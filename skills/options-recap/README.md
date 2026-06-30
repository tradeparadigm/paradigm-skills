# options-recap — maintainer notes

Human/maintainer documentation for the `paradigm-options-recap` skill. This is
**not** loaded into the agent's context — `SKILL.md` is the runbook the agent
follows; everything an operator or contributor needs lives here.

## What it does

`/recap [asset] [window]` produces a fixed four-section options recap (Snapshot,
Biggest Print, Block Flow, Vol Surface) for BTC/ETH over a window (default 24h).
Output shape is fixed and defined in `SKILL.md`.

## Architecture

The live path is **one command** the agent runs, then it relays stdout verbatim:

```
bash scripts/run_recap.sh <ASSET> <WINDOW>
        │
        ├── STS bootstrap (IRSA → temporary S3 creds)
        ├── writes /tmp/recap.sql (one DuckDB session, 5 COPY statements → CSVs)
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
python3 tests/test_recap.py       # 77 checks — orchestrator: window parsing,
                                    #   hot-CSV ingest, the volume/block corruption
                                    #   guards, assembly, rendering, run_duckdb
```

LLM output-format evals live in `evals/evals.json` and run via `run_evals.py`
(the `evals` CI job, gate ≥0.8). Fixture-backed eval 5 injects
`evals/fixtures/btc_8h_2026-06-05.json`.

## Versioning

`metadata.version` in `SKILL.md` is a single **patch** increment over whatever
`main` currently carries — a branch/PR represents one patch release, not a
running count of in-branch edits. `main` is at `1.3`, so this branch is `1.3.1`;
don't bump per-commit. (The repo `CLAUDE.md` describes minor/major bumps for
larger releases.)
