#!/usr/bin/env bash
# run_recap.sh — the entire live recap in one command, so the agent types one
# short line instead of regenerating a ~50-line bootstrap+SQL block (that
# generation was ~12s of the old run). Does: STS bootstrap, one DuckDB session
# into CSVs, then recap.py --render. Its stdout IS the final four-section recap.
#
# Usage: bash scripts/run_recap.sh <ASSET> <WINDOW>     e.g. run_recap.sh BTC 8h
set -uo pipefail

# Some users type the no-op keyword "options" (/recap btc options 8h). This skill
# is always options, so drop any "options"/"option" token before assigning
# asset/window — otherwise a stray token lands in the window slot and breaks
# parsing (hot__recap_options.parquet doesn't exist; parse_window_ms raises).
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

# Window → seconds, parsed GENERICALLY (Nm/Nh/Nd) so any window works. The rolling
# recap-aggregates file is windowed by bucket_at at query time, so there are no
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
# Cap at 24h. Every flow source (rolling recap-aggregates file, Deribit public
# tape) holds only ~24h, so a longer window rendered partially-covered flow
# under a full-window header. Until >24h flow is wired to the cold store,
# clamp and DISCLOSE — the banner line below is part of the recap output.
CAP_NOTE=""
if [ "$SECS" -gt 86400 ]; then
  CAP_NOTE="⚠ window capped at 24h — $WIN exceeds the ~24h data horizon."
  WIN=24h; SECS=86400
fi
# PRESET flags the canonical windows. Since the migration to the single rolling
# recap-aggregates file every window reads the same source (bucket_at-windowed), so
# PRESET no longer gates the data path — it's retained for the plan hook below and
# as an observability signal (canonical vs ad-hoc window).
case "$WIN" in
  5m|10m|20m|1h|4h|8h|24h) PRESET=1;; *) PRESET=0;;
esac

# Testability hooks: echo resolved state and exit before any STS/DuckDB work (no
# creds/network needed). Used by tests/test_run_recap.py.
#   RECAP_PRINT_ARGS → "ASSET WIN"          (arg normalization)
#   RECAP_PRINT_PLAN → "ASSET WIN SECS PRESET"  (window parsing + preset flag)
[ -n "${RECAP_PRINT_ARGS:-}" ] && { echo "$ASSET $WIN"; exit 0; }
[ -n "${RECAP_PRINT_PLAN:-}" ] && { echo "$ASSET $WIN $SECS $PRESET"; exit 0; }
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

# Single rolling file of 5-min aggregates over trailing 24h (replaces the old
# per-window hot__recap_<window> files). The window is applied at query time via
# a bucket_at filter + aggregation, not by picking a per-window file. recap.py
# reads only the dvol_spot + volume rows here (block flow comes from the Deribit
# tape; the surface from v_vol_surface below).
REC=s3://dt-exchange-venue-data/hot/hot__recap_aggregates_5m_24h.parquet

# Vol-surface deltas (ΔATM/ΔRR/ΔFly) need a window-OPEN surface, which the recap
# aggregates file doesn't carry (it has no surface rows). Read the consolidated
# per-strike store v_vol_surface instead: its rolling _hot.parquet holds ~2h of
# 1-min snapshots (covers windows ≤1h — both endpoints in one file), and older
# opens come from the cold hour-partition that contains window-start. "Now" is
# always _hot.parquet's latest snapshot, so open+close share one pipeline (clean deltas).
# SECS is already parsed above (works for any window, not just presets).
NOW_S=$(date -u +%s); START_S=$((NOW_S - SECS)); START_MS=$((START_S * 1000))
VS_HOT=s3://dt-paradigm-data/paradigm_data/v_vol_surface/_hot.parquet
if [ "$SECS" -le 3600 ]; then
  VS_OPEN=$VS_HOT                                   # window-start within _hot's buffer
else                                                # cold partition at window-start hour
  SY=$(date -u -d "@$START_S" +%Y 2>/dev/null || date -u -r "$START_S" +%Y)
  SM=$(date -u -d "@$START_S" +%m 2>/dev/null || date -u -r "$START_S" +%m)
  SD=$(date -u -d "@$START_S" +%d 2>/dev/null || date -u -r "$START_S" +%d)
  SH=$(date -u -d "@$START_S" +%H 2>/dev/null || date -u -r "$START_S" +%H)
  VS_OPEN=s3://dt-paradigm-data/paradigm_data/v_vol_surface/base=${ASSET}/year=${SY}/month=${SM}/day=${SD}/hour=${SH}/v_vol_surface.parquet
fi

# One DuckDB session → CSVs. One statement per line; `at` is reserved → alias it.
# dvol_spot + volume come from the rolling recap-aggregates file, windowed at query
# time by bucket_at (>= START_MS) — one file serves every window, preset or not.
# Each COPY echoes `asset` through so recap.py can assert the slice is for THIS
# asset (defense in depth against any future shared-state/wrong-file regression).
cat > "$WORK/recap.sql" <<SQL
INSTALL httpfs; LOAD httpfs;
SET s3_region='ap-northeast-1';
SET s3_access_key_id='${AK}';
SET s3_secret_access_key='${SK}';
SET s3_session_token='${ST}';
COPY (SELECT asset, exchange, metric, arg_min(open, bucket_at) AS open, arg_max(close, bucket_at) AS close, max(high) AS high, min(low) AS low FROM read_parquet('${REC}') WHERE asset='${ASSET}' AND row_type='dvol_spot' AND bucket_at >= ${START_MS} GROUP BY asset, exchange, metric) TO '${WORK}/dvol_spot.csv' (HEADER, DELIMITER ',');
COPY (SELECT asset, exchange, optionType, sum(volume_sum) AS volume_sum, sum(notional_usd) AS notional, sum(buy_volume) AS buy_volume, sum(sell_volume) AS sell_volume, sum(trade_count) AS trade_count FROM read_parquet('${REC}') WHERE asset='${ASSET}' AND row_type='volume' AND bucket_at >= ${START_MS} GROUP BY asset, exchange, optionType) TO '${WORK}/volume.csv' (HEADER, DELIMITER ',');
COPY (WITH h AS (SELECT symbol, mark_iv, delta, "at" FROM read_parquet('${VS_HOT}') WHERE base='${ASSET}' AND symbol LIKE '${ASSET}-%' AND mark_iv IS NOT NULL) SELECT symbol, mark_iv, delta FROM h WHERE "at"=(SELECT max("at") FROM h)) TO '${WORK}/surface_now.csv' (HEADER, DELIMITER ',');
COPY (WITH h AS (SELECT symbol, mark_iv, delta, "at" FROM read_parquet('${VS_OPEN}') WHERE base='${ASSET}' AND symbol LIKE '${ASSET}-%' AND mark_iv IS NOT NULL) SELECT symbol, mark_iv, delta FROM h WHERE "at"=(SELECT "at" FROM h ORDER BY abs("at"-${START_MS}) LIMIT 1)) TO '${WORK}/surface_open.csv' (HEADER, DELIMITER ',');
SQL

# recap.py runs this DuckDB session in a thread concurrent with the Deribit fetch.
# No exec — the EXIT trap must fire to clean up $WORK.
[ -n "$CAP_NOTE" ] && { echo "$CAP_NOTE"; echo; }
cd "$DIR" && uv run scripts/recap.py \
  --asset "$ASSET" --window "$WIN" --csv-dir "$WORK" --duckdb-sql "$WORK/recap.sql" --render
