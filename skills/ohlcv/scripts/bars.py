"""
bars.py — deterministic bar construction for the ohlcv skill.

Single source of truth for turning trade rows into OHLCV bars, for finding the
periods that produced none, and for the summary line. The collector and the
renderer both import from here so the arithmetic never forks.

Pure functions, no I/O, no network — unit-tested in test_bars.py. Inputs are
three parallel sequences rather than rows: the reader hands back an Arrow
table, and `column.to_pylist()` stays columnar where a list of per-row tuples
would materialise the whole window.

Bad market data never raises: a renderer that crashes on one odd bar tells the
user nothing, so `check()` returns findings and the caller decides. A bad CALL
does raise — an unparseable interval or ragged input is a programmer error, not
something a market produced.
"""

import math
import re

_UNIT_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}

# Reasons carried on a coverage finding. An unreadable partition and a quiet
# market are different facts and never merge into one run (output-format.md).
ABSENT = "partition absent"
NO_TRADES = "no trades"


# ── Intervals and buckets ──────────────────────────────────────────────────

def parse_interval(text: str) -> int:
    """`Nm`/`Nh`/`Nd` to milliseconds."""
    match = re.fullmatch(r"([1-9][0-9]*)([mhd])", str(text).strip().lower())
    if not match:
        raise ValueError("interval must be a positive Nm, Nh, or Nd value")
    return int(match.group(1)) * _UNIT_MS[match.group(2)]


def bucket_start(ts_ms: int, interval_ms: int) -> int:
    """Floor to the bucket boundary, anchored at the UTC epoch.

    Anchoring at the epoch is what makes `1h` bars land on the hour and `1d`
    bars on UTC midnight regardless of where the requested window starts. The
    cost is that a window beginning mid-period opens on a partial bar, which
    `coverage()` reports rather than hides.
    """
    if interval_ms <= 0:
        raise ValueError("interval_ms must be positive")
    return ts_ms - (ts_ms % interval_ms)


# ── Bars ───────────────────────────────────────────────────────────────────

def to_bars(times, prices, sizes, interval_ms: int) -> list[dict]:
    """Bucket trades into OHLCV bars, ascending by bucket.

    Open and close are the earliest and latest trade BY TIMESTAMP in the
    bucket, never by arrival order: the reader fetches objects concurrently and
    guarantees no ordering across them, so trusting scan order silently
    transposes the two on any window built from more than one partition.

    Timestamps tie often — venues stamp in milliseconds and prints batch — so
    the tie-break is part of the rule rather than left to arrival order, which
    would make the same window produce different bars on different runs. Both
    ends take the LOWEST price of their instant. Taking the lowest for the open
    and the highest for the close would also be deterministic, but it would
    render every tied bucket as an up candle and a single-instant bucket as a
    full-range green one — a direction invented by the tie-break rather than
    observed. Same side at both ends makes a tie show as `open == close`.

    A row whose size is null still sets the prices; its quantity is counted in
    `volume_unknown` rather than added as zero, because an unreported size is
    not a size of nothing.
    """
    if not (len(times) == len(prices) == len(sizes)):
        raise ValueError("times, prices and sizes must be the same length")

    opens: dict[int, tuple[int, float]] = {}
    closes: dict[int, tuple[int, float]] = {}
    highs: dict[int, float] = {}
    lows: dict[int, float] = {}
    volumes: dict[int, list[float]] = {}
    unknown: dict[int, int] = {}

    for index in range(len(times)):
        ts, price, size = times[index], prices[index], sizes[index]
        if ts is None or price is None:
            continue
        price = float(price)
        # NaN would poison min/max silently — every comparison against it is
        # False, so it wins nothing and corrupts nothing visibly.
        if not math.isfinite(price):
            continue
        bucket = bucket_start(int(ts), interval_ms)
        stamp = int(ts)
        first = opens.get(bucket)
        if first is None or (stamp, price) < first:
            opens[bucket] = (stamp, price)
        last = closes.get(bucket)
        # Latest timestamp, then the lowest price at it: negating the price
        # makes one max() pick both.
        if last is None or (stamp, -price) > last:
            closes[bucket] = (stamp, -price)
        highs[bucket] = price if bucket not in highs else max(highs[bucket], price)
        lows[bucket] = price if bucket not in lows else min(lows[bucket], price)
        volumes.setdefault(bucket, [])
        unknown.setdefault(bucket, 0)
        if size is None or not math.isfinite(float(size)):
            unknown[bucket] += 1
        else:
            volumes[bucket].append(float(size))

    return [{
        "time": bucket,
        "open": opens[bucket][1],
        "high": highs[bucket],
        "low": lows[bucket],
        "close": -closes[bucket][1],
        # fsum, not sum: a day of 1m buckets accumulates enough float error in
        # naive addition to move the last displayed digit.
        "volume": math.fsum(volumes[bucket]),
        "volume_unknown": unknown[bucket],
    } for bucket in sorted(opens)]


