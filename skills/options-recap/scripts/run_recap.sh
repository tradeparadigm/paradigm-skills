#!/usr/bin/env bash
# run_recap.sh — the entire live recap in one command, so the agent types one
# short line instead of regenerating a ~50-line bootstrap+SQL block (that
# generation was ~12s of the old run). Does: STS bootstrap, one DuckDB session
# into CSVs, then recap.py --render. Its stdout IS the final four-section recap.
#
# Usage: bash scripts/run_recap.sh <ASSET> <WINDOW>     e.g. run_recap.sh BTC 8h
#
# Source overrides (all optional; defaults are the production paths):
#   RECAP_MA_ROOT        s3://dt-exchange-venue-data/market_aggregates_5m
#   RECAP_MA_GLOB        ${RECAP_MA_ROOT}/**/*.parquet   (narrow it once the
#                        partition layout is known — see README "Data sources")
#   RECAP_PT_ROOT        s3://dt-exchange-venue-data/paradigm_trade_tape
#   RECAP_PARADIGM_TAPE  ${RECAP_PT_ROOT}/**/*.parquet   (full glob, verbatim)
set -uo pipefail

# Some users type the no-op keyword "options" (/recap btc options 8h). This skill
# is always options, so drop any "options"/"option" token before assigning
# asset/window — otherwise a stray token lands in the window slot and breaks
# parsing (parse_window_ms raises on "options").
ARGS=""
for a in "$@"; do
  case "$(printf '%s' "$a" | tr '[:upper:]' '[:lower:]')" in
    options|option) ;;                       # no-op keyword — drop
    *) ARGS="$ARGS $a" ;;
  esac
done
set -- $ARGS

ASSET=$(printf '%s' "${1:-BTC}" | tr '[:lower:]' '[:upper:]')
WIN="${2:-8h}"
[ "$WIN" = "1d" ] && WIN=24h                 # exact match — the old substring
                                             # substitution turned 31d into 324h

# Window → seconds, parsed GENERICALLY (Nm/Nh/Nd) so any window works. The
# 5-min aggregates are windowed by bucket_at at query time, so there are no
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
# Cap at 24h — DISCLOSED (the banner line below is part of the recap output).
# The cap predates this data path: the retired hot rollup held only ~24h. The
# market_aggregates_5m store it replaced is the pipeline's own aggregation
# layer and retains history, but its retention has not been verified against a
# long window yet, and build()'s >24h handling (Deribit-preferred DVOL/spot,
# the hot_horizon banner) still assumes a 24h Snapshot horizon. Lift the cap
# and that assumption together, once a >24h window has been verified live.
CAP_NOTE=""
if [ "$SECS" -gt 86400 ]; then
  CAP_NOTE="⚠ window capped at 24h — $WIN exceeds the ~24h Snapshot-data horizon."
  WIN=24h; SECS=86400
fi
# PRESET flags the canonical windows. Every window reads the same source
# (bucket_at-windowed), so PRESET no longer gates the data path — it's retained
# for the plan hook below and as an observability signal (canonical vs ad-hoc).
case "$WIN" in
  5m|10m|20m|1h|4h|8h|24h) PRESET=1;; *) PRESET=0;;
esac

