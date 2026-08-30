#!/usr/bin/env bash
# Collect bounded direct-data evidence for an options recap.
# stdout is one JSON document; diagnostics go to stderr.
set -euo pipefail

ARGS=()
for arg in "$@"; do
  case "$(printf '%s' "$arg" | tr '[:upper:]' '[:lower:]')" in
    option|options) ;;
    *) ARGS+=("$arg") ;;
  esac
done

ASSET=$(printf '%s' "${ARGS[0]:-BTC}" | tr '[:lower:]' '[:upper:]')
WINDOW=$(printf '%s' "${ARGS[1]:-24h}" | tr '[:upper:]' '[:lower:]')
[ "$WINDOW" = "1d" ] && WINDOW=24h
case "$ASSET" in *[!A-Z0-9]*) echo "recap: invalid asset '$ASSET'" >&2; exit 2;; esac
[[ "$WINDOW" =~ ^[1-9][0-9]*[mhd]$ ]] || {
  echo "recap: bad window '$WINDOW' — use e.g. 30m, 8h, 2d" >&2; exit 2;
}
MAGNITUDE=${WINDOW%[mhd]}
UNIT=${WINDOW##*[0-9]}
case "$UNIT" in
  m) SECONDS=$((MAGNITUDE * 60));;
  h) SECONDS=$((MAGNITUDE * 3600));;
  d) SECONDS=$((MAGNITUDE * 86400));;
  *) echo "recap: bad window '$WINDOW' — use e.g. 30m, 8h, 2d" >&2; exit 2;;
esac
[ -n "${RECAP_PRINT_ARGS:-}" ] && { echo "$ASSET $WINDOW"; exit 0; }
[ -n "${RECAP_PRINT_PLAN:-}" ] && { echo "$ASSET $WINDOW $SECONDS direct"; exit 0; }

DIR="$(cd "$(dirname "$0")/.." && pwd)"
exec uv run "$DIR/scripts/collect_recap.py" --asset "$ASSET" --window "$WINDOW"