# ── Coverage ───────────────────────────────────────────────────────────────

def coverage(bars: list[dict], interval_ms: int, start_ms: int, end_ms: int,
             *, absent=(), watermark_ms: int | None = None) -> list[dict]:
    """Periods the bars do not account for, as ordered findings.

    `bars` is assumed ascending by `time`, as `to_bars` returns it.

    Each finding is `{kind, from, to, reason}`. Missing buckets merge into runs
    only while the reason holds: a run never spans both an unreadable partition
    and a quiet market, because presenting them as one fact claims knowledge of
    a market that was never read. A bucket that is both is reported absent —
    unreadable outranks unproven.
    """
    if start_ms >= end_ms:
        return []
    absent_set = {bucket_start(int(b), interval_ms) for b in absent}
    present = {bar["time"] for bar in bars}
    findings: list[dict] = []

    def clamp(value: int) -> int:
        """Report only time the caller asked about.

        The grid is bucket-aligned but the window need not be, so the first and
        last buckets can reach outside it.
        """
        return min(max(value, start_ms), end_ms)

    run_start: int | None = None
    run_reason: str | None = None
    bucket = bucket_start(start_ms, interval_ms)
    while bucket < end_ms:
        missing = bucket not in present
        reason = (ABSENT if bucket in absent_set else NO_TRADES) if missing else None
        if reason != run_reason and run_start is not None:
            findings.append({"kind": "gap", "from": clamp(run_start),
                             "to": clamp(bucket), "reason": run_reason})
            run_start = None
        if reason is not None and run_start is None:
            run_start = bucket
        run_reason = reason
        bucket += interval_ms

    # Flush unconditionally. The loop cannot do it: with a window whose end is
    # not bucket-aligned, the final step jumps past `end_ms` and an open run
    # would leave with it — reporting unread periods as nothing at all.
    if run_start is not None:
        findings.append({"kind": "gap", "from": clamp(run_start),
                         "to": clamp(bucket), "reason": run_reason})

    # Overlap, not containment: the bar covering the window's start has a
    # bucket time BEFORE start_ms whenever the window opens mid-period, which
    # is exactly the bar the partial check exists to find.
    inside = [bar for bar in bars
              if bar["time"] + interval_ms > start_ms and bar["time"] < end_ms]
    if inside:
        first, last = inside[0], inside[-1]
        if start_ms > first["time"]:
            findings.append({"kind": "partial", "from": first["time"],
                             "to": start_ms,
                             "reason": "window opens mid-period"})
        period_end = last["time"] + interval_ms
        if watermark_ms is not None and watermark_ms < period_end:
            findings.append({"kind": "partial", "from": last["time"],
                             "to": period_end,
                             "reason": "period incomplete — data reaches "
                                       f"{watermark_ms}"})
    return findings


# ── Validation and summary ─────────────────────────────────────────────────

def check(bars: list[dict]) -> list[str]:
    """Structural problems in a bar series. Returns findings, never raises."""
    findings: list[str] = []
    previous: dict | None = None
    for bar in bars:
        label = bar.get("time")
        if bar["high"] < max(bar["open"], bar["close"]):
            findings.append(f"bar {label}: high below open/close")
        if bar["low"] > min(bar["open"], bar["close"]):
            findings.append(f"bar {label}: low above open/close")
        if bar["high"] < bar["low"]:
            findings.append(f"bar {label}: high below low")
        if previous is not None:
            if bar["time"] == previous["time"]:
                findings.append(f"bar {label}: duplicate bucket")
            elif bar["time"] < previous["time"]:
                findings.append(f"bar {label}: out of order")
        previous = bar
    return findings


def summarize(bars: list[dict]) -> dict:
    """Summary-line values for the RENDERED bars, not the requested window.

    `change_pct` is None when it cannot be stated rather than when it is zero:
    a single bar has nothing to change against, and a non-positive first open
    has no meaningful denominator. The renderer omits the field in that case
    instead of printing a fabricated `+0.00%`.
    """
    if not bars:
        return {"count": 0, "first_open": None, "last_close": None,
                "change_pct": None, "high": None, "low": None, "volume": 0.0}
    first_open = bars[0]["open"]
    last_close = bars[-1]["close"]
    change = None
    if len(bars) > 1 and first_open > 0:
        change = (last_close - first_open) / first_open * 100.0
    return {
        "count": len(bars),
        "first_open": first_open,
        "last_close": last_close,
        "change_pct": change,
        "high": max(bar["high"] for bar in bars),
        "low": min(bar["low"] for bar in bars),
        "volume": math.fsum(bar["volume"] for bar in bars),
    }
