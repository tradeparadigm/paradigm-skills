"""
render_ohlcv.py — the fixed /ohlcv rendering.

Turns the collector's evidence into the contract in references/output-format.md
and nothing else. No value is computed here that bars.py already computed, and
no value is invented to fill the template: a field the evidence cannot
establish becomes a coverage line, not a plausible number.

Pure functions, no I/O, no network — unit-tested in test_render.py.
"""

import datetime as dt
import json

import bars as bar_math

# No instrument metadata is read, so tick_size never resolves and the contract's
# documented fallback applies on every run.
FALLBACK_DECIMALS = 1

COLUMNS = ("Time", "Open", "High", "Low", "Close", "Volume")

# Past this many missing periods the list becomes a wall of text; name the span.
MAX_LABELS = 8
MAX_VOLUME_DECIMALS = 6


def volume_decimals(values: list[float]) -> int:
    """Enough precision that no traded size renders as zero.

    A bar with 0.04 of volume printed at one decimal place is `0.0` — a
    zero-volume bar, which is the one thing the contract forbids inventing.
    """
    traded = [abs(v) for v in values if v]
    if not traded:
        return 1
    decimals = 1
    while decimals < MAX_VOLUME_DECIMALS and round(min(traded), decimals) == 0:
        decimals += 1
    return decimals


def _utc(ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)


def _clock(ms: int, with_date: bool) -> str:
    """Time-column format: `10:00`, or `16Sep 10:00` across a date boundary."""
    moment = _utc(ms)
    return (f"{moment:%d%b %H:%M}" if with_date else f"{moment:%H:%M}")


def _header_clock(ms: int, with_date: bool) -> str:
    """Header format, which the contract writes differently: `Sep 16 10:00`."""
    moment = _utc(ms)
    return (f"{moment:%b %-d %H:%M}" if with_date else f"{moment:%H:%M}")


def _price(value: float | None, decimals: int) -> str:
    return "" if value is None else f"{value:.{decimals}f}"


def _header(evidence: dict) -> str:
    """`**SYMBOL · INTERVAL · VENUE · start–end UTC**`.

    Dates appear when the REQUESTED window is 24h or longer: any multiple of
    24h has identical start and end clock times, so a bare HH:MM–HH:MM would
    read as a zero-length window. This is a different test from the Time
    column's, which follows the bars actually rendered, and the two can
    legitimately disagree.
    """
    span = evidence["end_ms"] - evidence["start_ms"]
    dated = span >= 86_400_000
    start = _header_clock(evidence["start_ms"], dated)
    end = _header_clock(evidence["end_ms"], dated)
    symbol = f"{evidence['asset']}-PERP"
    return (f"**{symbol} · {evidence['interval']} · {evidence['venue']} · "
            f"{start}–{end} UTC**")


def _period_labels(finding: dict, interval_ms: int, dated: bool) -> tuple[int, str]:
    """A merged run back into the periods it covers.

    coverage() merges consecutive missing buckets into one finding, but the
    contract reports periods: `2 periods unavailable (13:00, 14:00)`. Printing
    the run's raw bounds instead would be accurate and unreadable.

    The label names the PERIOD, which is why a run clamped to a mid-period
    window start still shows that period's own boundary.
    """
    labels = []
    bucket = bar_math.bucket_start(finding["from"], interval_ms)
    while bucket < finding["to"]:
        labels.append(_clock(bucket, dated))
        bucket += interval_ms
    if not labels:
        labels.append(_clock(finding["from"], dated))
    count = len(labels)
    # A seven-day run at 1m is 10,080 periods; listing them is a wall of text,
    # not a coverage line. Past a handful, name the span instead.
    if count > MAX_LABELS:
        return count, f"{labels[0]}–{labels[-1]}"
    return count, ", ".join(labels)


def _partial_line(finding: dict, dated: bool) -> str:
    """Partials carry instants as fields, so they format as clock times."""
    if "reaches" in finding:
        return (f"⚠ last bar is partial — period ends "
                f"{_clock(finding['period_end'], dated)}, data reaches "
                f"{_clock(finding['reaches'], dated)}")
    if "window_edge" in finding:
        return (f"⚠ first bar is partial — period opens "
                f"{_clock(finding['period_start'], dated)}, window starts "
                f"{_clock(finding['window_edge'], dated)}")
    return f"⚠ {finding['reason']}"


def _coverage_lines(evidence: dict, interval_ms: int, dated: bool,
                    *, rendered: bool) -> list[str]:
    lines: list[str] = []
    for finding in evidence.get("coverage", []):
        if finding.get("kind") == "gap":
            count, labels = _period_labels(finding, interval_ms, dated)
            reason = finding["reason"]
            if reason == bar_math.ABSENT:
                reason = "partitions absent" if count > 1 else "partition absent"
            lines.append(f"⚠ {count} period{'s' if count > 1 else ''} "
                         f"unavailable ({labels}) — {reason}")
        else:
            lines.append(_partial_line(finding, dated))

    unknown = evidence.get("volume_unknown") or 0
    if unknown:
        lines.append(f"⚠ {unknown} trade{'s' if unknown != 1 else ''} with "
                     f"unreported size — volume is the proven subset")

    # Neither of these can be proved without an instrument metadata read, which
    # this skill does not do. Both are stated rather than silently assumed —
    # but only when there is a table for them to describe.
    if rendered:
        lines.append(f"⚠ price shown to {FALLBACK_DECIMALS} decimal place — "
                     f"no instrument metadata read, so tick size is unknown")
        lines.append(f"⚠ volume in the venue's native units "
                     f"({evidence['venue']}) — no metadata to prove a "
                     f"conversion")

    for finding in evidence.get("findings", []):
        lines.append(f"⚠ {finding}")
    return lines


