#!/usr/bin/env bash
# Collect direct-data evidence for one RFQ. stdout is one JSON document.
set -euo pipefail

SUPPLIED_ID=${1:-}
[ -n "$SUPPLIED_ID" ] || { echo "usage: analyze.sh <rfq_id>" >&2; exit 2; }
CORE=$(printf '%s' "$SUPPLIED_ID" | sed -E 's/^(DRFQv2-|GRFQ-)//')
case "$CORE" in
  ''|*[!A-Za-z0-9_-]*) echo "analyze: invalid rfq_id" >&2; exit 2;;
esac

[ -n "${ANALYZE_PRINT_ID:-}" ] && { echo "$CORE"; exit 0; }

DIR="$(cd "$(dirname "$0")/.." && pwd)"
exec uv run "$DIR/scripts/collect_analysis.py" --rfq-id "$SUPPLIED_ID"
