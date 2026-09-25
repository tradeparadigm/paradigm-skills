#!/usr/bin/env bash
# Render OHLCV candles for one market from bounded non-hot partition data.
set -euo pipefail

# Only these venues publish trade rows, which is what a candle is built from:
# deribit has perp_trade (btc, eth) and bullish has perp_trade and spot_trade.
# The other catalog venues carry option and summary feeds only, so accepting
# one would refuse late with an empty read instead of early with a reason.
# See data-discovery/references/exchange-raw.md, "Available feeds".
VENUES="deribit bullish"
INTERVALS="1m 5m 15m 1h 4h 1d"

# Render bound: the chart component holds 2000 bars, and a table nobody can
# read is not a better answer than a refusal.
MAX_BARS=2000
# Listing bound. One day-level glob per calendar day, each listing every object
# in that day — 288 rows-objects at the 5m level this reads.
#
# The bound is MEMORY, not time. On a laptop the window is fast and flat —
# 6h 4s, 1d 4s, 7d 6s, 30d 15s against the real bucket — which is what made an
# earlier 30-day default look safe. It is not: the reader holds every object's
# rows in one Arrow table, and in the openclaw container that table shares a
# 4Gi limit with the agent process and its model context. A 30-day window
# OOM-killed the container (exit 137), which does not fail the query — it kills
# openclaw, drops the user's websocket, and wipes the pod's credentials file.
#
# 7 days is what has run in-pod without a restart. Raise it only against a
# measurement taken INSIDE the container, and only once the read streams
# instead of accumulating.
#
# Short intervals hit MAX_BARS first: 7d at 5m is 2,016 bars and is refused
# before the day count matters.
MAX_DAYS="${OHLCV_MAX_DAYS:-7}"

ASSET=BTC
VENUE=deribit
INTERVAL=
WINDOW=
ASSET_SET=
VENUE_SET=

COMPONENT=
periods=()
while [ "$#" -gt 0 ]; do
  arg=$1
  # The only flag: the catalog component id the client advertised. Given one,
  # the collector prints that component's spec instead of the table.
  if [ "$arg" = "--component" ]; then
    shift
    [ "$#" -gt 0 ] || { echo "ohlcv: --component needs an id" >&2; exit 2; }
    COMPONENT=$1
    shift
    continue
  fi
  shift
  token=$(printf '%s' "$arg" | tr '[:upper:]' '[:lower:]')
  # Digits are capped in the pattern rather than checked after conversion:
  # bash truncates an oversized integer literal silently, so a 20-digit period
  # arrives as a plausible-looking number instead of an overflow to catch.
  if [[ "$token" =~ ^[1-9][0-9]{0,5}[mhd]$ ]]; then
    periods+=("$token")
  elif [[ " $VENUES " == *" $token "* ]]; then
    [ -z "$VENUE_SET" ] || { echo "ohlcv: specify one venue" >&2; exit 2; }
    VENUE=$token
    VENUE_SET=1
  elif [[ "$token" =~ ^[a-z][a-z0-9-]*$ ]]; then
    [ -z "$ASSET_SET" ] || { echo "ohlcv: specify one asset" >&2; exit 2; }
    ASSET=$(printf '%s' "$token" | tr '[:lower:]' '[:upper:]')
    ASSET_SET=1
  else
    echo "ohlcv: invalid argument '$arg' — use an asset, a venue, and Nm/Nh/Nd interval and window" >&2
    exit 2
  fi
done

case "${#periods[@]}" in
  0) WINDOW=24h ;;
  1) WINDOW=${periods[0]} ;;
  2) ;;  # resolved by magnitude below, once they can be compared in seconds
  *) echo "ohlcv: specify at most one interval and one window" >&2; exit 2 ;;
esac

case "$ASSET" in *[!A-Z0-9]*)
  echo "ohlcv: invalid asset '$ASSET'" >&2; exit 2;;
esac