# Vol-surface deltas (ΔATM/ΔRR/ΔFly) need a window-OPEN surface, which the 5-min
# aggregates don't carry (no surface rows). Read the consolidated per-strike
# store v_vol_surface: its rolling _hot.parquet holds ~2h of 1-min snapshots,
# and older opens come from the cold hour-partition containing window-start
# (hourly files, published ~15min after each hour closes). "Now" is always
# _hot.parquet's latest snapshot, so open+close share one pipeline. Resolved
# here, before the STS bootstrap: it's pure date math, which lets the
# RECAP_PRINT_SOURCES test hook exercise it with no creds. RECAP_NOW_S pins the
# clock so tests can assert exact partition paths.
NOW_S=${RECAP_NOW_S:-$(date -u +%s)}; START_S=$((NOW_S - SECS)); START_MS=$((START_S * 1000))
# Venue-tape `block` rows are bucketed to 5 minutes while the Paradigm tape
# carries exact-ms traded_at. Windowing both on START_MS drops any venue block
# in the first PARTIAL bucket, so a Paradigm print in that bucket has no
# counterpart, full-coverage proof fails, and the whole venue falls back to
# structural — measured to leave the id merge inactive about half the time from
# the bucket edge alone. Flooring the venue window to the containing bucket
# removes that term. It can only ADD venue rows that were already in-window at
# 5-minute resolution, so it cannot create a double count.
START_MS_5M=$(( (START_S - START_S % 300) * 1000 ))
VS_HOT=s3://dt-paradigm-data/paradigm_data/v_vol_surface/_hot.parquet
VS_COLD=""
if [ "$SECS" -gt 3600 ]; then               # window-start may predate _hot's buffer
  SY=$(date -u -d "@$START_S" +%Y 2>/dev/null || date -u -r "$START_S" +%Y)
  SM=$(date -u -d "@$START_S" +%m 2>/dev/null || date -u -r "$START_S" +%m)
  SD=$(date -u -d "@$START_S" +%d 2>/dev/null || date -u -r "$START_S" +%d)
  SH=$(date -u -d "@$START_S" +%H 2>/dev/null || date -u -r "$START_S" +%H)
  VS_COLD=s3://dt-paradigm-data/paradigm_data/v_vol_surface/base=${ASSET}/year=${SY}/month=${SM}/day=${SD}/hour=${SH}/v_vol_surface.parquet
fi

