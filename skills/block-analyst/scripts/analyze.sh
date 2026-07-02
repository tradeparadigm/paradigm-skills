#!/usr/bin/env bash
# analyze.sh — the entire block analysis in one command, so the agent types one
# short line instead of regenerating a ~50-line bootstrap+SQL block and then
# orchestrating multi-round fetches + greek reasoning by hand (that was 40–70s of
# model work). Does: STS bootstrap → ONE DuckDB scan of the tape (resolve FILL +
# 30d same-structure HIST, ID only) → analyze.py (concurrent Deribit fetch, net
# greeks, render). Its stdout IS the finished block.
#
# Usage: bash scripts/analyze.sh <rfq_id>      e.g. analyze.sh r_3FvzJWGF…
set -uo pipefail

RAW="${1:-}"
[ -z "$RAW" ] && { echo "usage: analyze.sh <rfq_id>"; exit 2; }
# Only the ID is authoritative; any <rfq description> after it is ignored here.
CORE=$(printf '%s' "$RAW" | sed -E 's/^(DRFQv2-|GRFQ-)//')
DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT=/tmp/analyze; mkdir -p "$OUT"

# Testability hook: print the resolved core id and exit (no creds/network).
[ -n "${ANALYZE_PRINT_ID:-}" ] && { echo "$CORE"; exit 0; }

# STS bootstrap (IRSA → temporary creds; see paradigm-data-discovery skill).
TOKEN=$(cat "$AWS_WEB_IDENTITY_TOKEN_FILE")
CREDS=$(curl -s "https://sts.ap-northeast-1.amazonaws.com/?Action=AssumeRoleWithWebIdentity&Version=2011-06-15&RoleArn=${AWS_ROLE_ARN}&RoleSessionName=duckdb&WebIdentityToken=${TOKEN}")
AK=$(printf '%s' "$CREDS" | grep -o '<AccessKeyId>[^<]*'     | cut -d'>' -f2)
SK=$(printf '%s' "$CREDS" | grep -o '<SecretAccessKey>[^<]*' | cut -d'>' -f2)
ST=$(printf '%s' "$CREDS" | grep -o '<SessionToken>[^<]*'    | cut -d'>' -f2)

TAPE=s3://terminal-dime-prod/paradigm_data/paradigm_trade_tape_slim.csv.gz

# ONE DuckDB session: scan the gzip once into a temp table, COPY the FILL rows
# (by RFQ_ID) and the 30d HIST recurrence (self-matched on the FILL's own
# PRODUCT + normalized DESCRIPTION — no description tokens, ID-authoritative).
cat > "$OUT/q.sql" <<SQL
INSTALL httpfs; LOAD httpfs;
SET s3_region='ap-northeast-1';
SET s3_access_key_id='${AK}';
SET s3_secret_access_key='${SK}';
SET s3_session_token='${ST}';
CREATE TEMP TABLE tape AS
SELECT DATE, TIME, AUCTION, PRODUCT, DESCRIPTION, QTY, PRICE, REF_PRICE, SIDE,
       QUOTE_CURRENCY, NOTIONAL_VOLUME_USD, RFQ_ID, TRADE_ID, BLOCK_TRADE_ID,
       UPPER(REPLACE(DESCRIPTION,' ','')) AS DESC_N
FROM read_csv_auto('${TAPE}')
WHERE RFQ_ID LIKE '%${CORE}%'
   OR DATE >= (CURRENT_DATE - INTERVAL 30 DAY);
COPY (SELECT PRODUCT, DESCRIPTION, QTY, PRICE, REF_PRICE, SIDE, QUOTE_CURRENCY,
             RFQ_ID, TRADE_ID, BLOCK_TRADE_ID
      FROM tape WHERE RFQ_ID LIKE '%${CORE}%') TO '${OUT}/fill.csv' (HEADER, DELIMITER ',');
COPY (SELECT DATE, TIME, PRODUCT, DESCRIPTION, QTY, PRICE, REF_PRICE, SIDE, BLOCK_TRADE_ID
      FROM tape
      WHERE PRODUCT IN (SELECT PRODUCT FROM tape WHERE RFQ_ID LIKE '%${CORE}%')
        AND DESC_N  IN (SELECT DESC_N  FROM tape WHERE RFQ_ID LIKE '%${CORE}%')
      ORDER BY DATE DESC, TIME DESC) TO '${OUT}/hist.csv' (HEADER, DELIMITER ',');
SQL

duckdb < "$OUT/q.sql" >/dev/null 2>"$OUT/duck.err" || {
  echo "tape query failed: $(head -c 200 "$OUT/duck.err")"; exit 1; }

cd "$DIR" && exec uv run scripts/analyze.py --csv-dir "$OUT" --render
