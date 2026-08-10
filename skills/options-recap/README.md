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
fallback when the S3 read fails **or its data is stale** — see Data freshness).

Notes / non-obvious bits:
- **`PRESET` is just a label now.** The canonical windows (`5m 10m 20m 1h 4h 8h
  24h`) set `PRESET=1`, but since every window reads the same rolling file this no
  longer gates the data path — it's retained for the plan/test hook and as an
  observability signal (canonical vs ad-hoc window).
- **Dollar Volume and Activity/P-C span all venues.** See "Data sources" below —
  the `$` Volume line sums the upstream `turnover_usd` column (per-trade USD
  premium, normalized at ingestion), while the unit-free `trade_count` drives the
  multi-venue Activity line and the P/C ratio. On a pre-upgrade recap file (no
  `turnover_usd`) the Volume line falls back to the old Deribit-scoped
  `volume_sum × spot` calc and says so in its label.
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
block tape (Biggest Print + Block Flow). The Deribit public API adds the 7d
realized-vol closes, and serves as the live DVOL/spot fallback when the recap
aggregates are unreadable or stale (see §Data freshness — do not treat
`_fetch_market_fallback` as dead code). `recap.py` reads the `dvol_spot` + `volume` rows from the
recap file. The `row_type` map in `hot__recap_aggregates_5m_24h.parquet` (a single
rolling file of 5-min buckets over the trailing 24h; windowed at query time via
`WHERE bucket_at >= now - window` + aggregation):

| Section | `row_type` | Key columns |
|---|---|---|
| Snapshot DVOL/spot | `dvol_spot` | `metric`, `open`, `close`, `high`, `low` (OHLC: `arg_min(open,bucket_at)` / `arg_max(close,bucket_at)` / `max(high)` / `min(low)`) |
| Snapshot volume/P-C/$Volume | `volume` | `exchange`, `optionType`, `volume_sum`, `turnover_usd`, `notional_usd`, `trade_count` |
| Block Flow (non-Paradigm venues) | `block` | `exchange`, `block_id`, `volume_sum`, `notional_usd` (**premium**, not underlying — see below), `leg_count`, `iv_sum`/`iv_count` |

There is **no `surface` `row_type`** — the vol surface lives in `v_vol_surface`.
Biggest Print / Block Flow are primarily the block tape (below), plus the
`block` rows for venues the tape doesn't broker. `notional` is `notional_usd`.

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

**Venue-tape blocks (`venue_blocks.csv`) — full-market block coverage.** The
recap file's `block` rows carry every block/OTC print off the exchanges' own
feeds (Deribit `block_trade_id`, OKX `blockTdId`, Bullish `otcTradeId`);
`run_recap.sh` groups the **option-kind** rows per `(exchange, block_id)` into
`venue_blocks.csv` (`instrument_kind='option'` in the COPY — a perp/spot OTC
block must never compete in an options recap) with **unit-explicit columns**:
`volume_coin` (Σ leg amounts, coin) and `premium_usd` (Σ premium — kept for
debuggability, **never displayed as notional**: it's ~50–100× below the
underlying-USD basis the block sections use). `recap.py` then:

- **Merges only venues Paradigm never brokers** (`_dedupe_venue_blocks` /
  `_TAPE_BROKERED_VENUES`). A block on a venue Paradigm brokers
  (Deribit/Bullish/Paradex) can appear on *both* the Paradigm tape and the
  exchange's own tape, and the slim tape carries no shared id to dedupe on, so
  those venues are excluded — leaving the venues with zero Paradigm overlap.
  **OKX today** (Bybit has no group id, so it never reaches `block` rows at
  all). This structural exclusion is window-independent and depends on no tape
  column. **Deferred to the Snowflake-off migration (taskwarrior #119):**
  exporting the venue's own block id (`VENUE_BLOCK_TRADE_ID` — Deribit
  `BLOCK-…`, Bullish `otc_trade_id`) onto the slim tape, which then lets *every*
  venue merge by exact id-dedupe with no double-count. That export was
  deliberately **not** bolted onto the live `analytics.trade` dbt model (too
  much blast radius); it lands from S3 with proper CDC dedup under #119.
- **Prices them as `volume_coin × spot`** — underlying-USD, the same basis as the
  tape's `NOTIONAL_VOLUME_USD`, valued at recap-time spot (a disclosed
  approximation vs the tape's trade-time figures). No spot → skipped with a
  warning, never guessed.
- Merges them into the same pool: min-notional filter, Biggest Print candidacy and
  top-N ranking on equal terms. The venue tape carries **no leg geometry**, so they
  render as `<Venue> Block` rows (the venue lives in the structure label — there is
  no per-row venue column) with a `(venue tape)` detail note and `~HH:MM` times
  (5-min bucket resolution); a venue-tape Biggest Print reads `via venue tape`.
  Bybit can never appear here — its feed has an is-block flag but no group id, so
  its blocks are unreconstructable and ride the volume/flow rows.

**Multi-venue representation (truthful + consistent).** The `volume` rows span
Deribit, OKX, Bybit, Bullish. The dollar **Volume** line sums **`turnover_usd`**
across all of them — the pipeline's per-trade USD premium, computed at ingestion
from each venue's own instrument spec (contract multipliers + trade-time index),
so the sum is a true market total with no per-venue logic here. **Activity** and
**P/C** aggregate on the unit-free **`trade_count`** basis as before. `volume_sum`
(venue-native contract units) and `notional_usd` are still never summed across
venues. The "all venues" label is **gated on per-venue completeness**: if any
venue that traded carries only null turnover cells (a partial upstream rollout),
the line falls back to the Deribit-scoped `volume_sum × spot` calc with the
"Deribit only" label rather than present a partial sum as a market total. Same
fallback on a recap file that predates `turnover_usd` entirely (the upgraded
volume.csv COPY fails at bind and the legacy shape stands). Remaining caveat the
gate can't see: for ~24h after the upstream deploy, a venue's EARLY buckets carry
null turnover while its later ones don't, so a technically-complete sum still
under-counts until the retained series turns over — upstream cannot backfill
those values (they only exist from ingestion onward). **No venue contract
multipliers are hardcoded anywhere.**

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

