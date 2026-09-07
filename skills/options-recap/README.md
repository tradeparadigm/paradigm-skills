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
                    ├── ingests the DuckDB CSVs + blocks.csv (the block tape)
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
volume/`trade_count` rows come from the `market_aggregates_5m/` partitions (one
object per 5-min bucket), read as one glob per UTC day the window touches and
windowed at query time by `WHERE bucket_at >= now - window` + aggregation; the
vol surface + ΔATM/ΔRR/ΔFly come from the normalized per-venue option summaries
at the same 5-min grain; and Biggest Print / Block Flow come from the multi-venue
Paradigm block tape (the `paradigm_trade` rows), scanned in the same DuckDB session. The
Deribit public API adds only the 7d realized-vol closes (and a live DVOL/spot
fallback when the S3 read fails **or its data is stale** — see Data freshness).

Notes / non-obvious bits:
- **`PRESET` is just a label now.** The canonical windows (`5m 10m 20m 1h 4h 8h
  24h`) set `PRESET=1`, but since every window reads the same partitions this no
  longer gates the data path — it's retained for the plan/test hook and as an
  observability signal (canonical vs ad-hoc window).
- **Dollar Volume and Activity/P-C span all venues.** See "Data sources" below —
  the `$` Volume line sums the upstream `turnover_usd` column (per-trade USD
  premium, normalized at ingestion), while the unit-free `trade_count` drives the
  multi-venue Activity line and the P/C ratio. On partitions predating that
  column the Volume line falls back to the old Deribit-scoped
  `volume_sum × spot` calc and says so in its label.
- **The old bug:** a preset `case` mapped unknown windows to a silent 8h default,
  so surface deltas were computed against an 8h-old open. Fixed by parsing the
  window into seconds instead of enumerating presets.
- **The 24h cap is now a product limit, not a data one.** It used to be the
  retention of the rolling recap-aggregates object. The `market_aggregates_5m/`
  partitions retain about two months, and the block tape 30 days, so nothing in
  the data forces the clamp any more. It stays because `/recap` is documented as
  a ≤24h command and lifting it needs a wider read plan than two day globs (see
  "Reading the partitions" below) — a deliberate change, not a side effect of
  this one. `run_recap.sh` still caps at 24h and discloses it; for windows >24h
  `build()` also sets a `hot_horizon` field and `render_md` prepends a one-line
  banner scoped to the Snapshot sections.
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

Four S3 stores in one DuckDB session, all on `dt-exchange-venue-data`: the 5-min
market aggregates (DVOL/spot, volume, activity/P-C, venue blocks), the normalized
per-venue option summaries (surface + Δ), the instrument specs (contract sizes),
and the Paradigm block tape (Biggest Print + Block Flow). The Deribit public API
adds the 7d realized-vol closes, and serves as the live DVOL/spot fallback when the
aggregates are unreadable or stale (see §Data freshness — do not treat
`_fetch_market_fallback` as dead code).

### Why not the `hot/` rollups

`hot__recap_aggregates_5m_24h.parquet` and `v_vol_surface/_hot.parquet` are
convenience rollups — single objects clobbered in place, each a re-publication of
a partitioned store that already lives in the same bucket. Reading the rollup
buys one S3 GET and costs a whole class of failure: a producer that stops
re-publishing leaves a plausible, complete, WRONG object at a stable key with a
fresh mtime. That is exactly the 2026-07-10 freeze, which rendered July 10
DVOL/spot as current for ~3.5 weeks with every "is it running?" check passing.
The partitioned stores cannot fail that way — a stalled producer stops CREATING
objects, so the gap is visible in the key space rather than hidden inside a file.
They are also the rollups' own inputs, so nothing is lost by reading them
directly, and the reads come out slightly FRESHER: the aggregate partitions land
~5 minutes after each bucket closes, where the rollup adds its own republish lag
(measured ~10 minutes behind the partitions on a healthy pipeline).

Two things had to move with the data, because the rollup was doing them on the
way in:

