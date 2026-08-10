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

# Snowflake-free Paradigm tape: hot__paradigm_trade_tape_30d.parquet (row_type=
# 'paradigm_trade', leg grain, trailing 30 days — exactly the HIST horizon),
# built by the exchange-venue-data paradigm-trade CronJob from the
# Airbyte→S3 UM landing. Replaces the Snowflake-egressed
# paradigm_trade_tape_slim.csv.gz; the temp table below aliases its columns
# to the old tape names so fill/hist stay shape-identical downstream.
#
# QUOTE_CURRENCY is DERIVED, not renamed. The legacy column was the premium
# currency (BTC/ETH/USD); the parquet has no such column — `asset` is the
# UNDERLYING. Aliasing asset onto it fed the wrong value to analyze_core.offset,
# whose `quote not in _STABLE_QUOTES and abs(ref) < 1` branch then reports a
# sub-$1 USDC premium in bps (the -324953-bps class of bug). Deribit BTC/ETH
# options are coin-quoted (inverse); Paradex, Bullish and Deribit's alt books
# are USDC-linear, so everything else derives USDC — the STABLE branch, which is
# also the safe default when in doubt.
#
# INVERTED DEFAULT: the coin quote requires a POSITIVE inverse signal, and
# everything else — including anything unknown — lands on 'USDC', the STABLE
# branch, which routes offset() to percent. That is the safe direction: reporting
# a coin fraction in percent understates, whereas reporting a dollar price in bps
# is the -324953-bps bug. `coalesce(instrument_name,'')` in the old form was a
# no-op (SQL CASE already treats a NULL WHEN as not-true), so a NULL name still
# derived the coin; requiring `IS NOT NULL` positively is what actually fixes it.
# This is also the right shape given the predicate is a no-op on live data (see
# below) — defensive code should fail safe, not merely be present.
#
# venue alone cannot separate
# Deribit's inverse book from its USDC-linear sibling: both are venue 'DBT', so a
# venue-only rule derived BTC for a USDC-linear BTC option and a sub-$1 premium
# went down the bps branch anyway. Verified against the live tape — 50 of 1782
# option rows carry USDC in instrument_name, so the distinction is present in the
# data and does not need to be inferred.
#
# Venue comes from the PRODUCT suffix, not the `venue` column, so this agrees
# with parse_product() / _tape_venue_code() / rfq-lookup.md rather than
# introducing a second source of truth on the same rows.
#
# Fix properly by carrying a real currency column on the hot tape; this
# derivation is the correct-today stopgap.
TAPE=s3://dt-exchange-venue-data/hot/hot__paradigm_trade_tape_30d.parquet
# `_` is a LIKE wildcard and ids are r_…-style — escape it so the match is literal.
CORE_SQL=${CORE//_/\\_}

# ONE DuckDB session: scan the tape once into a temp table, COPY the FILL rows
# (by RFQ_ID) and the 30d HIST recurrence (self-matched on the FILL's own
# PRODUCT + normalized DESCRIPTION — no description tokens, ID-authoritative).
cat > "$OUT/q.sql" <<SQL
INSTALL httpfs; LOAD httpfs;
SET s3_region='ap-northeast-1';
SET s3_access_key_id='${AK}';
SET s3_secret_access_key='${SK}';
SET s3_session_token='${ST}';
CREATE TEMP TABLE tape AS
SELECT strftime(CAST(traded_at_iso AS TIMESTAMP), '%Y-%m-%d') AS DATE,
       strftime(CAST(traded_at_iso AS TIMESTAMP), '%H:%M:%S') AS TIME,
       auction AS AUCTION, product AS PRODUCT, description AS DESCRIPTION,
       quantity AS QTY, trade_price AS PRICE, mark_price AS REF_PRICE,
       taker_side AS SIDE,
       CASE WHEN upper(trim(split_part(coalesce(product,''), ' - ', 2))) = 'DBT' AND upper(coalesce(asset,'')) IN ('BTC','ETH') AND instrument_name IS NOT NULL AND upper(instrument_name) NOT LIKE '%USDC%' THEN upper(asset) ELSE 'USDC' END AS QUOTE_CURRENCY, notional_volume_usd AS NOTIONAL_VOLUME_USD,
       rfq_id AS RFQ_ID, trade_id AS TRADE_ID, block_trade_id AS BLOCK_TRADE_ID,
       UPPER(REPLACE(description,' ','')) AS DESC_N
FROM read_parquet('${TAPE}')
WHERE row_type='paradigm_trade';
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
