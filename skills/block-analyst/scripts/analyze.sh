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
# Charset gate: the id goes into SQL below — reject anything but [A-Za-z0-9_-]
# (quotes/%/etc. would break out of the LIKE literal).
case "$CORE" in
  ''|*[!A-Za-z0-9_-]*) echo "invalid rfq_id — expected an r_… id (letters/digits/_/- only)"; exit 2 ;;
esac
DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Testability hook: print the resolved core id and exit (no creds/network).
[ -n "${ANALYZE_PRINT_ID:-}" ] && { echo "$CORE"; exit 0; }

# Private per-run workdir (mktemp -d → 0700): q.sql carries the STS creds, so no
# fixed shared path, no cross-run races, and everything is removed on exit.
OUT=$(mktemp -d "${TMPDIR:-/tmp}/analyze.XXXXXX")
trap 'rm -rf "$OUT"' EXIT

# STS bootstrap (IRSA → temporary creds; see paradigm-data-discovery skill).
if [ -z "${AWS_WEB_IDENTITY_TOKEN_FILE:-}" ] || [ -z "${AWS_ROLE_ARN:-}" ]; then
  echo "no IRSA env (AWS_WEB_IDENTITY_TOKEN_FILE / AWS_ROLE_ARN) — no tape access; use the SKILL.md manual fallback"
  exit 1
fi
# POST with the token read straight from its file keeps it out of argv/ps.
CREDS=$(curl -s --max-time 20 -X POST "https://sts.ap-northeast-1.amazonaws.com/" \
  --data "Action=AssumeRoleWithWebIdentity&Version=2011-06-15&RoleSessionName=duckdb" \
  --data-urlencode "RoleArn=${AWS_ROLE_ARN}" \
  --data-urlencode "WebIdentityToken@${AWS_WEB_IDENTITY_TOKEN_FILE}")
AK=$(printf '%s' "$CREDS" | grep -o '<AccessKeyId>[^<]*'     | cut -d'>' -f2)
SK=$(printf '%s' "$CREDS" | grep -o '<SecretAccessKey>[^<]*' | cut -d'>' -f2)
ST=$(printf '%s' "$CREDS" | grep -o '<SessionToken>[^<]*'    | cut -d'>' -f2)
if [ -z "$AK" ] || [ -z "$SK" ] || [ -z "$ST" ]; then
  echo "STS bootstrap failed: $(printf '%s' "$CREDS" | head -c 200)"; exit 1
fi

TAPE=s3://dt-paradigm-data/paradigm_data/paradigm_trade_tape_slim.csv.gz
# `_` is a LIKE wildcard and ids are r_…-style — escape it so the match is literal.
CORE_SQL=${CORE//_/\\_}

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
WHERE RFQ_ID LIKE '%${CORE_SQL}%' ESCAPE '\'
   OR DATE >= (CURRENT_DATE - INTERVAL 30 DAY);
COPY (SELECT PRODUCT, DESCRIPTION, QTY, PRICE, REF_PRICE, SIDE, QUOTE_CURRENCY,
             RFQ_ID, TRADE_ID, BLOCK_TRADE_ID
      FROM tape WHERE RFQ_ID LIKE '%${CORE_SQL}%' ESCAPE '\') TO '${OUT}/fill.csv' (HEADER, DELIMITER ',');
COPY (SELECT DATE, TIME, PRODUCT, DESCRIPTION, QTY, PRICE, REF_PRICE, SIDE, BLOCK_TRADE_ID
      FROM tape
      WHERE PRODUCT IN (SELECT PRODUCT FROM tape WHERE RFQ_ID LIKE '%${CORE_SQL}%' ESCAPE '\')
        AND DESC_N  IN (SELECT DESC_N  FROM tape WHERE RFQ_ID LIKE '%${CORE_SQL}%' ESCAPE '\')
      ORDER BY DATE DESC, TIME DESC) TO '${OUT}/hist.csv' (HEADER, DELIMITER ',');
SQL

duckdb < "$OUT/q.sql" >/dev/null 2>"$OUT/duck.err" || {
  echo "tape query failed: $(head -c 200 "$OUT/duck.err")"; exit 1; }

# No exec — the EXIT trap must survive to clean the creds/CSVs after the render.
cd "$DIR" && uv run scripts/analyze.py --csv-dir "$OUT" --render
