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
# Measured against the real bucket (deribit BTC, 1h bars, SSO credentials on a
# laptop): 6h 4s, 1d 4s, 2d 5s, 7d 6s, 14d 9s, 30d 15s. The window's cost is
# close to flat because the reader fetches up to 512 objects concurrently, so
# 30d is comfortable. An earlier 7-day bound was inherited from the recap
# skill's 7m08s/30d option-chain measurement, which does not describe this read
# at all — candle partitions are one small object per five minutes, not a chain
# snapshot per instrument.
#
# Short intervals are bounded by MAX_BARS rather than by this: 7d at 5m is
# 2,016 bars and is refused before the day count matters.
MAX_DAYS="${OHLCV_MAX_DAYS:-30}"

ASSET=BTC
VENUE=deribit
INTERVAL=
WINDOW=
ASSET_SET=
VENUE_SET=

periods=()
for arg in "$@"; do
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
  echo "ohlcv: this is a listing-cost bound — each day lists every object in it —" >&2
  echo "ohlcv: not a limit on what the data retains. Report it and ask which" >&2
  echo "ohlcv: window to use; do not re-run at the bound or coarsen the interval." >&2
  exit 2
fi

[ -n "${OHLCV_PRINT_ARGS:-}" ] && { echo "$ASSET $VENUE $INTERVAL $WINDOW"; exit 0; }
[ -n "${OHLCV_PRINT_PLAN:-}" ] && { echo "$ASSET $VENUE $INTERVAL $WINDOW $SPAN $BARS $DAYS"; exit 0; }

DIR="$(cd "$(dirname "$0")/.." && pwd)"
exec uv run "$DIR/scripts/collect_ohlcv.py" \
  --asset "$ASSET" --venue "$VENUE" \
  --interval "$INTERVAL" --window "$WINDOW" --render
