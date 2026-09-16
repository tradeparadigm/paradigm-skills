#!/usr/bin/env bash
# Render the established recap from bounded non-hot source data.
set -euo pipefail

ASSET=BTC
WINDOW=24h
ASSET_SET=
WINDOW_SET=
for arg in "$@"; do
  token=$(printf '%s' "$arg" | tr '[:upper:]' '[:lower:]')
  case "$token" in
    option|options) ;;
    *)
      if [[ "$token" =~ ^[1-9][0-9]*[mhd]$ ]]; then
        [ -z "$WINDOW_SET" ] || { echo "recap: specify one window" >&2; exit 2; }
        WINDOW=$token
        WINDOW_SET=1
      elif [[ "$token" =~ ^[a-z][a-z0-9]*$ ]] && [ -z "$ASSET_SET" ]; then
        ASSET=$(printf '%s' "$token" | tr '[:lower:]' '[:upper:]')
        ASSET_SET=1
      else
        echo "recap: invalid argument '$arg' — use an asset and Nm/Nh/Nd window" >&2
        exit 2
      fi ;;
  esac
done

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
exec uv run "$DIR/scripts/collect_recap.py" --asset "$ASSET" --window "$WINDOW" --render
