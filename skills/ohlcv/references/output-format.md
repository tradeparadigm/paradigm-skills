# Output Format — FIXED

One header line, one table, one summary line, then any coverage lines. Never
reorder them, never drop the summary, never omit a gap that was found.

Never invent a bar to fill the template. A period the read could not establish
is a gap line, not a row.

---

**[SYMBOL] · [INTERVAL] · [VENUE] · [start]–[end] UTC**

Windows of 24h or more stamp a date on each side (`Sep 16 10:00–Sep 17 10:00
UTC`), because any multiple of 24h has identical start and end clock times and
a bare `HH:MM–HH:MM` would read as a zero-length window. Intraday windows stay
`HH:MM` only.

```text
Time      Open      High      Low       Close     Volume
--------  --------  --------  --------  --------  --------
10:00     63120.5   63400.0   63050.0   63380.5     142.7
11:00     63380.5   63512.0   63201.4   63244.9      98.3
…
```

Columns are left-aligned for `Time` and right-aligned for the five numeric
columns.

Price precision is taken from `tick_size` in the instrument metadata record
described in the raw exchange catalog: the number of decimal places in the tick
(`0.5` → 1, `0.01` → 2). When no applicable record resolves, use one decimal
place and say so in a coverage line. Every row in one table uses the same
precision.

The `Time` column is the bar's OPEN time in UTC. It is `HH:MM` when every
rendered bar falls on one UTC date, and `DDMMM HH:MM` (`16Sep 10:00`) when they
do not — decided by the bars actually rendered, not by the requested window, so
a window that straddles midnight but returned bars on one date stays `HH:MM`.
This is a separate test from the header's date rule above, which is about the
requested window; the two can legitimately disagree.

Volume is the venue's base-asset volume for the period. When the unit cannot be
proved from instrument metadata, append the native unit to the column header
rather than converting.

**Summary line**

```text
[N] bars · [+/-X.XX%] · high [X] · low [X] · vol [X]
```

The percentage is last close against first open across the rendered bars — not
against a window boundary that produced no bar. When only one bar rendered,
write `1 bar` and omit the percentage rather than printing `+0.00%`.

**Coverage lines**

Each begins with `⚠` and names the specific fact:

```text
⚠ 2 periods unavailable (13:00, 14:00) — partitions absent
⚠ last bar is partial — period ends 11:00, data reaches 10:47
⚠ 3 trades with unreported size — volume is the proven subset
⚠ price shown to 1 decimal place — no instrument metadata read, so tick size is unknown
⚠ volume in the venue's native units (deribit) — no metadata to prove a conversion
```

The last two are unconditional whenever a table is rendered, and come last in
that order. Nothing reads `meta/instruments/`, so neither the tick size nor the
volume unit is ever established, and both say so rather than letting the
column imply a precision or a unit that was never proved. They are omitted when
there is no table for them to describe.

Missing periods carry a date (`16Sep 13:00`) when the requested WINDOW spans
more than one UTC date — not when the rendered bars do. A two-day window whose
bars all landed on one date would otherwise print two different days' missing
periods as the same clock times. Past eight periods a run names its span
(`00:00–23:00`) instead of listing them, and always states the count.

The last of those is required whenever any trade in the window carried no size.
A trade with an unknown size still sets the bar's prices, but its quantity is
not in the volume figure and is never counted as zero — so the volume column is
a lower bound for that bar, and the line says so. Report the count of such
trades across the rendered window.

A period with no trades and a period whose source could not be read are
separate lines with separate reasons. Never merge them, and never render either
as a zero-volume bar.

## Empty result

When the window resolved to no bars at all, print the header line, then a
single line stating the reason — `No bars — all 24 partitions absent` or
`No bars — no trades in the window` — and the coverage lines. Do not print an
empty table.

## Chart spec

When the client advertised a chart component, the script prints the component
spec instead of the table. It carries the same bars, the same summary values
and the same gaps, and the component id is the one the client advertised —
never hardcoded. Absent that advertisement, the table is the output.