- **Contract sizes.** The rollup scaled `volume_sum` and converted `notional` to
  USD per venue. Reading the raw aggregates means applying `meta/instruments`
  here instead — from the authoritative source, not a constant baked into the
  script. OKX is the venue that matters (`contract_size` 0.01): unscaled, its
  blocks price 100× too high and take over Biggest Print. See "Contract specs"
  below for the two different join semantics and why they differ.
- **`notional` is named `notional` upstream, and is venue-native.** The rollup
  published it as `notional_usd`. `run_recap.sh` reproduces that conversion so
  the CSV contract is unchanged, but note that `recap.py` reads neither column —
  underlying notional for a venue block is `volume_coin × spot`.

### 5-min market aggregates

`s3://dt-exchange-venue-data/market_aggregates_5m/market_aggregates_5m__<YYYYMMDD>T<HHMM>00Z.parquet`
— one object per 5-min bucket, ~2 months retained, same `row_type` discriminator
as the rollup. `recap.py` reads the `dvol_spot` + `volume` rows; `run_recap.sh`
rolls the `block` rows into `venue_blocks.csv`:

| Section | `row_type` | Key columns |
|---|---|---|
| Snapshot DVOL/spot | `dvol_spot` | `metric`, `open`, `close`, `high`, `low` (OHLC: `arg_min(open,bucket_at)` / `arg_max(close,bucket_at)` / `max(high)` / `min(low)`) |
| Snapshot volume/P-C/$Volume | `volume` | `exchange`, `optionType`, `volume_sum`, `turnover_usd`, `notional_usd`, `trade_count` |
| Block Flow (non-Paradigm venues) | `block` | `exchange`, `block_id`, `volume_sum`, `notional_usd` (**premium**, not underlying — see below), `leg_count`, `iv_sum`/`iv_count` |

There is **no `surface` `row_type`** — the vol surface comes from the option
summaries (below). Biggest Print / Block Flow are primarily the block tape,
plus the `block` rows for venues the tape doesn't broker. The upstream column is
`notional` (venue-native premium); `run_recap.sh` emits it as the USD `notional`
/ `premium_usd` the CSV contract has always carried.

### Reading the partitions

One glob **per UTC day** the window touches — at most two, since the window is
capped at 24h — loaded once into a staging table that every COPY then reads.
Three constraints shape that:

- **Day globs, not hour globs.** DuckDB treats a glob matching zero objects as an
  error, and an hour-level pattern is legitimately empty for the first ~5 minutes
  of every hour. A day-level pattern is empty only in the first minutes of a UTC
  day, which the per-glob statement split below already covers. Day globs are
  also much faster: the cost is LIST round-trips, and 25 hourly globs measured
  ~10.5s against ~3.7s for two daily ones.
- **One INSERT per glob.** The DuckDB CLI does not stop on error (`-bail` is
  off), so a day or hour with no objects costs exactly that slice, not the run.
  Verified against real history: a 24h window pinned to 2026-07-10 12:00 UTC,
  whose start day predates the store entirely, still renders from the day that
  does exist.
- **Stage once, then COPY from tables.** Every COPY re-reading the parquet was
  free against one rollup object; it is ~300 objects per statement against the
  partitions.

Schema drift is live, not hypothetical — `underlying_price` is present on most
aggregate objects and absent from the newest — so the aggregate load is hardened
both ways: `union_by_name=true` unifies across objects within a glob, and the
explicit projection is unioned with a zero-row `SELECT * FROM agg WHERE false`
template that supplies any DROPPED column while the projection discards any ADDED
one. `INSERT … BY NAME SELECT *` alone handles only the first case; an added
upstream column is a hard binder error that would take the whole Snapshot down.

`SET threads TO 32` is deliberate oversubscription. A 24h window is ~576 objects
of ~15KB, so the scan is round-trip-bound: measured end-to-end at 4/8/16/32
threads it runs 38.9s / 23.8s / 15.8s / 12.9s from outside the region — near-perfect
inverse scaling, the signature of latency rather than work. The default (one
thread per core) would tie read time to the pod's CPU allocation.

