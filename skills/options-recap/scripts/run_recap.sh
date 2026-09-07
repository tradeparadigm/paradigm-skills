#!/usr/bin/env bash
# run_recap.sh — the entire live recap in one command, so the agent types one
# short line instead of regenerating a ~50-line bootstrap+SQL block (that
# generation was ~12s of the old run). Does: STS bootstrap, one DuckDB session
# into CSVs, then recap.py --render. Its stdout IS the final four-section recap.
#
# Usage: bash scripts/run_recap.sh <ASSET> <WINDOW>     e.g. run_recap.sh BTC 8h
#
# SOURCES: this script reads the DURABLE, per-interval partitioned stores that
# the `hot/` rollups are themselves built from — `market_aggregates_5m/` for the
# Snapshot flow and venue blocks, `normalized/…/option_summary/` for the vol
# surface. See the "Why not the hot rollups" note above the SQL. The one
# remaining `hot/` read is the Paradigm block tape, whose upstream (the Airbyte
# UNIFIED_MARKETS landing) is in a bucket this role cannot reach.
set -uo pipefail

# Some users type the no-op keyword "options" (/recap btc options 8h). This skill
# is always options, so drop any "options"/"option" token before assigning
# asset/window — otherwise a stray token lands in the window slot and breaks
# parsing (parse_window_ms raises on it).
ARGS=""
for a in "$@"; do
  case "$(printf '%s' "$a" | tr '[:upper:]' '[:lower:]')" in
    options|option) ;;                       # no-op keyword — drop
    *) ARGS="$ARGS $a" ;;
  esac
done
set -- $ARGS

ASSET=$(printf '%s' "${1:-BTC}" | tr '[:lower:]' '[:upper:]')
CCY=$(printf '%s' "$ASSET" | tr '[:upper:]' '[:lower:]')   # partition key is lowercase
WIN="${2:-8h}"
[ "$WIN" = "1d" ] && WIN=24h                 # exact match — the old substring
                                             # substitution turned 31d into 324h

# Window → seconds, parsed GENERICALLY (Nm/Nh/Nd) so any window works. The 5-min
# aggregate partitions are windowed by bucket_at at query time, so there are no
# per-window files to enumerate. An earlier preset-only `case` silently defaulted
# unknown windows (e.g. 3h) to 8h, so surface deltas were computed against the
# wrong window-open. Parse instead of enumerate.
WL=$(printf '%s' "$WIN" | tr '[:upper:]' '[:lower:]')
WN=${WL%[mhd]}; WU=${WL##*[0-9]}             # magnitude / unit
case "$WU" in
  m) SECS=$((WN * 60));; h) SECS=$((WN * 3600));; d) SECS=$((WN * 86400));;
  *) SECS=0;;
esac
if ! [ "$WN" -gt 0 ] 2>/dev/null || [ "$SECS" -le 0 ]; then
  echo "recap: bad window '$WIN' — use e.g. 30m, 3h, 8h, 24h" >&2; exit 2
fi
# Cap at 24h. This is now a PRODUCT constraint, not a data one: `/recap` is
# documented as a ≤24h command and every window renders one shape. The 5-min
# aggregate partitions retain ~2 months, so lifting the cap is a separate,
# deliberate change (it needs a wider read plan than the two day-globs below) —
# not a side effect of moving off the 24h rolling rollup. Until then, clamp and
# DISCLOSE (the banner line below is part of the recap output).
CAP_NOTE=""
if [ "$SECS" -gt 86400 ]; then
  CAP_NOTE="⚠ window capped at 24h — $WIN exceeds the 24h /recap horizon."
  WIN=24h; SECS=86400
fi
# PRESET flags the canonical windows. Every window reads the same partitions
# (bucket_at-windowed), so PRESET does not gate the data path — it's retained
# for the plan hook below and as an observability signal (canonical vs ad-hoc).
case "$WIN" in
  5m|10m|20m|1h|4h|8h|24h) PRESET=1;; *) PRESET=0;;