# ── 5-min market aggregates: the Snapshot's DVOL/spot, $ Volume, Activity/P-C
# and the venue-tape blocks. ────────────────────────────────────────────────
# This is the pipeline's own aggregation layer — the data the retired
# hot__recap_aggregates_5m_24h.parquet rollup was generated FROM (traced
# row-for-row: same buckets, same volume_sum/trade_count; the hot copy only
# added a USD-converted `notional_usd`, which nothing here renders). Reading
# the layer directly removes one derived artifact from the path, and with it
# the class of failure where the derived file's mtime kept moving while its
# contents did not (the 2026-07-10 freeze, see the freshness probe below).
#
# Layout: a prefix of parquet files, read through ONE glob. The default is the
# recursive glob, which is layout-agnostic (hive_partitioning=true picks up any
# key=value directories as columns; union_by_name=true tolerates columns that
# were added mid-history, e.g. turnover_usd) but lists the whole prefix and
# opens every file's footer for bucket_at pruning. Once the partition layout
# is verified (README: "Data sources" has the probe), narrow it with
# RECAP_MA_GLOB. A glob that matches NO files fails at bind, which fails every
# read below toward the Deribit fallback + a "freshness could not be verified"
# banner — loud, never a silent all-clear.
MA_ROOT=${RECAP_MA_ROOT:-s3://dt-exchange-venue-data/market_aggregates_5m}
MA_GLOB=${RECAP_MA_GLOB:-${MA_ROOT}/**/*.parquet}
MA="read_parquet('${MA_GLOB}', hive_partitioning=true, union_by_name=true)"
# Freshness probe lookback. The probe must not be WINDOW-filtered (a source
# frozen before window-start returns zero windowed rows, indistinguishable from
# a quiet market), but an unbounded max over the whole store scans every file
# in the layer on every recap. 7 days is wide enough to report a precise lag
# for any realistic freeze; a source frozen longer than that yields NO row,
# which recap.py reports as `unknown` (freshness unverifiable → banner + divert),
# never as fresh. Both outcomes fail safe; only the wording differs.
PROBE_FROM_MS=$(( (NOW_S - 7 * 86400) * 1000 ))
# Block-tape source (documented with its read below, resolved here so the
# RECAP_PRINT_PT hook can exercise it with no creds).
PT_ROOT=${RECAP_PT_ROOT:-s3://dt-exchange-venue-data/paradigm_trade_tape}
PT_GLOB=${RECAP_PARADIGM_TAPE:-${PT_ROOT}/**/*.parquet}
PT="read_parquet('${PT_GLOB}', hive_partitioning=true, union_by_name=true)"

# Testability hooks: echo resolved state and exit before any STS/DuckDB work (no
# creds/network needed). Used by tests/test_run_recap.py.
#   RECAP_PRINT_ARGS → "ASSET WIN"          (arg normalization)
#   RECAP_PRINT_PLAN → "ASSET WIN SECS PRESET"  (window parsing + preset flag)
#   RECAP_PRINT_SOURCES → "ASSET WIN START_MS VS_COLD|-"  (surface-open resolution)
#   RECAP_PRINT_MA → "MA_GLOB PROBE_FROM_MS"  (aggregates source resolution)
#   RECAP_PRINT_PT → "PT_GLOB"                (block-tape source resolution)
[ -n "${RECAP_PRINT_ARGS:-}" ] && { echo "$ASSET $WIN"; exit 0; }
[ -n "${RECAP_PRINT_PLAN:-}" ] && { echo "$ASSET $WIN $SECS $PRESET"; exit 0; }
[ -n "${RECAP_PRINT_SOURCES:-}" ] && { echo "$ASSET $WIN $START_MS ${VS_COLD:--}"; exit 0; }
[ -n "${RECAP_PRINT_MA:-}" ] && { echo "$MA_GLOB $PROBE_FROM_MS"; exit 0; }
[ -n "${RECAP_PRINT_PT:-}" ] && { echo "$PT_GLOB"; exit 0; }
DIR="$(cd "$(dirname "$0")/.." && pwd)"      # skill dir (scripts/..)
# Per-invocation workdir. The old fixed /tmp/recap + /tmp/recap.sql were shared
# state: two concurrent recaps (e.g. BTC and ETH fired from separate sessions)
# raced on the SQL file and CSVs, and one recap silently rendered the other's
# asset/window slice (exit 0, no warning). A fresh mktemp dir per run isolates
# them completely; it also supersedes the old stale-CSV wipe — nothing stale can
# exist in a directory this run just created.
WORK=$(mktemp -d "${TMPDIR:-/tmp}/recap.XXXXXX") || { echo "recap: mktemp failed" >&2; exit 1; }
trap 'rm -rf "$WORK"' EXIT

# STS bootstrap (IRSA → temporary creds; see paradigm-data-discovery skill).
TOKEN=$(cat "$AWS_WEB_IDENTITY_TOKEN_FILE")
CREDS=$(curl -s "https://sts.ap-northeast-1.amazonaws.com/?Action=AssumeRoleWithWebIdentity&Version=2011-06-15&RoleArn=${AWS_ROLE_ARN}&RoleSessionName=duckdb&WebIdentityToken=${TOKEN}")
AK=$(printf '%s' "$CREDS" | grep -o '<AccessKeyId>[^<]*'     | cut -d'>' -f2)
SK=$(printf '%s' "$CREDS" | grep -o '<SecretAccessKey>[^<]*' | cut -d'>' -f2)
ST=$(printf '%s' "$CREDS" | grep -o '<SessionToken>[^<]*'    | cut -d'>' -f2)

# Multi-venue Paradigm block tape — the SOLE source for Biggest Print + Block
# Flow. s3://dt-exchange-venue-data/paradigm_trade_tape/ is the persisted
# store of the exchange-venue-data paradigm-trade pipeline (leg grain, built
# from the Airbyte→S3 UM landing on s3://dt-paradigm-data); the
# hot__paradigm_trade_tape_30d.parquet rollup this replaced was that pipeline's
# trailing-30d single-file copy of the same rows. It spans every venue Paradigm
# brokers (Deribit/Paradex/Bullish/…) with USD notional PER LEG and the
# structure named in DESCRIPTION, so recap.py needs no cross-venue $
# normalization and no instrument-name inference. It also carries
# venue_block_trade_id — the venue's own block id — which is what unlocks exact
# block dedupe against the venue tapes.
#
# Same read shape as the aggregates layer above: ONE recursive glob by default
# (hive_partitioning picks up key=value directories, union_by_name tolerates
# columns added mid-history), narrowed via RECAP_PT_ROOT / RECAP_PARADIGM_TAPE
# once the partition layout is verified (README "Data sources" has the probe).
# The COPY below expects the columns the hot rollup exposed (traded_at ms +
# traded_at_iso, product, description, quantity, trade_price, mark_price,
# taker_side, asset, instrument_name, instrument_kind, notional_volume_usd,
# rfq_id, trade_id, block_trade_id, venue_block_trade_id); a store without one
# of them fails the COPY at bind, which recap.py renders as Block Flow MISSING.
# The hot rollup's row_type='paradigm_trade' discriminator is NOT applied: a
# dedicated tape store is single-kind, and a predicate on a column it may not
# carry would fail every recap for nothing. asset / instrument_kind are
# compared case-insensitively for the same reason.
#
# There is no fallback: an empty result is Block Flow MISSING rather than
# stale — recap.py renders that distinction explicitly. Not freshness-probed,
# and must not be: it is event-driven (its newest row is whenever anyone last
# traded), so a quiet hour would fire a false alarm.

# One DuckDB session → CSVs. One statement per line; `at` is reserved → alias it.
# Each COPY echoes `asset` through so recap.py can assert the slice is for THIS
# asset (defense in depth against any future shared-state/wrong-file regression).
#
# The aggregates layer is scanned ONCE into a temp table (`ma`) holding this
# asset's rows from the floored window-open onward; the dvol_spot / volume /
# venue_blocks COPYs then read that table. Reading the glob in each COPY would
# re-list the prefix and re-open every footer per statement. The temp table is
# already asset-scoped and floored; the COPYs restate their own bounds anyway
# (exact START_MS for the Snapshot rows, START_MS_5M for venue blocks) so each
# statement's contract is legible on its own line. enable_object_cache keeps
# parquet metadata across the session for the reads that do hit S3 twice (the
# freshness probe, the surface).
cat > "$WORK/recap.sql" <<SQL
INSTALL httpfs; LOAD httpfs;
SET s3_region='ap-northeast-1';
SET s3_access_key_id='${AK}';
SET s3_secret_access_key='${SK}';
SET s3_session_token='${ST}';
SET enable_object_cache=true;
CREATE TEMP TABLE ma AS SELECT * FROM ${MA} WHERE asset='${ASSET}' AND bucket_at >= ${START_MS_5M};
COPY (SELECT asset, exchange, metric, arg_min(open, bucket_at) AS open, arg_max(close, bucket_at) AS close, max(high) AS high, min(low) AS low FROM ma WHERE row_type='dvol_spot' AND bucket_at >= ${START_MS} GROUP BY asset, exchange, metric) TO '${WORK}/dvol_spot.csv' (HEADER, DELIMITER ',');
COPY (SELECT asset, exchange, optionType, sum(volume_sum) AS volume_sum, sum(notional) AS notional_native, sum(buy_volume) AS buy_volume, sum(sell_volume) AS sell_volume, sum(trade_count) AS trade_count FROM ma WHERE row_type='volume' AND bucket_at >= ${START_MS} GROUP BY asset, exchange, optionType) TO '${WORK}/volume.csv' (HEADER, DELIMITER ',');
COPY (SELECT asset, exchange, block_id, min(bucket_at) AS bucket_at, sum(volume_sum) AS volume_coin, sum(notional) AS premium_native, sum(leg_count) AS leg_count, sum(iv_sum) AS iv_sum, sum(iv_count) AS iv_count FROM ma WHERE row_type='block' AND instrument_kind='option' AND bucket_at >= ${START_MS_5M} GROUP BY asset, exchange, block_id) TO '${WORK}/venue_blocks.csv' (HEADER, DELIMITER ',');
COPY (WITH h AS (SELECT symbol, mark_iv, delta, "at" FROM read_parquet('${VS_HOT}') WHERE base='${ASSET}' AND symbol LIKE '${ASSET}-%' AND mark_iv IS NOT NULL) SELECT symbol, mark_iv, delta FROM h WHERE "at"=(SELECT max("at") FROM h)) TO '${WORK}/surface_now.csv' (HEADER, DELIMITER ',');
COPY (WITH h AS (SELECT symbol, mark_iv, delta, "at" FROM read_parquet('${VS_HOT}') WHERE base='${ASSET}' AND symbol LIKE '${ASSET}-%' AND mark_iv IS NOT NULL) SELECT symbol, mark_iv, delta FROM h WHERE "at"=(SELECT "at" FROM h WHERE abs("at"-${START_MS})<=900000 ORDER BY abs("at"-${START_MS}) LIMIT 1)) TO '${WORK}/surface_open.csv' (HEADER, DELIMITER ',');
COPY (SELECT asset, exchange, optionType, sum(volume_sum) AS volume_sum, sum(notional) AS notional_native, sum(turnover_usd) AS turnover_usd, sum(buy_volume) AS buy_volume, sum(sell_volume) AS sell_volume, sum(trade_count) AS trade_count FROM ma WHERE row_type='volume' AND bucket_at >= ${START_MS} GROUP BY asset, exchange, optionType) TO '${WORK}/volume.csv' (HEADER, DELIMITER ',');
COPY (SELECT strftime(CAST(traded_at_iso AS TIMESTAMP), '%Y-%m-%d') AS "DATE", strftime(CAST(traded_at_iso AS TIMESTAMP), '%H:%M:%S') AS "TIME", product AS PRODUCT, description AS DESCRIPTION, quantity AS QTY, trade_price AS PRICE, mark_price AS REF_PRICE, taker_side AS SIDE, CASE WHEN upper(trim(split_part(coalesce(product,''), ' - ', 2))) = 'DBT' AND upper(coalesce(asset,'')) IN ('BTC','ETH') AND instrument_name IS NOT NULL AND upper(instrument_name) NOT LIKE '%USDC%' THEN upper(asset) ELSE 'USDC' END AS QUOTE_CURRENCY, notional_volume_usd AS NOTIONAL_VOLUME_USD, rfq_id AS RFQ_ID, trade_id AS TRADE_ID, block_trade_id AS BLOCK_TRADE_ID, venue_block_trade_id AS VENUE_BLOCK_TRADE_ID FROM ${PT} WHERE upper(asset)='${ASSET}' AND upper(instrument_kind)='OPTION' AND traded_at >= ${START_MS}) TO '${WORK}/blocks.csv' (HEADER, DELIMITER ',');
COPY (SELECT 'market_aggregates' AS source, min(mx) AS max_at FROM (SELECT metric, max(bucket_at) AS mx FROM ${MA} WHERE asset='${ASSET}' AND row_type='dvol_spot' AND bucket_at >= ${PROBE_FROM_MS} GROUP BY metric) AS g) TO '${WORK}/freshness_ma.csv' (HEADER, DELIMITER ',');
COPY (SELECT 'vol_surface' AS source, max("at") AS max_at FROM read_parquet('${VS_HOT}') WHERE base='${ASSET}' AND symbol LIKE '${ASSET}-%' AND mark_iv IS NOT NULL) TO '${WORK}/freshness_vs.csv' (HEADER, DELIMITER ',');
SQL

# notional columns (volume.csv `notional_native`, venue_blocks.csv
# `premium_native`): the layer's `notional` is option PREMIUM in each venue's
# native quote unit (coin on deribit / okex-options, USD on bybit-options /
# deribit-usdc — the hot rollup multiplied the coin venues by underlying_price
# to get its notional_usd). recap.py renders NEITHER column — $ Volume is
# turnover_usd, block notional is volume_coin × spot — so the conversion is
# not reproduced here: it would add a dependency on underlying_price and on a
# per-venue unit rule for a number nobody reads. The suffix says what the
# column IS so nobody sums it across venues as dollars.

# volume.csv is written TWICE, on purpose. The second COPY adds turnover_usd —
# the pipeline's per-trade USD premium, summable across ALL venues (drives the
# cross-venue $ Volume line). If NO file in the glob carries that column the
# second COPY fails at bind, the first (Activity/P-C intact) stands, and
# recap.py labels the Volume line Deribit-scoped. union_by_name already
# covers the partial case (files that predate the column read NULL for it),
# so this two-step is only the total-absence guard. Placed BEFORE the VS_COLD
# append so neither may-fail statement can shadow the other's output.

# freshness_*.csv: the newest timestamp each continuously-written source
# carries, deliberately NOT window-filtered — a source frozen before START_MS
# returns zero windowed rows, indistinguishable from "quiet market". The
# market_aggregates probe is bounded to PROBE_FROM_MS (see above) purely for
# cost; a freeze older than that reads `unknown`, never fresh.
#
# ONE FILE PER SOURCE, not one UNION ALL. A single COPY spanning both reads
# means either read failing writes ZERO bytes, silently disabling the gate for
# BOTH sources — and a disabled gate is indistinguishable in the output from
# "everything fresh". Split, a ${VS_HOT} outage costs only the vol_surface probe
# and market_aggregates is still checked. recap.py treats a source it cannot
# read as UNKNOWN and says so rather than assuming fresh; see load_freshness /
# check_freshness.
#
# MIN OVER THE PER-METRIC MAXIMA for market_aggregates, not a flat max.
# `row_type='dvol_spot'` is two series (metric='dvol' and metric='spot') read as
# separate fields by load_hot. A flat max reports the FRESHEST constituent, so a
# dead DVOL scraper hides behind a live spot ticker and the recap renders frozen
# DVOL with no banner.
#
# GROUP BY metric ONLY — deliberately NOT by venue. load_hot collapses every
# venue to one dvol and one spot, sorting so Deribit wins, so the recap renders
# DERIBIT's numbers. Grouping by venue made the probe measure a superset of
# what is rendered: any other venue emitting sparse dvol_spot rows and lagging
# past the limit would fire the banner, discard a perfectly live Deribit
# snapshot and force a serial refetch on every run — the cry-wolf outcome the
# limits are explicitly sized to avoid.
#
# Note the reading is the laggiest PRESENT constituent: `min` cannot see a group
# that does not exist, so a metric absent entirely does not register here. That
# case is caught downstream by `hot['dvol'] is None`, which already diverts.
#
# WHY THIS EXISTS. The hot recap rollup went stale on 2026-07-10 and kept
# rendering July 10 numbers as if live until 2026-08-04 — ~3.5 weeks — because
# the object's mtime kept changing while its CONTENTS did not, and nothing
# anywhere compared a timestamp to the clock. Reading the aggregation layer
# directly removes that particular derived artifact, but the layer is still a
# continuously-written feed that can stop, so the probe stays.
#
# ONLY heartbeat sources are probed. dvol_spot rows are emitted every 5 min and
# vol-surface snapshots every minute regardless of trading activity, so a gap in
# them is unambiguously a pipeline fault. Event-driven sources are NOT probed and
# must not be: the block tape's newest trade is a function of whether anyone
# traded, so gating on it would fire on any quiet hour. (Measured: venue `block`
# rows legitimately sat 1h13m behind with the pipeline perfectly healthy, because
# only 29 blocks printed in 24h.)

# surface_open: the statement above is a SAFE fallback from _hot (always exists),
# tolerance-guarded to 15min so a window-start outside _hot's ~2h buffer writes a
# header-only CSV (→ n/a) instead of a wrong open. For >1h windows the
# authoritative open is the cold hour-partition — appended as the session's LAST
# statement so it OVERWRITES the fallback when it succeeds. If the partition
# object is missing (start hour's file not yet published, or older than the cold
# history), read_parquet fails at bind before the COPY sink opens, the fallback
# file stands, and only this final statement is lost: nothing depends on the
# DuckDB CLI continuing past the error. This closes the just-over-1h gap (cold
# partition unpublished, but _hot still covers the start) and keeps clean n/a
# otherwise.
if [ -n "$VS_COLD" ]; then
  cat >> "$WORK/recap.sql" <<SQL
COPY (WITH h AS (SELECT symbol, mark_iv, delta, "at" FROM read_parquet('${VS_COLD}') WHERE base='${ASSET}' AND symbol LIKE '${ASSET}-%' AND mark_iv IS NOT NULL) SELECT symbol, mark_iv, delta FROM h WHERE "at"=(SELECT "at" FROM h ORDER BY abs("at"-${START_MS}) LIMIT 1)) TO '${WORK}/surface_open.csv' (HEADER, DELIMITER ',');
SQL
fi

# recap.py runs this DuckDB session in a thread concurrent with the Deribit fetch.
# No exec — the EXIT trap must fire to clean up $WORK.
[ -n "$CAP_NOTE" ] && { echo "$CAP_NOTE"; echo; }
cd "$DIR" && uv run scripts/recap.py \
  --asset "$ASSET" --window "$WIN" --csv-dir "$WORK" --duckdb-sql "$WORK/recap.sql" --render
