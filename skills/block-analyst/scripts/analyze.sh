#!/usr/bin/env bash
# analyze.sh — the entire block analysis in one command, so the agent types one
# short line instead of orchestrating multi-round fetches and greek reasoning by
# hand. Does: resolve the RFQ off the execution tape (collect_analysis.py) →
# analyze.py (concurrent Deribit fetch, net greeks, render). Its stdout IS the
# finished block.
#
# The STS bootstrap and the inline DuckDB scan are gone: the shared reader in
# data-discovery resolves credentials through the chain, so no keys are written
# to a temp SQL file, and it reads the daily execution partitions rather than
# hot__paradigm_trade_tape_30d.
#
# Usage: bash scripts/analyze.sh <rfq_id>      e.g. analyze.sh r_3FvzJWGF…
set -uo pipefail

RAW="${1:-}"
[ -z "$RAW" ] && { echo "usage: analyze.sh <rfq_id>"; exit 2; }
# Only the ID is authoritative; any <rfq description> after it is ignored here.
CORE=$(printf '%s' "$RAW" | sed -E 's/^(DRFQv2-|GRFQ-)//')
case "$CORE" in
  ''|*[!A-Za-z0-9_-]*) echo "invalid rfq_id — expected an r_… id (letters/digits/_/- only)"; exit 2 ;;
esac
DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Testability hook: print the resolved core id and exit (no creds/network).
[ -n "${ANALYZE_PRINT_ID:-}" ] && { echo "$CORE"; exit 0; }

OUT=$(mktemp -d "${TMPDIR:-/tmp}/analyze.XXXXXX")
trap 'rm -rf "$OUT"' EXIT

# Resolve FIRST and stop on failure. analyze.py reports a missing fill.csv as
# "RFQ not resolved (not on Paradigm tape)", which would blame the trade for a
# producer outage — so a reader refusal has to surface as itself, here.
# `status=$?` after `if ! cmd` reads the NEGATION's result (always 0), which
# would send every failure to the default branch. Capture, then test.
uv run "$DIR/scripts/collect_analysis.py" "$RAW" --out-dir "$OUT" >/dev/null
status=$?
if [ "$status" -ne 0 ]; then
  case "$status" in
    3) echo "ambiguous rfq_id — re-run with the exact DRFQv2- or GRFQ- prefix" ;;
    5) echo "rfq_id not found on the execution tape for the trailing 30 days" ;;
    6) echo "rfq_id not found, but the tape's coverage is incomplete — this is missing" \
            "evidence, not a missing trade. Retry after the next sync." ;;
    *) echo "execution tape unavailable — the analysis cannot run. This is a data" \
            "pipeline failure, not an unknown RFQ; see SKILL.md for the manual fallback." ;;
  esac
  exit "$status"
fi

# No exec — the EXIT trap must survive to clean the CSVs after the render.
cd "$DIR" && uv run scripts/analyze.py --csv-dir "$OUT" --render