### Contract specs

`s3://dt-exchange-venue-data/meta/instruments/exchange=<venue>/currency=<ccy>/instruments__<venue>__<ccy>__<TS>.parquet`
— `contract_size` and `price_unit` per instrument, uniform per venue. Read for
the previous full UTC hour (they are written at ~HH:05, so the previous hour is
always published) and reduced to one row per venue.

Venues are enumerated in the script rather than globbed with `exchange=*`: a
wildcard in the DIRECTORY component makes DuckDB list the whole `meta/instruments/`
tree, measured at ~12s against ~0.5s for the explicit keys.

The two consumers join differently, on purpose:

- `volume.csv` — **LEFT JOIN, defaulting to `contract_size` 1.0 / a USD price
  unit.** Every field `recap.py` reads is safe at that default: `turnover_usd`
  and `trade_count` need no scaling, and `volume_sum` is summed for Deribit only
  (`contract_size` 1.0). It is also the true spec for the two venues whose
  metadata publishes irregularly — bullish and deribit-usdc are both 1.0 /
  `quote_usd` — so a missing spec row changes nothing for them. A missing spec
  must not drop a venue's Activity/P-C contribution.
- `venue_blocks.csv` — **INNER JOIN.** Here the multiplier is load-bearing: these
  blocks are priced `volume_coin × spot` and ranked against the Paradigm tape, so
  an unscaled OKX block reads 100× its true size. A venue whose specs could not be
  read is DROPPED rather than assumed 1.0 — the same fail-toward-exclusion rule
  the venue dedupe uses, and the same trade: a missed block, never an invented
  one. Only deribit and okex-options emit block rows today, and both publish
  specs hourly.

**Block tape (Biggest Print + Block Flow).**
`s3://dt-exchange-venue-data/hot/hot__paradigm_trade_tape_30d.parquet`
(`row_type='paradigm_trade'`) — the Snowflake-free tape, built by the
`exchange-venue-data` paradigm-trade CronJob from the Airbyte→S3 UM landing. Leg
grain, **trailing 30 days**, read fresh per recap and windowed by `traded_at` in
`run_recap.sh`.

