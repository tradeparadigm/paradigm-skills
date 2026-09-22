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
# collect's own stderr IS the message, relayed onto stdout unchanged. Re-wording
# it here produced two differently worded lines for one failure — and the arm for
# exit 6 still said "Retry after the next sync" after collect_analysis.py had been
# rewritten to stop saying exactly that. One author, one sentence.
# `status=$?` after `if ! cmd` reads the NEGATION's result (always 0), which
# would send every failure to the default branch. Capture, then test.
# collect's stderr goes to a FILE, not through `2>&1 >/dev/null`: that captures
# uv's stderr too, so a cold package cache put "Installed 13 packages in 106ms"
# on stdout above the block — and SKILL.md tells the model stdout is its entire
# reply. Only lines collect_analysis.py itself authored are relayed.
err="$OUT/.collect.err"
uv run "$DIR/scripts/collect_analysis.py" "$RAW" --out-dir "$OUT" >/dev/null 2>"$err"
status=$?
note=$(grep '^analyze: ' "$err" 2>/dev/null)
if [ "$status" -ne 0 ]; then
  if [ -n "$note" ]; then
    printf '%s\n' "$note"
  else
    # collect died before it could speak (import error, uv, argparse): the filter
    # alone would leave stdout empty, so say so and relay the tail minus uv chatter.
    printf 'analyze: collect_analysis.py failed (exit %s) before reporting:\n' "$status"
    grep -vE '^ *(Installed|Uninstalled|Downloading|Downloaded|Resolved|Prepared|Audited) ' \
      "$err" 2>/dev/null | tail -n 5
  fi
  exit "$status"
fi
# Exit 0 can still carry a note — `recurrence is a FLOOR`. It is part of the
# answer, so it goes to stdout with the block rather than being swallowed.
[ -n "$note" ] && printf '%s\n' "$note"

# No exec — the EXIT trap must survive to clean the CSVs after the render.
cd "$DIR" && uv run scripts/analyze.py --csv-dir "$OUT" --render