esac

# ── Source resolution (pure date math — no creds, no network) ───────────────
# Resolved here, before the STS bootstrap, so the RECAP_PRINT_SOURCES test hook
# can exercise it with no creds. RECAP_NOW_S pins the clock so tests can assert
# exact partition paths.
NOW_S=${RECAP_NOW_S:-$(date -u +%s)}; START_S=$((NOW_S - SECS)); START_MS=$((START_S * 1000))
# Venue `block` rows are bucketed to 5 minutes while the Paradigm tape carries
# exact-ms traded_at. Windowing both on START_MS drops any venue block in the
# first PARTIAL bucket, so a Paradigm print in that bucket has no counterpart,
# full-coverage proof fails, and the whole venue falls back to structural dedupe
# — measured to leave the id merge inactive about half the time from the bucket
# edge alone. Flooring the venue window to the containing bucket removes that
# term. It can only ADD venue rows that were already in-window at 5-minute
# resolution, so it cannot create a double count.
START_MS_5M=$(( (START_S - START_S % 300) * 1000 ))

# date -u portability: GNU takes -d @epoch, BSD takes -r epoch.
utc() { date -u -d "@$1" "+$2" 2>/dev/null || date -u -r "$1" "+$2"; }

# UTC day stamps for the aggregate partitions. A window is capped at 24h, so it
# spans at most two UTC days; dedupe when it spans one.
AGG_DAYS=$(utc "$START_S" %Y%m%d)
D_NOW=$(utc "$NOW_S" %Y%m%d)
[ "$D_NOW" = "$AGG_DAYS" ] || AGG_DAYS="$AGG_DAYS $D_NOW"

# Vol-surface hours. `now` is the freshest published 5-min bucket, so read the
# current hour AND the previous one (the current hour is empty for the first few
# minutes past the top of the hour). The window-OPEN surface comes from the hour
# containing window-start. Deduped — a short window shares hours.
H_NOW=$(utc "$NOW_S" %Y/%m/%d/%H)
H_PREV=$(utc $((NOW_S - 3600)) %Y/%m/%d/%H)
H_OPEN=$(utc "$START_S" %Y/%m/%d/%H)
SURF_HOURS=$(printf '%s\n%s\n%s\n' "$H_OPEN" "$H_PREV" "$H_NOW" | awk '!seen[$0]++')

# Instrument metadata hour. Written at ~HH:05, so the PREVIOUS full hour is
# always published — one read per venue instead of a fallback ladder.
META_HOUR=$(utc $((NOW_S - 3600)) %Y%m%dT%H)

# Testability hooks: echo resolved state and exit before any STS/DuckDB work (no
# creds/network needed). Used by tests/test_run_recap.py.
#   RECAP_PRINT_ARGS → "ASSET WIN"                   (arg normalization)
#   RECAP_PRINT_PLAN → "ASSET WIN SECS PRESET"       (window parsing + preset flag)
#   RECAP_PRINT_SOURCES → "ASSET WIN START_MS <agg days,> <surface-open hour>"
[ -n "${RECAP_PRINT_ARGS:-}" ] && { echo "$ASSET $WIN"; exit 0; }
[ -n "${RECAP_PRINT_PLAN:-}" ] && { echo "$ASSET $WIN $SECS $PRESET"; exit 0; }
[ -n "${RECAP_PRINT_SOURCES:-}" ] && {
  echo "$ASSET $WIN $START_MS $(printf '%s' "$AGG_DAYS" | tr ' ' ',') $H_OPEN"; exit 0; }

DIR="$(cd "$(dirname "$0")/.." && pwd)"      # skill dir (scripts/..)
# Per-invocation workdir. The old fixed /tmp/recap + /tmp/recap.sql were shared
# state: two concurrent recaps (e.g. BTC and ETH fired from separate sessions)
# raced on the SQL file and CSVs, and one recap silently rendered the other's
# asset/window slice (exit 0, no warning). A fresh mktemp dir per run isolates
# them completely; it also supersedes the old stale-CSV wipe — nothing stale can
# exist in a directory this run just created.
WORK=$(mktemp -d "${TMPDIR:-/tmp}/recap.XXXXXX") || { echo "recap: mktemp failed" >&2; exit 1; }
trap 'rm -rf "$WORK"' EXIT