It REPLACED `s3://dt-paradigm-data/paradigm_data/paradigm_trade_tape_slim.csv.gz`
(a flat csv.gz spanning all dates). That read was removed in this PR: data#712
decommissioned its producer on 2026-08-10, so it froze that day and returns zero
rows for any recent window — it could no longer act as a fallback, only mask a
failure of the hot-tape read. There is now no fallback at all, which is why an
empty result renders as Block Flow MISSING rather than quiet. Two differences
matter when reading this section: the horizon is 30
days rather than all history, and the parquet adds `VENUE_BLOCK_TRADE_ID` (the
venue's own block id), which is what lets `recap.py` dedupe venue-tape blocks
against the Paradigm tape exactly rather than by the structural brokered-venue
exclusion.

The tape spans every venue Paradigm brokers (`DBT`/`PRDX`/`BLSH`/…) with USD
notional **per leg** (`NOTIONAL_VOLUME_USD`) and the structure named in
`DESCRIPTION`, so `vol_math` does no cross-venue $ normalization and no
instrument-name inference. `vol_math` groups it two ways: by `BLOCK_TRADE_ID` (a
block; Σ per-leg notional → the Biggest Print is the single largest) and by
`RFQ_ID` (a worked order; its blocks roll into one Block Flow row with a `Blocks`
count). Columns used: `DATE`, `TIME`, `PRODUCT` (→ asset + venue), `DESCRIPTION`,
`QTY`, `SIDE`, `NOTIONAL_VOLUME_USD`, `RFQ_ID`, `BLOCK_TRADE_ID`,
`VENUE_BLOCK_TRADE_ID`. The tape carries **no IV** — the top blocks' IV is looked
up from the vol surface (Deribit legs only). See the `paradigm-data-discovery`
skill for the tape schema and the `paradigm-block-analyst` skill for the
`DESCRIPTION` grammar.

**Venue-tape blocks (`venue_blocks.csv`) — full-market block coverage.** The
aggregates' `block` rows carry every block/OTC print off the exchanges' own
feeds (Deribit `block_trade_id`, OKX `blockTdId`, Bullish `otcTradeId`);
`run_recap.sh` groups the **option-kind** rows per `(exchange, block_id)` into
`venue_blocks.csv` (`instrument_kind='option'` in the COPY — a perp/spot OTC
block must never compete in an options recap) with **unit-explicit columns**:
`volume_coin` (Σ leg amounts, coin) and `premium_usd` (Σ premium — kept for
debuggability, **never displayed as notional**: it's ~50–100× below the
underlying-USD basis the block sections use). `recap.py` then:

- **Exact id dedupe where the venue has proved it.** A venue-tape block whose
  `block_id` matches a `VENUE_BLOCK_TRADE_ID` on the Paradigm tape is the same
  print and is dropped; a genuinely non-Paradigm block on that venue merges.
  The structural brokered-venue exclusion is still the FALLBACK, and every
  guard fails toward it:
  - ids are scoped per venue (independent, often numeric, id spaces);
  - a venue only merges once EVERY one of its ids on the Paradigm tape has found
    a counterpart in the venue tape — otherwise a benign format difference (`BLOCK-280624` vs
    `280624`) would match nothing and merge everything, doubling the headline;
  - any tape block row on a venue without a venue id, or an unparseable
    `PRODUCT`, gates that venue (or all of them) back to structural;
  - block rows are keyed `BLOCK_TRADE_ID or TRADE_ID`, matching `vol_math`.

  So the worst case is the pre-id behaviour — a non-Paradigm block is missed —
  and a double count needs BOTH a format regression and full-coverage proof to
  have been granted, which the coverage rule is designed to withhold in exactly
  that case. OKX is never brokered, so it always merges. Bybit can never appear
  at all — its feed has an is-block flag but no group id, so its blocks are
  unreconstructable and ride the volume/flow rows instead.
- **Prices them as `volume_coin × spot`** — underlying-USD, the same basis as the
  tape's `NOTIONAL_VOLUME_USD`, valued at recap-time spot (a disclosed
  approximation vs the tape's trade-time figures). No spot → skipped with a
  warning, never guessed.
- Merges them into the same pool: min-notional filter, Biggest Print candidacy and
  top-N ranking on equal terms. The venue tape carries **no leg geometry**, so they
  render as `<Venue> Block` rows (the venue lives in the structure label — there is
  no per-row venue column) with a `(venue tape)` detail note and `~HH:MM` times
  (5-min bucket resolution); a venue-tape Biggest Print reads `via venue tape`.


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
fallback on partitions that predate `turnover_usd` entirely (the column arrives
as NULL through the schema-drift template and contributes nothing, so `tus` is
empty and `build()` takes the Deribit-scoped branch). Remaining caveat the
gate can't see: for ~24h after the upstream deploy, a venue's EARLY buckets carry
null turnover while its later ones don't, so a technically-complete sum still
under-counts until the retained series turns over — upstream cannot backfill
those values (they only exist from ingestion onward). **No venue contract
multipliers are hardcoded anywhere.**

The "now" values (latest DVOL/spot close, current surface) come from the newest
`bucket_at` in the aggregate partitions and the newest option-summary bucket.
(`hot__market_signals_1m.parquet` is the live signals heartbeat used by
`paradigm-block-analyst`; `/recap` no longer reads it.)
S3 access (IRSA STS bootstrap) is documented in the `paradigm-data-discovery` skill.

### Vol surface + deltas (ΔATM/ΔRR/ΔFly)

The aggregate partitions carry no surface rows, so the full surface and the
window-over-window deltas read the normalized per-venue option summaries:

`s3://dt-exchange-venue-data/normalized/exchange=deribit/data_type=option_summary/currency=<ccy>/level=5m/year=/month=/day=/hour=/start_minute=/…__agg__….parquet`

This is the upstream that the consolidated `v_vol_surface` store on
`dt-paradigm-data` was itself derived from; `markIV_close`/`delta_close` per
instrument `symbol` is exactly the `(symbol, mark_iv, delta)` triple `recap.py`
consumes. Reading it directly collapses the old hot/cold split:

- **now** = the newest 5-min bucket in the current or previous hour partition.
- **open** = the bucket nearest window-start, from the hour partition holding it,
  tolerance-guarded to 15 minutes.

Both publish on the same ~5-minute cadence, so `open` no longer waits ~1h for an
hourly cold partition to close — which is what used to degrade every Δ column to
`n/a` for windows just over an hour. Both endpoints still come from one pipeline,
so the deltas carry no inter-feed noise, and the displayed level comes from the
same `now` bucket. Missing/empty either CSV (`surface_now.csv`/`surface_open.csv`)
degrades gracefully — the deltas read `n/a`. The table is capped to the front
`MAX_SURFACE_ROWS` expiries.

**OTM only.** The read filters to strikes beyond the underlying, which is what
`v_vol_surface` published and therefore what this section has always been
computed from. The venue chain also lists every ITM mirror; by put-call parity
those carry the same strike and a call-delta that collides with the OTM leg's, so
admitting them would silently overwrite half the interpolation grid with the
other side's mark. It is expressed as strike-vs-`underlyingPrice_close` rather
than a delta cutoff so it states the actual rule instead of approximating it.
Measured against the old path on the same window: 25Δ RR and ATM within 0.4v,
Fly within 0.2v, identical skew and term labels — the residual is the 5-min
bucket close versus the old 1-min snapshot.

## Known upstream quirk (important)

The aggregates' `volume` rows have **inconsistent units** and **aggregate rows**
that, summed naively, produced an absurd Volume (~$9.8T) in early versions:

- `volume` carries a per-exchange **aggregate row** (blank `optionType`) whose
  `notional` double-counts, and `volume_sum` units differ by venue
  (Deribit/Bullish in BTC, OKX/Bybit in contracts).

`recap.py` defends: dollar Volume sums only the normalized `turnover_usd` column
(never raw `notional`), dropping the blank-`optionType` aggregate rows, and falls
back to a **Deribit-only** contracts × spot calc when the column is absent. This
is pinned by regression tests (`test_recap.py`). Biggest Print + Block Flow come
from the Paradigm block tape, where notional is already USD per leg; the `block`
rows only supply venues the tape doesn't broker, scaled by `contract_size` (see
"Contract specs" above).