to_seconds() {
  local value=$1 magnitude=${1%[mhd]} unit=${1##*[0-9]}
  case "$unit" in
    m) echo $((magnitude * 60));;
    h) echo $((magnitude * 3600));;
    d) echo $((magnitude * 86400));;
    *) echo "ohlcv: bad period '$value' — use e.g. 30m, 8h, 2d" >&2; exit 2;;
  esac
}

# Two periods resolve by magnitude, not by position: the shorter is the
# interval and the longer the window. Position would make the command only
# half order-independent — "1h 24h" and "24h 1h" would mean different things,
# and the second would be refused as a 24h interval rather than understood.
if [ "${#periods[@]}" -eq 2 ]; then
  first=$(to_seconds "${periods[0]}")
  second=$(to_seconds "${periods[1]}")
  if [ "$first" -le "$second" ]; then
    INTERVAL=${periods[0]}; WINDOW=${periods[1]}
  else
    INTERVAL=${periods[1]}; WINDOW=${periods[0]}
  fi
fi

SPAN=$(to_seconds "$WINDOW")
# Negative means the multiplication overflowed.
if [ "$SPAN" -le 0 ]; then
  echo "ohlcv: window '$WINDOW' is out of range" >&2
  exit 2
fi

# Derived, not substituted: the header states whichever interval was used, and
# an interval the user actually typed is never replaced. Note 1d is a legal
# INTERVAL here, so it is never normalised to 24h the way /recap does.
if [ -z "$INTERVAL" ]; then
  if [ "$SPAN" -le 14400 ]; then INTERVAL=1m
  elif [ "$SPAN" -le 259200 ]; then INTERVAL=1h
  else INTERVAL=1d
  fi
fi

[[ " $INTERVALS " == *" $INTERVAL "* ]] || {
  echo "ohlcv: unsupported interval '$INTERVAL' — use one of $INTERVALS" >&2
  exit 2
}

STEP=$(to_seconds "$INTERVAL")
if [ "$STEP" -le 0 ] || [ "$SPAN" -le "$STEP" ]; then
  echo "ohlcv: window '$WINDOW' must be longer than interval '$INTERVAL'" >&2
  exit 2
fi

BARS=$((SPAN / STEP))
if [ "$BARS" -gt "$MAX_BARS" ]; then
  echo "ohlcv: '$WINDOW' at '$INTERVAL' is $BARS bars, over the $MAX_BARS-bar render bound." >&2
  echo "ohlcv: report this and ask which window or interval to use." >&2
  exit 2
fi

DAYS=$(((SPAN + 86399) / 86400))
if [ "$DAYS" -gt "$MAX_DAYS" ]; then
  echo "ohlcv: '$WINDOW' spans $DAYS day partitions, over the $MAX_DAYS-day read bound." >&2
  echo "ohlcv: this is a memory bound, not a limit on what the data retains —" >&2
  echo "ohlcv: a wider window has OOM-killed the agent container. Report it and" >&2
  echo "ohlcv: ask which window to use; do not re-run at the bound and do not" >&2
  echo "ohlcv: coarsen the interval to squeeze under it." >&2
  exit 2
fi

[ -n "${OHLCV_PRINT_ARGS:-}" ] && { echo "$ASSET $VENUE $INTERVAL $WINDOW${COMPONENT:+ $COMPONENT}"; exit 0; }
[ -n "${OHLCV_PRINT_PLAN:-}" ] && { echo "$ASSET $VENUE $INTERVAL $WINDOW $SPAN $BARS $DAYS"; exit 0; }

DIR="$(cd "$(dirname "$0")/.." && pwd)"
set -- --asset "$ASSET" --venue "$VENUE" \
       --interval "$INTERVAL" --window "$WINDOW" --render
[ -n "$COMPONENT" ] && set -- "$@" --component "$COMPONENT"
exec uv run "$DIR/scripts/collect_ohlcv.py" "$@"