# ── Credentials ────────────────────────────────────────────────────────────
# In the pod: IRSA → STS → temporary keys (see the paradigm-data-discovery
# skill). The inline-shell warning in that skill's s3-access.md is about creds
# an agent assembles across `exec` calls; a committed .sh is one process with
# known shell state, so the STS path is fine here.
# Off-cluster (no projected token — a maintainer running this on a laptop),
# fall back to DuckDB's own CREDENTIAL_CHAIN so the script is runnable and
# testable against real S3 without hand-built creds.
if [ -n "${RECAP_PRINT_SQL:-}" ]; then
  # Test hook (see below): never mint credentials just to print the plan, and
  # never let the printed plan contain any.
  CRED_SQL="-- credentials elided"
elif [ -n "${AWS_WEB_IDENTITY_TOKEN_FILE:-}" ] && [ -r "${AWS_WEB_IDENTITY_TOKEN_FILE}" ]; then
  TOKEN=$(cat "$AWS_WEB_IDENTITY_TOKEN_FILE")
  CREDS=$(curl -s "https://sts.ap-northeast-1.amazonaws.com/?Action=AssumeRoleWithWebIdentity&Version=2011-06-15&RoleArn=${AWS_ROLE_ARN}&RoleSessionName=duckdb&WebIdentityToken=${TOKEN}")
  AK=$(printf '%s' "$CREDS" | grep -o '<AccessKeyId>[^<]*'     | cut -d'>' -f2)
  SK=$(printf '%s' "$CREDS" | grep -o '<SecretAccessKey>[^<]*' | cut -d'>' -f2)
  ST=$(printf '%s' "$CREDS" | grep -o '<SessionToken>[^<]*'    | cut -d'>' -f2)
  CRED_SQL="SET s3_region='ap-northeast-1';
SET s3_access_key_id='${AK}';
SET s3_secret_access_key='${SK}';
SET s3_session_token='${ST}';"
else
  CRED_SQL="INSTALL aws; LOAD aws;
CREATE OR REPLACE SECRET s3_irsa (TYPE S3, PROVIDER CREDENTIAL_CHAIN, REGION 'ap-northeast-1');"
fi

