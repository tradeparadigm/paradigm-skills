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
  m) SPAN=$((MAGNITUDE * 60));;
  h) SPAN=$((MAGNITUDE * 3600));;
  d) SPAN=$((MAGNITUDE * 86400));;
  *) echo "recap: bad window '$WINDOW' — use e.g. 30m, 8h, 2d" >&2; exit 2;;
esac
# No 24h clamp — partitions serve any window — but the execution tape keeps 30
# days, and an unbounded window globs every hour of it for every venue.
# -le 0 catches the multiplication overflowing to a negative span.
if [ "$SPAN" -le 0 ] || [ "$SPAN" -gt 2592000 ]; then
  echo "recap: window '$WINDOW' exceeds 30d — the execution tape keeps 30 days." >&2
  echo "recap: a smaller container enforces a narrower ceiling still; the" >&2
  echo "recap: collector names whichever limit it applied." >&2
  echo "recap: report this and ask which window to use. Do not re-run at 30d:" >&2
  echo "recap: it lists ~10,000 partitions, takes minutes, and was not asked for." >&2
  exit 2
fi
[ -n "${RECAP_PRINT_ARGS:-}" ] && { echo "$ASSET $WINDOW"; exit 0; }
[ -n "${RECAP_PRINT_PLAN:-}" ] && { echo "$ASSET $WINDOW $SPAN direct"; exit 0; }

DIR="$(cd "$(dirname "$0")/.." && pwd)"
exec uv run "$DIR/scripts/collect_recap.py" --asset "$ASSET" --window "$WINDOW" --render
