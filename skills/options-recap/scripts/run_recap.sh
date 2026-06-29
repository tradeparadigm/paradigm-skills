#!/usr/bin/env bash
# run_recap.sh — the entire live recap in one command, so the agent types one
# short line instead of regenerating a ~50-line bootstrap+SQL block (that
# generation was ~12s of the old run). Does: STS bootstrap, one DuckDB session
# into CSVs, then recap.py --render. Its stdout IS the final four-section recap.
#
# Usage: bash scripts/run_recap.sh <ASSET> <WINDOW>     e.g. run_recap.sh BTC 8h
set -uo pipefail

ASSET=$(printf '%s' "${1:-BTC}" | tr '[:lower:]' '[:upper:]')
WIN="${2:-8h}"; WIN="${WIN/1d/24h}"          # 1d → 24h
DIR="$(cd "$(dirname "$0")/.." && pwd)"      # skill dir (scripts/..)
mkdir -p /tmp/recap

# STS bootstrap (IRSA → temporary creds; see paradigm-data-discovery skill).
TOKEN=$(cat "$AWS_WEB_IDENTITY_TOKEN_FILE")
CREDS=$(curl -s "https://sts.ap-northeast-1.amazonaws.com/?Action=AssumeRoleWithWebIdentity&Version=2011-06-15&RoleArn=${AWS_ROLE_ARN}&RoleSessionName=duckdb&WebIdentityToken=${TOKEN}")
AK=$(printf '%s' "$CREDS" | grep -o '<AccessKeyId>[^<]*'     | cut -d'>' -f2)
SK=$(printf '%s' "$CREDS" | grep -o '<SecretAccessKey>[^<]*' | cut -d'>' -f2)
ST=$(printf '%s' "$CREDS" | grep -o '<SessionToken>[^<]*'    | cut -d'>' -f2)

SIG=s3://terminal-dime-prod/paradigm_data/hot/hot__market_signals_1m.parquet
REC=s3://terminal-dime-prod/paradigm_data/hot/hot__recap_${WIN}.parquet

# One DuckDB session → CSVs. One statement per line; `at` is reserved → alias it.
cat > /tmp/recap.sql <<SQL
INSTALL httpfs; LOAD httpfs;
SET s3_region='ap-northeast-1';
SET s3_access_key_id='${AK}';
SET s3_secret_access_key='${SK}';
SET s3_session_token='${ST}';
COPY (SELECT signal_type, exchange, expiry, value, atm_call_iv, atm_put_iv, underlying_price, call_volume, put_volume, buy_volume, sell_volume, notional, trade_count, "at" AS at_ms FROM read_parquet('${SIG}') WHERE asset='${ASSET}') TO '/tmp/recap/snapshot.csv' (HEADER, DELIMITER ',');
COPY (SELECT exchange, metric, open, close, high, low FROM read_parquet('${REC}') WHERE asset='${ASSET}' AND row_type='dvol_spot') TO '/tmp/recap/dvol_spot.csv' (HEADER, DELIMITER ',');
COPY (SELECT exchange, optionType, volume_sum, notional, buy_volume, sell_volume, trade_count FROM read_parquet('${REC}') WHERE asset='${ASSET}' AND row_type='volume') TO '/tmp/recap/volume.csv' (HEADER, DELIMITER ',');
COPY (SELECT block_id, notional, volume_sum, leg_count, avg_iv FROM read_parquet('${REC}') WHERE asset='${ASSET}' AND row_type='block') TO '/tmp/recap/block.csv' (HEADER, DELIMITER ',');
COPY (SELECT expiry, strike, optionType, markIV_close, delta, openInterest, underlying_price FROM read_parquet('${REC}') WHERE asset='${ASSET}' AND row_type='surface' AND exchange='deribit') TO '/tmp/recap/surface.csv' (HEADER, DELIMITER ',');
SQL

duckdb < /tmp/recap.sql > /tmp/recap/duck.log 2>&1 \
  || echo "WARN: duckdb failed (see /tmp/recap/duck.log) — hot sections will read No data" >&2

cd "$DIR" && exec uv run scripts/recap.py --asset "$ASSET" --window "$WIN" --csv-dir /tmp/recap --render