# ── Sources ────────────────────────────────────────────────────────────────
#
# WHY NOT THE `hot/` ROLLUPS. `hot__recap_aggregates_5m_24h.parquet` and
# `v_vol_surface/_hot.parquet` are convenience rollups: single objects clobbered
# in place, each a re-publication of a partitioned store that already lives in
# the same bucket. Reading the rollup buys one S3 GET and costs a whole class of
# failure — a producer that stops re-publishing leaves a plausible, complete,
# WRONG file at a stable key with a fresh mtime (exactly the 2026-07-10 freeze
# that rendered July 10 DVOL as current for ~3.5 weeks). The partitioned stores
# cannot fail that way: a stalled producer stops CREATING objects, so the gap is
# visible in the key space itself rather than hidden inside a file. They are
# also the rollups' own inputs, so nothing is lost by reading them directly.
#
# MA — 5-min market aggregates, ONE OBJECT PER BUCKET, ~2 months retained.
# Same row_type discriminator and (modulo the notes below) same columns as the
# rollup. Read as one day-glob per UTC day the window touches: a day-level
# pattern always matches at least one object, whereas an hour-level pattern is
# empty for the first ~5 minutes of every hour and DuckDB errors on a glob that
# matches nothing. Two globs is also markedly faster than 25 hourly ones — the
# cost here is LIST round-trips, not bytes.
MA=s3://dt-exchange-venue-data/market_aggregates_5m/market_aggregates_5m__
#
# NRM — normalized per-venue option summaries at 5-min grain: the upstream the
# consolidated `v_vol_surface` store is derived from. `markIV_close`/`delta_close`
# per instrument `symbol` is exactly the (symbol, mark_iv, delta) triple recap.py
# consumes. Reading it directly also collapses the old hot/cold split: the
# window-open surface is the 5-min partition containing window-start, published
# on the same ~5-min cadence as `now` — no ~1h wait for an hourly cold partition
# to close, which is what used to degrade ΔATM/ΔRR/ΔFly to n/a.
NRM=s3://dt-exchange-venue-data/normalized/exchange=deribit/data_type=option_summary/currency=${CCY}/level=5m
#
# MET — instrument specs (contract_size, price_unit) per venue. The rollup
# applied these to `volume_sum`/`notional` on the way in; reading the raw
# aggregates means applying them here instead, from the authoritative source
# rather than a constant baked into this script. OKX is the venue that matters:
# contract_size 0.01, so an unscaled `volume_sum` prices its blocks 100× too
# high and they swamp Biggest Print. Venues are enumerated (not `exchange=*`)
# because a wildcard in the DIRECTORY component makes DuckDB list the whole
# `meta/instruments/` tree — measured at ~12s versus ~0.5s for these.
MET=s3://dt-exchange-venue-data/meta/instruments
MET_VENUES="deribit deribit-usdc okex-options bybit-options bullish"
#
# PT — multi-venue Paradigm block tape: the SOLE source for Biggest Print +
# Block Flow. Leg grain, trailing 30d, USD notional PER LEG, the structure named
# in DESCRIPTION, plus VENUE_BLOCK_TRADE_ID (the venue's own block id) for exact
# dedupe against the venue tapes.
#
# THIS IS THE ONE ROLLUP STILL READ, and not by choice: it is built from the
# Airbyte UNIFIED_MARKETS_TRADE_PROD landing in `paradigm-airbyte-data-us-east-1`,
# which this pod's IAM role cannot read (it is granted dt-exchange-venue-data,
# dt-paradigm-data and dt-paradex-data only). Repointing it needs an IAM grant,
# not a script change. The Snowflake-egressed csv.gz that used to back it froze
# on 2026-08-10 (data#712) and was removed; there is no fallback, so an empty
# result is Block Flow MISSING rather than stale, and recap.py renders that
# distinction explicitly.
PT=s3://dt-exchange-venue-data/hot/hot__paradigm_trade_tape_30d.parquet