`recap.py` defends: dollar Volume sums only the normalized `turnover_usd` column
(never raw `notional`), dropping the blank-`optionType` aggregate rows, and falls
back to a **Deribit-only** contracts × spot calc when the column is absent. This
is pinned by regression tests (`test_recap.py`). (The old hot `block` row_type —
which had its own unit-corrupt rows — is no longer read at all: Biggest Print +
Block Flow now come from the Paradigm block tape, where notional is already USD
per leg.)

On a hot miss (DuckDB fails / CSVs absent) it degrades: affected sections read
`No data` and the output is prefixed `⚠ hot surface unavailable`. It never fabricates.

## Data freshness

A hot **miss** was always handled. A hot source that is present but **stale** was
not, and that is a different failure: the recap aggregates froze on 2026-07-10 and
kept rendering July 10 DVOL/spot as current until the 2026-08-04 deploy — about
3.5 weeks. The object's mtime kept changing while its contents did not, so every
"is it still running?" check passed. Only comparing a data timestamp against the
clock catches it.

`run_recap.sh` writes one probe CSV **per source** (`freshness_rec.csv`,
`freshness_vs.csv`) — the newest timestamp that source carries, deliberately
**not** window-filtered (a source frozen before window-start returns zero
windowed rows, which is indistinguishable from a quiet market). One file per
source, not one `UNION ALL`: a single COPY spanning both means either read
failing writes zero bytes and silently disables the gate for *both*.

For `recap_aggregates` the probe takes the **min over per-(exchange, metric)
maxima**, not a flat max. `row_type='dvol_spot'` is two series; a flat max
reports the freshest, so a dead DVOL scraper hides behind a live spot ticker.

`recap.py` classifies each source into one of three states:

| state | meaning |
|---|---|
| fresh | read fine, within limit |
| `stale` | read fine, lag exceeds the limit |
| `unknown` | the probe yielded no usable timestamp — freshness **cannot** be asserted |

`unknown` does not fail open. It is reachable when a COPY errors, the SQL alias
is renamed, or a value will not parse, and each of those used to leave the
source simply absent — read downstream as "nothing to report", i.e. a silent
all-clear. Limits, against lags measured on a healthy pipeline (2026-08-09):

| source | healthy lag | limit |
|---|---|---|
| `recap_aggregates` | 13m15s — 5-min buckets, open bucket unpublished | 45m |
| `vol_surface` | 8m30s | 45m |

The banner leads the output because it is the only warning that says the numbers
may be *wrong* rather than missing — `No data` is self-evident to a reader,
stale DVOL is not. **What follows the banner differs by source**, and the
consequence is spelled out per source rather than left generic:

- **`recap_aggregates`** — DVOL/spot divert to the live Deribit fallback. If
  that fetch fails the stale figures are retained rather than blanked (figures
  plus a banner beat no figures) and the banner says `could NOT be re-sourced`
  instead of `re-sourced live from Deribit`, so the two outcomes are
  distinguishable on screen. `$ Volume`, Activity, P/C and venue Block Flow come
  from the *same* parquet and are windowed by `bucket_at`, so a partial freeze
  truncates them: they cover only up to the freeze while the header claims the
  full window. The Snapshot divert does not help them, so the banner discloses
  the truncation explicitly.
- **`vol_surface`** — banner only. `_SNAPSHOT_SOURCES` covers `recap_aggregates`
  alone, so a stale surface triggers no fetch of its own (the Deribit ticker
  surface can still backfill it when the fallback runs for another reason); ATM/RR/Fly,
  skew, term and the Δ columns all come from the frozen snapshot (with the Δs
  computed between two frozen readings, so they read flat).

Diverting requires dropping the stale keys from `hot`, not merely fetching the
replacement — `build()` reads `hot['dvol']` first and consults the Deribit series
only when it is absent, so leaving them in place renders the old numbers anyway.
That is `drop_stale_snapshot_fields()`.

**Only heartbeat sources are probed.** `dvol_spot` rows publish every 5 min and
vol-surface snapshots every minute regardless of trading activity, so a gap is
unambiguously a fault. Event-driven sources are excluded and must stay excluded:
the block tape's newest trade depends on whether anyone traded — venue `block`
rows were measured 1h13m behind on a perfectly healthy pipeline because only 29
blocks printed in 24h. Once the tape carries a pipeline-stamped `generated_at`,
that column (not the trade time) is the right thing to add.

## Performance

- Mechanical path (STS + DuckDB ‖ Deribit + compute + render): ~1.3s.
- DuckDB runs in a thread concurrent with the Deribit fetch (both network-bound).
- Trade pagination is concurrent and time-sliced (no serial cursor backfill).
- End-to-end `/recap` is ~6s; the remainder is model per-turn latency, not the skill.

## Testing

Stdlib-only, no network/S3. Run in CI on any change under `skills/options-recap/`
via `.github/workflows/options-recap-tests.yml`:

```bash
python3 tests/test_vol_math.py    # 166 checks — the math formulas + tape parsing
                                    #   (parse_tape_description) and block ranking/
                                    #   rollup (build_tape_blocks: Σ-per-block
                                    #   notional, RFQ clip rollup, IV lookup)
python3 tests/test_recap.py       # 327 checks — orchestrator: window parsing,
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
