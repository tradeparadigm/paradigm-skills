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

## Windows — presets vs. dynamic

`run_recap.sh` parses the window generically into seconds (`Nm`/`Nh`/`Nd`), so
**any** window renders. There are two paths:

- **Presets** (`5m 10m 20m 1h 4h 8h 24h`) — a server-side `hot__recap_<win>.parquet`
  is pre-aggregated, so DVOL/spot OHLC, volume, and the close-surface come from one
  fast S3 read. `PRESET=1` in the script gates those COPYs.
- **Dynamic** (any other window, e.g. `3h`, `90m`) — no pre-baked parquet exists,
  so `recap.py` reconstructs the same sections live: DVOL/spot OHLC from the
  Deribit index/chart APIs over `[start,end]`, Volume/P-C from the Deribit window
  tape (`deribit_tape_volume`), and the vol surface + ΔATM/ΔRR/ΔFly from
  `v_vol_surface` (the two `surface_now`/`surface_open` COPYs run for **every**
  window). Slower (more Deribit round-trips) but the output shape is identical.

Notes / non-obvious bits:
- **Volume/P-C come from the live Deribit tape for EVERY window** (screen + block,
  i.e. incl. Paradigm) — `deribit_tape_volume`, not the hot `volume` parquet. The
  hot rows undercount by ~25%: they drop most block flow despite the recap's
  "incl. Paradigm" label (8h sample: tape 6712 BTC / 4202 trades vs hot 5059 /
  3129), which made volume non-monotonic across the preset/dynamic boundary (a 3h
  window reading more than a 4h one). The tape is already fetched for Block Flow,
  so this is free. The hot `volume` rows are now only an empty-tape fallback (e.g.
  a Deribit fetch failure). Root cause is the server-side `hot__recap` bake
  dropping blocks; if fixed there, presets could read the parquet again.
- **The old bug:** a preset `case` mapped unknown windows to a silent 8h default,
  so `hot__recap_3h.parquet` was read (missing → n/a Snapshot) and surface deltas
  were computed against an 8h-old open. Fixed by parsing instead of enumerating.
- **~24h flow horizon.** Volume / Biggest Print / Block Flow come from the Deribit
  public tape, which only retains ~24h (empirically confirmed: both `get_last_trades*`
  endpoints return 0 rows for ranges older than ~24h). DVOL/spot (OHLC) and the vol
  surface (`v_vol_surface`) retain far longer. So for windows >24h, `build()` sets a
  `flow_horizon` field and `render_md` prepends a one-line banner — the flow sections
  cover ~24h, the rest spans the full window. The >24h data *does* exist in the cold
  store (`paradigm_trade_tape_slim` for blocks, fresh; tardis for screen, ~2d stale)
  but isn't wired in yet — that's the planned follow-up that will retire the banner.
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

Two authoritative hot reads (one DuckDB session) plus the Deribit tape. **Hot
files are authoritative for DVOL/spot/volume/surface;** Deribit supplies only the
7d realized-vol closes and block-leg geometry (hot carries neither). The
`row_type` map in `hot__recap_<window>.parquet`:

| Section | `row_type` | Key columns |
|---|---|---|
| Snapshot DVOL/spot | `dvol_spot` | `metric`, `open`, `close`, `high`, `low` |
| Snapshot volume/P-C | `volume` | `exchange`, `optionType`, `volume_sum`, `notional_usd` |
| Block Flow | `block` | `block_id`, `notional_usd`, `volume_sum`, `leg_count`, `avg_iv` |

The per-strike vol surface is **no longer embedded in the recap**. The canonical
multi-venue surface is the standalone `hot__vol_surface.parquet` (`row_type='strike'`:
`expiry`, `strike`, `optionType`, `mark_iv`, `greek_delta`, `underlying_price`),
consumed by other skills. The recap itself reads its surface from `v_vol_surface`
(see the deltas note below) so the "now" and window-"open" endpoints share one
pipeline. `hot__market_signals_1m.parquet` is the "now" anchor (current DVOL/spot,
ATM IV) for other skills. The recap / signals / surface hot files live under
`s3://dt-exchange-venue-data/hot/`. S3 access (IRSA STS bootstrap) is documented in
the `paradigm-data-discovery` skill.

**Vol-surface deltas (ΔATM/ΔRR/ΔFly).** The standalone surface file is point-in-time
(no window-open snapshot), so window-over-window deltas read the consolidated
per-strike store `v_vol_surface` at
`s3://dt-paradigm-data/paradigm_data/v_vol_surface/` instead (columns `symbol`,
`type`, `mark_iv`, `delta`, `at`, …;
Deribit basis = `symbol LIKE '<ASSET>-%'`, dropping the `<ASSET>_USDC-` legs):

- **now** = the latest snapshot in the rolling `v_vol_surface/_hot.parquet`.
- **open** = the snapshot nearest window-start — from `_hot.parquet` for windows
  ≤1h (it holds ~2h of 1-min snapshots), else from the cold hour-partition
  `v_vol_surface/base=<ASSET>/year=/month=/day=/hour=/v_vol_surface.parquet` whose
  hour contains window-start.

Both endpoints come from one pipeline, so the deltas carry no inter-feed noise;
the displayed level also comes from this `now` snapshot. Missing/empty either CSV
(`surface_now.csv`/`surface_open.csv`) degrades gracefully — the deltas read `n/a`
and the Vol Surface section reads `No data` rather than mixing feeds. The
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
python3 tests/test_run_recap.py   # 10 checks — run_recap.sh arg normalization
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
**patch** to `1.4.1` (output is byte-identical). (See the repo `CLAUDE.md` for the
minor/major rules.)