# ── One DuckDB session → CSVs ──────────────────────────────────────────────
# Staged: load each store into a table ONCE, then COPY out of the tables. With a
# single rollup object the old shape (every COPY re-reading the parquet) cost one
# cheap GET per statement; against ~300 partition objects it would re-fetch the
# whole window eight times over.
#
# SCHEMA DRIFT IS LIVE, not hypothetical: `underlying_price` is present on most
# `market_aggregates_5m` objects and absent from the newest, so a single read can
# straddle the boundary. The aggregate load is therefore hardened BOTH ways:
#   • `union_by_name=true` unifies across objects within one glob;
#   • the explicit projection is unioned (`UNION ALL BY NAME`) with a zero-row
#     `SELECT * FROM agg WHERE false` template, which supplies any column the
#     parquet has DROPPED, while the projection discards any column it has
#     ADDED. `INSERT … BY NAME SELECT *` alone handles only the first case —
#     an added upstream column makes it a hard binder error, which would take
#     the entire Snapshot down.
# The other two loads project explicitly without the template on purpose: they
# fail CLOSED and partially (no specs → venue blocks excluded; no surface → Δ
# n/a plus a freshness banner), and both read stable normalized venue schemas.
#
# ONE INSERT PER GLOB makes a gap survivable. The DuckDB CLI does not stop on
# error (`-bail` is off), so a day or hour with no objects costs exactly that
# slice — not the run. This is what makes day/hour globs safe to build from the
# clock: DuckDB treats a glob matching zero objects as an error, and the first
# minutes of a UTC day (or hour) legitimately have none yet. Nothing downstream
# depends on statement ordering.
#
# `asset` is echoed through every COPY so recap.py can assert the slice is for
# THIS asset (defense in depth against any shared-state/wrong-file regression).
#
# `SET threads` is oversubscribed on purpose. A 24h window is ~576 objects of
# ~15KB, so this scan is round-trip-bound, not CPU- or memory-bound: threads sit
# blocked on HTTP and the whole working set is a few MB. Measured end-to-end on
# the 24h SQL at 4/8/16/32 threads: 38.9s / 23.8s / 15.8s / 12.9s — near-perfect
# inverse scaling, which is the signature of latency, not work. Leaving the
# default (one per core) would tie the read time to the pod's CPU allocation.
{
cat <<SQL
INSTALL httpfs; LOAD httpfs;
${CRED_SQL}
SET preserve_insertion_order=false;
SET threads TO 32;
CREATE TABLE agg (row_type VARCHAR, exchange VARCHAR, asset VARCHAR, instrument_kind VARCHAR, bucket_at BIGINT, metric VARCHAR, open DOUBLE, close DOUBLE, high DOUBLE, low DOUBLE, optionType VARCHAR, expiry VARCHAR, strike DOUBLE, side VARCHAR, volume_sum DOUBLE, notional DOUBLE, turnover_usd DOUBLE, buy_volume DOUBLE, sell_volume DOUBLE, trade_count BIGINT, iv_sum DOUBLE, iv_count BIGINT, block_id VARCHAR, leg_count BIGINT, underlying_price DOUBLE);
CREATE TABLE inst (exchange VARCHAR, contract_size DOUBLE, price_unit VARCHAR, captured_at VARCHAR);
CREATE TABLE osum (symbol VARCHAR, mark_iv DOUBLE, delta DOUBLE, bucket_at BIGINT);
SQL

# The window is applied at query time (bucket_at), NOT here: the freshness probe
# below needs the newest bucket each store carries regardless of the window (see
# its comment). `row_type='flow'` is per-contract screen flow — ~85% of the rows
# and read by nothing here — so it is dropped at ingest.
for d in $AGG_DAYS; do
  echo "INSERT INTO agg BY NAME SELECT row_type, exchange, asset, instrument_kind, bucket_at, metric, open, close, high, low, optionType, expiry, strike, side, volume_sum, notional, turnover_usd, buy_volume, sell_volume, trade_count, iv_sum, iv_count, block_id, leg_count, underlying_price FROM (SELECT * FROM read_parquet('${MA}${d}*.parquet', union_by_name=true, hive_partitioning=false) UNION ALL BY NAME SELECT * FROM agg WHERE false) WHERE asset='${ASSET}' AND row_type <> 'flow';"
done

for v in $MET_VENUES; do
  echo "INSERT INTO inst BY NAME SELECT exchange, contract_size, price_unit, captured_at FROM read_parquet('${MET}/exchange=${v}/currency=${CCY}/instruments__${v}__${CCY}__${META_HOUR}*.parquet', hive_partitioning=false) WHERE instType='option';"
done
# One spec row per venue, newest capture wins. A venue absent here is a venue
# whose specs could not be read this run — see the two JOINs below, which treat
# that case differently on purpose.
echo "CREATE TABLE spec AS SELECT exchange, arg_max(contract_size, captured_at) AS contract_size, arg_max(price_unit, captured_at) AS price_unit FROM inst GROUP BY exchange;"

# The bucket timestamp is reconstructed from the hive partition columns
# (year/month/day/hour/start_minute) — the summary rows themselves carry no
# timestamp, only the per-instrument close values for their bucket. Hence
# `hive_partitioning=true` here and `false` on every other read, where the path
# keys either don't exist or would collide with real columns of the same name.
#
# OTM ONLY (strike beyond the underlying), which is what `v_vol_surface`
# published and therefore what this section has always been computed from. The
# venue chain also lists every ITM mirror; by put-call parity those carry the
# same strike and a call-delta that collides with the OTM leg's, so admitting
# them would silently overwrite half the interpolation grid with the other
# side's mark. Expressed as strike-vs-underlying rather than a delta cutoff so
# it states the actual rule instead of approximating it.
for h in $SURF_HOURS; do
  y=${h%%/*}; rest=${h#*/}; mo=${rest%%/*}; rest=${rest#*/}; dd=${rest%%/*}; hh=${rest#*/}
  echo "INSERT INTO osum BY NAME SELECT symbol, markIV_close AS mark_iv, delta_close AS delta, CAST(epoch_ms(make_timestamp(CAST(year AS INT), CAST(month AS INT), CAST(day AS INT), CAST(hour AS INT), CAST(start_minute AS INT), 0)) AS BIGINT) AS bucket_at FROM read_parquet('${NRM}/year=${y}/month=${mo}/day=${dd}/hour=${hh}/start_minute=*/normalized__deribit__option_summary__${CCY}__5m__agg__*.parquet', union_by_name=true, hive_partitioning=true) WHERE markIV_close IS NOT NULL AND symbol LIKE '${ASSET}-%' AND ((symbol LIKE '%-C' AND CAST(split_part(symbol, '-', 3) AS DOUBLE) >= underlyingPrice_close) OR (symbol LIKE '%-P' AND CAST(split_part(symbol, '-', 3) AS DOUBLE) <= underlyingPrice_close));"
done

# ── Snapshot: DVOL/spot OHLC, volume, activity ─────────────────────────────
cat <<SQL
COPY (SELECT asset, exchange, metric, arg_min(open, bucket_at) AS open, arg_max(close, bucket_at) AS close, max(high) AS high, min(low) AS low FROM agg WHERE row_type='dvol_spot' AND bucket_at >= ${START_MS} GROUP BY asset, exchange, metric) TO '${WORK}/dvol_spot.csv' (HEADER, DELIMITER ',');
SQL

# Index price used to convert a coin-quoted premium to USD. Per-bucket
# `underlying_price` is the right basis and is what the rollup used, but the
# newest partitions have dropped the column, so fall back to the window's spot
# close rather than emit a NULL premium.
PX="coalesce(a.underlying_price, (SELECT arg_max(close, bucket_at) FROM agg WHERE row_type = 'dvol_spot' AND metric = 'spot'))"

# volume.csv — LEFT JOIN on the specs, defaulting to contract_size 1.0 and a
# USD price unit. Every field recap.py actually reads is safe under that
# default: `turnover_usd` and `trade_count` need no scaling at all, and
# `volume_sum` is summed for Deribit ONLY (contract_size 1.0). The default is
# also the true spec for the two venues whose metadata is published
# irregularly (bullish, deribit-usdc are both 1.0 / quote_usd), so a missing
# spec row changes nothing for them. `notional` is converted to USD the way the
# rollup did — premium × contract_size × index for coin-quoted venues, as-is for
# USD-quoted ones — so the column keeps its documented meaning; recap.py does
# not read it.
cat <<SQL
COPY (SELECT a.asset, a.exchange, a.optionType, sum(a.volume_sum * coalesce(s.contract_size, 1.0)) AS volume_sum, sum(CASE WHEN s.price_unit = 'coin' THEN a.notional * coalesce(s.contract_size, 1.0) * ${PX} ELSE a.notional END) AS notional, sum(a.turnover_usd) AS turnover_usd, sum(a.buy_volume * coalesce(s.contract_size, 1.0)) AS buy_volume, sum(a.sell_volume * coalesce(s.contract_size, 1.0)) AS sell_volume, sum(a.trade_count) AS trade_count FROM agg a LEFT JOIN spec s ON s.exchange = a.exchange WHERE a.row_type = 'volume' AND a.bucket_at >= ${START_MS} GROUP BY a.asset, a.exchange, a.optionType) TO '${WORK}/volume.csv' (HEADER, DELIMITER ',');
SQL

# venue_blocks.csv — INNER JOIN on the specs, deliberately. Here the multiplier
# IS load-bearing: recap.py prices these blocks as volume_coin × spot and ranks
# them against the Paradigm tape, so an unscaled OKX block reads 100× its true
# size and takes over Biggest Print. A venue whose specs could not be read is
# therefore DROPPED rather than assumed 1.0 — the same fail-toward-exclusion
# rule the venue dedupe uses, and the same trade: a missed block, never an
# invented one. Only deribit and okex-options emit block rows today, and both
# publish specs hourly.
cat <<SQL
COPY (SELECT a.asset, a.exchange, a.block_id, min(a.bucket_at) AS bucket_at, sum(a.volume_sum * s.contract_size) AS volume_coin, sum(CASE WHEN s.price_unit = 'coin' THEN a.notional * s.contract_size * ${PX} ELSE a.notional END) AS premium_usd, sum(a.leg_count) AS leg_count, sum(a.iv_sum) AS iv_sum, sum(a.iv_count) AS iv_count FROM agg a JOIN spec s ON s.exchange = a.exchange WHERE a.row_type = 'block' AND a.instrument_kind = 'option' AND a.bucket_at >= ${START_MS_5M} GROUP BY a.asset, a.exchange, a.block_id) TO '${WORK}/venue_blocks.csv' (HEADER, DELIMITER ',');
SQL

# ── Vol surface: now + window-open, one pipeline ───────────────────────────
# `now` is the newest 5-min snapshot loaded; the open is the snapshot nearest
# window-start, tolerance-guarded to 15 minutes so a start outside the loaded
# hours writes a header-only CSV (→ Δ n/a) instead of a wrong open. The
# subselect yielding NULL matches no row, which is exactly that header-only
# outcome.
cat <<SQL
COPY (SELECT symbol, mark_iv, delta FROM osum WHERE bucket_at = (SELECT max(bucket_at) FROM osum)) TO '${WORK}/surface_now.csv' (HEADER, DELIMITER ',');
COPY (SELECT symbol, mark_iv, delta FROM osum WHERE bucket_at = (SELECT bucket_at FROM osum WHERE abs(bucket_at - ${START_MS}) <= 900000 ORDER BY abs(bucket_at - ${START_MS}) LIMIT 1)) TO '${WORK}/surface_open.csv' (HEADER, DELIMITER ',');
SQL

# ── Paradigm block tape ────────────────────────────────────────────────────
cat <<SQL
COPY (SELECT strftime(CAST(traded_at_iso AS TIMESTAMP), '%Y-%m-%d') AS "DATE", strftime(CAST(traded_at_iso AS TIMESTAMP), '%H:%M:%S') AS "TIME", product AS PRODUCT, description AS DESCRIPTION, quantity AS QTY, trade_price AS PRICE, mark_price AS REF_PRICE, taker_side AS SIDE, CASE WHEN upper(trim(split_part(coalesce(product,''), ' - ', 2))) = 'DBT' AND upper(coalesce(asset,'')) IN ('BTC','ETH') AND instrument_name IS NOT NULL AND upper(instrument_name) NOT LIKE '%USDC%' THEN upper(asset) ELSE 'USDC' END AS QUOTE_CURRENCY, notional_volume_usd AS NOTIONAL_VOLUME_USD, rfq_id AS RFQ_ID, trade_id AS TRADE_ID, block_trade_id AS BLOCK_TRADE_ID, venue_block_trade_id AS VENUE_BLOCK_TRADE_ID FROM read_parquet('${PT}') WHERE row_type='paradigm_trade' AND asset='${ASSET}' AND instrument_kind='OPTION' AND traded_at >= ${START_MS}) TO '${WORK}/blocks.csv' (HEADER, DELIMITER ',');
SQL

# ── Freshness probes ───────────────────────────────────────────────────────
# The newest timestamp each continuously-written source carries, NOT windowed —
# which is why `agg` and `osum` are loaded un-windowed above. A source frozen
# before START_MS returns zero windowed rows, indistinguishable from "quiet
# market", and tells you nothing about the feed. The absolute max is the
# pipeline's heartbeat: is anything still writing?
#
# ONE FILE PER SOURCE, not one UNION ALL. A single COPY spanning both reads
# means either read failing writes ZERO bytes, silently disabling the gate for
# BOTH sources — and a disabled gate is indistinguishable in the output from
# "everything fresh", which is the original bug wearing a different hat. Split,
# a surface outage costs only the vol_surface probe. recap.py treats a source it
# cannot read as UNKNOWN and says so rather than assuming fresh; see
# load_freshness / check_freshness.
#
# MIN OVER THE PER-METRIC MAXIMA for the aggregates, not a flat max.
# `row_type='dvol_spot'` is two series (metric='dvol' and metric='spot') read as
# separate fields by load_hot. A flat max reports the FRESHEST constituent, so a
# dead DVOL scraper hides behind a live spot ticker and the recap renders frozen
# DVOL with no banner.
#
# GROUP BY metric ONLY — deliberately NOT `exchange, metric`. load_hot collapses
# every exchange to one dvol and one spot, sorting so Deribit wins, so the recap
# renders DERIBIT's numbers. Grouping by exchange made the probe measure a
# superset of what is rendered: any other venue emitting sparse dvol_spot rows
# and lagging past the limit would fire the banner, discard a perfectly live
# Deribit snapshot and force a serial refetch on every run — the cry-wolf
# outcome the limits are explicitly sized to avoid.
#
# Note the reading is the laggiest PRESENT constituent: `min` cannot see a group
# that does not exist, so a metric absent entirely does not register here. That
# case is caught downstream by `hot['dvol'] is None`, which already diverts.
#
# Partitioned sources sharpen this rather than weaken it. A producer that stops
# writing objects eventually leaves the loaded days/hours empty, the probe
# yields no timestamp, and recap.py banners UNKNOWN — the rollup's failure mode
# (a stale file that still parses cleanly) has no analogue here.
#
# ONLY heartbeat sources are probed. dvol_spot rows are emitted every 5 min and
# option summaries every 5 min regardless of trading activity, so a gap in them
# is unambiguously a pipeline fault. Event-driven sources are NOT probed and
# must not be: the block tape's newest trade is a function of whether anyone
# traded, so gating on it would fire on any quiet hour. (Measured: venue `block`
# rows legitimately sat 1h13m behind with the pipeline perfectly healthy,
# because only 29 blocks printed in 24h.)
cat <<SQL
COPY (SELECT 'recap_aggregates' AS source, min(mx) AS max_at FROM (SELECT metric, max(bucket_at) AS mx FROM agg WHERE row_type='dvol_spot' GROUP BY metric) AS g) TO '${WORK}/freshness_rec.csv' (HEADER, DELIMITER ',');
COPY (SELECT 'vol_surface' AS source, max(bucket_at) AS max_at FROM osum) TO '${WORK}/freshness_vs.csv' (HEADER, DELIMITER ',');
SQL
} > "$WORK/recap.sql"

# Test hook: RECAP_PRINT_SQL=1 emits the generated session and exits, with the
# credential block elided, so CI can assert on the REAL plan — which stores are
# read, how the drift/gap guards are spelled, which join each COPY uses —
# instead of grepping this file's source text. No creds, no network.
[ -n "${RECAP_PRINT_SQL:-}" ] && { cat "$WORK/recap.sql"; exit 0; }

# recap.py runs this DuckDB session in a thread concurrent with the Deribit fetch.
# No exec — the EXIT trap must fire to clean up $WORK.
[ -n "$CAP_NOTE" ] && { echo "$CAP_NOTE"; echo; }
cd "$DIR" && uv run scripts/recap.py \
  --asset "$ASSET" --window "$WIN" --csv-dir "$WORK" --duckdb-sql "$WORK/recap.sql" --render