On a source miss (DuckDB fails / CSVs absent) it degrades: affected sections read
`No data` and the output is prefixed `⚠ hot surface unavailable`. It never fabricates.

## Data freshness

A **miss** was always handled. A source that is present but **stale** was
not, and that is a different failure: the recap aggregates froze on 2026-07-10 and
kept rendering July 10 DVOL/spot as current until the 2026-08-04 deploy — about
3.5 weeks. A single rolling object was being clobbered in place, so its mtime kept
changing while its contents did not and every "is it still running?" check passed.
Only comparing a data timestamp against the clock catches it.

Reading the partitioned stores removes that specific trap — a stalled producer
stops creating objects, so the gap shows in the key space — but the gate stays,
and is now stricter: both probes read the same staging tables the output is
rendered from, so probe and payload cannot drift apart, and a producer that has
stopped writing eventually leaves those tables empty, which surfaces as `unknown`
rather than as a plausible stale number.

`run_recap.sh` writes one probe CSV **per source** (`freshness_rec.csv`,
`freshness_vs.csv`) — the newest timestamp that source carries, deliberately
**not** window-filtered (a source frozen before window-start returns zero
windowed rows, which is indistinguishable from a quiet market). This is why the
staging tables are loaded un-windowed and the window is applied per COPY. One
file per source, not one `UNION ALL`: a single COPY spanning both means either
read failing writes zero bytes and silently disables the gate for *both*.

