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
WIN="${2:-8h}"; WIN="${WIN/1d/24h}"          # 1d → 24h
# Testability hook: echo the resolved args and exit before any STS/DuckDB work
# (no creds/network needed). Used by tests/test_run_recap.py.
[ -n "${RECAP_PRINT_ARGS:-}" ] && { echo "$ASSET $WIN"; exit 0; }
DIR="$(cd "$(dirname "$0")/.." && pwd)"      # skill dir (scripts/..)
mkdir -p /tmp/recap

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
case "$WIN" in
  5m) SECS=300;; 10m) SECS=600;; 20m) SECS=1200;; 1h) SECS=3600;;
  4h) SECS=14400;; 8h) SECS=28800;; 24h) SECS=86400;; *) SECS=28800;;
esac
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
cat > /tmp/recap.sql <<SQL
INSTALL httpfs; LOAD httpfs;
SET s3_region='ap-northeast-1';
SET s3_access_key_id='${AK}';
SET s3_secret_access_key='${SK}';
SET s3_session_token='${ST}';
COPY (SELECT exchange, metric, arg_min(open, bucket_at) AS open, arg_max(close, bucket_at) AS close, max(high) AS high, min(low) AS low FROM read_parquet('${REC}') WHERE asset='${ASSET}' AND row_type='dvol_spot' AND bucket_at >= ${START_MS} GROUP BY exchange, metric) TO '/tmp/recap/dvol_spot.csv' (HEADER, DELIMITER ',');
COPY (SELECT exchange, optionType, sum(volume_sum) AS volume_sum, sum(notional_usd) AS notional, sum(buy_volume) AS buy_volume, sum(sell_volume) AS sell_volume, sum(trade_count) AS trade_count FROM read_parquet('${REC}') WHERE asset='${ASSET}' AND row_type='volume' AND bucket_at >= ${START_MS} GROUP BY exchange, optionType) TO '/tmp/recap/volume.csv' (HEADER, DELIMITER ',');
COPY (WITH h AS (SELECT symbol, mark_iv, delta, "at" FROM read_parquet('${VS_HOT}') WHERE base='${ASSET}' AND symbol LIKE '${ASSET}-%' AND mark_iv IS NOT NULL) SELECT symbol, mark_iv, delta FROM h WHERE "at"=(SELECT max("at") FROM h)) TO '/tmp/recap/surface_now.csv' (HEADER, DELIMITER ',');
COPY (WITH h AS (SELECT symbol, mark_iv, delta, "at" FROM read_parquet('${VS_OPEN}') WHERE base='${ASSET}' AND symbol LIKE '${ASSET}-%' AND mark_iv IS NOT NULL) SELECT symbol, mark_iv, delta FROM h WHERE "at"=(SELECT "at" FROM h ORDER BY abs("at"-${START_MS}) LIMIT 1)) TO '/tmp/recap/surface_open.csv' (HEADER, DELIMITER ',');
SQL

# recap.py runs this DuckDB session in a thread concurrent with the Deribit fetch.
cd "$DIR" && exec uv run scripts/recap.py \
  --asset "$ASSET" --window "$WIN" --csv-dir /tmp/recap --duckdb-sql /tmp/recap.sql --render
