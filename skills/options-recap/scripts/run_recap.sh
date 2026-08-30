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

# /recap is order-independent: a window-shaped token (digits + trailing letter)
# is the window wherever it appears; the other token is the asset.
A0=${ARGS[0]:-}
A1=${ARGS[1]:-}
lower0=$(printf '%s' "$A0" | tr '[:upper:]' '[:lower:]')
lower1=$(printf '%s' "$A1" | tr '[:upper:]' '[:lower:]')
if [[ "$lower0" =~ ^-?[0-9]+[a-z]+$ ]] && ! [[ "$lower1" =~ ^-?[0-9]+[a-z]+$ ]]; then
  tmp=$A0; A0=$A1; A1=$tmp
fi

ASSET=$(printf '%s' "${A0:-BTC}" | tr '[:lower:]' '[:upper:]')
WINDOW=$(printf '%s' "${A1:-24h}" | tr '[:upper:]' '[:lower:]')
[ "$WINDOW" = "1d" ] && WINDOW=24h
case "$ASSET" in *[!A-Z0-9]*) echo "recap: invalid asset '$ASSET'" >&2; exit 2;; esac
[[ "$WINDOW" =~ ^[1-9][0-9]{0,6}[mhd]$ ]] || {
  echo "recap: bad window '$WINDOW' — use e.g. 30m, 8h, 2d" >&2; exit 2;
}
MAGNITUDE=${WINDOW%[mhd]}
UNIT=${WINDOW##*[0-9]}
# Not named SECONDS: that is bash's elapsed-time special variable, and
# assigning it makes later reads drift by wall-clock time.
case "$UNIT" in
  m) WINDOW_SECONDS=$((MAGNITUDE * 60));;
  h) WINDOW_SECONDS=$((MAGNITUDE * 3600));;
  d) WINDOW_SECONDS=$((MAGNITUDE * 86400));;
  *) echo "recap: bad window '$WINDOW' — use e.g. 30m, 8h, 2d" >&2; exit 2;;
esac
[ "$WINDOW_SECONDS" -le 2678400 ] || {
  echo "recap: window too large — max 31d" >&2; exit 2;
}
[ -n "${RECAP_PRINT_ARGS:-}" ] && { echo "$ASSET $WINDOW"; exit 0; }
[ -n "${RECAP_PRINT_PLAN:-}" ] && { echo "$ASSET $WINDOW $WINDOW_SECONDS direct"; exit 0; }

DIR="$(cd "$(dirname "$0")/.." && pwd)"
exec uv run "$DIR/scripts/collect_recap.py" --asset "$ASSET" --window "$WINDOW"