For `recap_aggregates` the probe takes the **min over per-metric maxima**, not a
flat max. `row_type='dvol_spot'` is two series; a flat max reports the freshest,
so a dead DVOL scraper hides behind a live spot ticker. It groups by `metric`
alone, never by `exchange` — `load_hot` collapses every venue to Deribit's
reading, so grouping by exchange would measure a superset of what is rendered and
cry wolf whenever a sparse secondary venue lags.

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
| `recap_aggregates` | ~13m — 5-min buckets, open bucket unpublished | 45m |
| `vol_surface` | ~7m — 5-min buckets, published ~5m after each closes | 45m |

The banner leads the output because it is the only warning that says the numbers
may be *wrong* rather than missing — `No data` is self-evident to a reader,
stale DVOL is not. **What follows the banner differs by source**, and the
consequence is spelled out per source rather than left generic:

- **`recap_aggregates`** — DVOL/spot divert to the live Deribit fallback. If
  that fetch fails the stale figures are retained rather than blanked (figures
  plus a banner beat no figures) and the banner says `could NOT be re-sourced`
  instead of `re-sourced live from Deribit`, so the two outcomes are
  distinguishable on screen. `$ Volume`, Activity, P/C and venue Block Flow come
  from the *same* store and are windowed by `bucket_at`, so a partial freeze
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

**Only heartbeat sources are probed.** `dvol_spot` rows and option summaries both
publish every 5 min regardless of trading activity, so a gap is
unambiguously a fault. Event-driven sources are excluded and must stay excluded:
the block tape's newest trade depends on whether anyone traded — venue `block`
rows were measured 1h13m behind on a perfectly healthy pipeline because only 29
blocks printed in 24h. Once the tape carries a pipeline-stamped `generated_at`,
that column (not the trade time) is the right thing to add.

## Performance

- DuckDB runs in a thread concurrent with the Deribit fetch (both network-bound).
- Trade pagination is concurrent and time-sliced (no serial cursor backfill).
- End-to-end `/recap` is ~6s; the remainder is model per-turn latency, not the skill.
- The DuckDB session moved from 3 single-object reads to ~100–580 partition
  objects. That trade buys freshness and removes the clobbered-rollup failure
  mode; it costs LIST/GET round-trips, which is why the reads are day-globbed,
  staged once into tables, and run at `threads 32` (see "Reading the partitions").
  Round-trip cost dominates, so the in-region figure is far below the
  out-of-region measurements quoted there — re-measure from the pod before
  treating any number here as the pod's.

## Testing

Stdlib-only, no network/S3. Run in CI on any change under `skills/options-recap/`
via `.github/workflows/options-recap-tests.yml`:

```bash
python3 tests/test_vol_math.py    # 166 checks — the math formulas + tape parsing
                                    #   (parse_tape_description) and block ranking/
                                    #   rollup (build_tape_blocks: Σ-per-block
                                    #   notional, RFQ clip rollup, IV lookup)
python3 tests/test_recap.py       # 379 checks — orchestrator: window parsing,
                                    #   CSV ingest, the volume-corruption guard,
                                    #   block tape → Biggest Print/Block Flow (multi-
                                    #   venue, venue column, freshness stamp),
                                    #   assembly, vol-surface deltas, rendering
python3 tests/test_run_recap.py   # 70 checks — run_recap.sh: arg normalization
                                    #   (RECAP_PRINT_ARGS), window parsing
                                    #   (RECAP_PRINT_PLAN), partition resolution
                                    #   (RECAP_PRINT_SOURCES), and the generated
                                    #   DuckDB session (RECAP_PRINT_SQL — which
                                    #   stores are read, the drift/gap guards, and
                                    #   the join semantics per COPY)
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
removed fields. Moving every read off the clobbered-in-place `hot/` rollups onto
the partitioned stores they are built from is the **minor** bump to `1.16`: same
four sections, same fields, same trigger — only where the numbers come from
changes. (See the repo `CLAUDE.md` for the minor/major rules.)