def _summary_line(summary: dict, decimals: int, vol_dp: int) -> str:
    count = summary["count"]
    parts = [f"{count} bar{'s' if count != 1 else ''}"]
    change = summary.get("change_pct")
    if change is not None:
        parts.append(f"{change:+.2f}%")
    parts.append(f"high {_price(summary['high'], decimals)}")
    parts.append(f"low {_price(summary['low'], decimals)}")
    parts.append(f"vol {summary['volume']:.{vol_dp}f}")
    return " · ".join(parts)


def _table(bars: list[dict], decimals: int, dated: bool,
           vol_dp: int) -> list[str]:
    rows = [[_clock(bar["time"], dated),
             _price(bar["open"], decimals), _price(bar["high"], decimals),
             _price(bar["low"], decimals), _price(bar["close"], decimals),
             f"{bar['volume']:.{vol_dp}f}"] for bar in bars]
    headers = list(COLUMNS)
    # The unit is unproven, so the header says so rather than the column
    # implying a coin amount the metadata never established.
    headers[-1] = "Volume (native)"
    widths = [max(len(headers[i]), *(len(row[i]) for row in rows))
              for i in range(len(headers))]
    lines = ["  ".join(
        headers[i].ljust(widths[i]) if i == 0 else headers[i].rjust(widths[i])
        for i in range(len(headers)))]
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        lines.append("  ".join(
            row[i].ljust(widths[i]) if i == 0 else row[i].rjust(widths[i])
            for i in range(len(row))))
    return lines


def render(evidence: dict) -> str:
    interval_ms = bar_math.parse_interval(evidence["interval"])
    bars = evidence.get("bars") or []
    dates = {_utc(bar["time"]).date() for bar in bars}
    dated_rows = len(dates) > 1
    # Coverage spans the whole window, not just the periods that produced
    # bars, so its labels follow the window's dates. A two-day window whose
    # bars all land on one date would otherwise print both days' missing
    # periods as the same clock times.
    dated_gaps = (_utc(evidence["start_ms"]).date()
                  != _utc(evidence["end_ms"] - 1).date())
    decimals = FALLBACK_DECIMALS

    lines = [_header(evidence), ""]

    if not bars:
        # The two cases are different facts: nothing was readable, versus
        # everything was readable and the market was quiet.
        if evidence.get("status") == "unavailable":
            absent = len(evidence.get("path_plan", {}).get("missing_days", []))
            noun = "partition" if absent == 1 else "partitions"
            lines.append(f"No bars — all {absent} {noun} absent")
        else:
            lines.append("No bars — no trades in the window")
        lines.append("")
        lines.extend(_coverage_lines(evidence, interval_ms, dated_gaps,
                                     rendered=False))
        return "\n".join(lines).rstrip() + "\n"

    vol_dp = volume_decimals([bar["volume"] for bar in bars])
    lines.extend(_table(bars, decimals, dated_rows, vol_dp))
    lines.append("")
    lines.append(_summary_line(evidence["summary"], decimals, vol_dp))
    coverage = _coverage_lines(evidence, interval_ms, dated_gaps, rendered=True)
    if coverage:
        lines.append("")
        lines.extend(coverage)
    return "\n".join(lines).rstrip() + "\n"


def spec(evidence: dict, component: str) -> dict:
    """The catalog spec for a client that advertised a chart component.

    Reached via the collector's `--component <id>`, where the id is one a
    client advertised — never assumed here. `bars` is required to be non-empty
    by the component's schema, so an empty result has no spec and falls back
    to the table, which can say why it is empty.
    """
    bars = evidence.get("bars") or []
    if not bars:
        raise ValueError("a spec needs at least one bar; render the table")
    return {
        "layout": "stack",
        "children": [{
            "component": component,
            "props": {
                # volume_unknown is bookkeeping for the coverage line, not a
                # field the component knows.
                "bars": [{k: bar[k] for k in
                          ("time", "open", "high", "low", "close", "volume")}
                         for bar in bars],
                "symbol": f"{evidence['asset']}-PERP",
                "interval": evidence["interval"],
                "venue": evidence["venue"],
                "priceDecimals": FALLBACK_DECIMALS,
                # Only real gaps: a partial period is a coverage line, not a
                # band across the chart.
                "gaps": [{"from": f["from"], "to": f["to"],
                          "reason": f["reason"]}
                         for f in evidence.get("coverage", [])
                         if f.get("kind") == "gap"],
            },
        }],
    }


def spec_json(evidence: dict, component: str) -> str:
    return json.dumps(spec(evidence, component), indent=2)
