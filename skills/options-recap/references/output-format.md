# Output Format — FIXED

This is the exact rendering contract for the recap. **In the live path you do
not need this file** — `run_recap.sh` already emits this shape and you relay its
stdout verbatim. Read this only in the **injected-data** and **simulate** modes,
where you render the four sections yourself.

Four sections, this exact order, every recap. Never reorder, add, or drop
sections. **Do not emit Themes, Dealer positioning, or a Bottom Line.** Work
silently — no narration.

---

**[ASSET] Options · [WINDOW] Recap · [HH:MM]–[HH:MM] UTC**

Windows of 24h or more stamp a date on each time (`Jul 14 05:22–Jul 15 05:22
UTC`) — any multiple of 24h has identical start/end clock times, so bare HH:MM
would read as a zero-length window. Intraday windows stay HH:MM-only.

**Snapshot**

```yaml
Spot      $[X]        [up/down X%] (from $[Y], low $[Z])
DVOL      [X]v        [flat/rising/falling] ([open] -> [close])
RV 7d     [X]v        implied [CHEAP/RICH/IN LINE] vs realized
VRP       [±X]v       vol [underpriced/overpriced/roughly fair] vs delivered
Activity  [Nk]        trades — [Venue X% · Venue Y% · ...] (by trade count)
Volume    $[X]M       Deribit only (cross-venue $ pending)
P/C       [X.Xx]      [descriptor] (all venues, by trades)
```

**Biggest Print — Paradigm block flow**

```yaml
[DDMMMYY] [structure]   [Nx]   $[X]M   [HH:MM] UTC   via Paradigm/[Venue] ([Buy/Sell, ][IV]v avg)
```

The single largest **block** (one `BLOCK_TRADE_ID`) in the window, by summed
per-leg USD notional — from the Paradigm block tape (**Paradigm RFQ/DRFQ flow
only**, across every venue Paradigm brokers), so `[Venue]` is the venue that
executed it (Deribit/Paradex/Bullish/…). This is NOT the whole market's biggest
print — only the biggest Paradigm-brokered one; the `— Paradigm block flow`
title and the `via Paradigm/…` tag both make that explicit. The side word appears only when
the whole block is one-directional (Buy/Sell); mixed-direction structures (any
spread) carry no side tag — never write "two-way" here. The `[IV]v avg` appears
only for Deribit blocks (IV is looked up from the vol surface, which is
Deribit-scoped); other venues carry no IV tag.

`[Nx]` is the structure UNIT size — the base (ratio-1) leg count of the
package, e.g. a 4×63-lot iron fly is `63x`, a 600-per-leg calendar is `600x`.
Never the leg-sum, which overstates a 4-leg package 4×. The same convention
applies to the `x[size]` in Block Flow details (there it is the unit size
summed across the row's clips).

Strike labels abbreviate at 10K and above (`68K`, `62.5K`); below 10K they
stay raw (`1875`, `2000` — never `2K`), so one table never mixes conventions.
Multi-expiry structure labels are chronological: `near/far` when those two
ARE the complete expiry set (calendar, diagonal), `near→far` when interior
tenors are elided (3+ expiries) — each leg's own expiry always appears in
the Detail column.

**Block Flow (Paradigm RFQ) — $[X]M / [N] blocks / [M] structures[ (top 8 by notional)][ · tape through [HH:MM] UTC[ ([n]h behind)]]**

```yaml
#  Structure                  Venue    Notl     Blocks  Detail
-  -------------------------  -------  -------  ------  -------------------------------
1  [structure]                [Venue]  $[X]M    [n]     [K1][C/P] / [K2][C/P] x[size] [IV]v ([Side])
2  …
```

The Structure column has a 27-char floor but stretches to the longest label in
the window (a typed cross-expiry label like `24JUL26/31JUL26 Call Diagonal`
runs past 27); the Venue column likewise stretches — the header and rows stay
aligned to whatever width the widest values need.

Two granularities, both always stated: tape **blocks** (`BLOCK_TRADE_ID`s, the
industry term for the individual prints) and **structures** (clips of one worked
order — the blocks sharing an `RFQ_ID` — grouped into one row). Rows are
structures and `#` numbers them; the Blocks column carries each row's block
count, so it sums to the header `[N]` and the row count equals `[M]`. When more
than 8 structures qualify, the header gains the `(top 8 by notional)` suffix.

The tape is S3-sourced (near-real-time, not live-to-the-second), so the header
carries a `tape through [HH:MM] UTC` freshness stamp, plus `([n]h behind)` when
the newest block is 90+ min behind the window end.

Detail: strike+type legs (`[K1]C / [K2]P`), the structure unit `x[size]`, the
average `[IV]v` (Deribit blocks only), and a `([Side])` tag when the block is
one-directional (Buy/Sell) — omitted for mixed-direction structures. Multi-expiry
structures prefix each leg with its own expiry.

**Vol Surface**
Skew: front 25Δ RR [±X]v → [puts bid / calls bid / flat] · Term: [front]v → [back]v → [contango / flat / backwardation / humped — peak at [DDMMMYY] / dished — trough at [DDMMMYY] / mixed]

Term reads the whole listed curve, front to last expiry — monotonic (±0.2v
tolerance) with >1v span is contango/backwardation; non-monotonic curves are
humped/dished and name the interior peak/trough, or `mixed` when the shape is
neither cleanly humped nor dished. `[back]` is the LAST listed
expiry's ATM, not the second. The skew side word is the RR's sign (negative →
puts bid, positive → calls bid, zero → flat); extrapolated wings put a `*` on
the RR figure (`+1.3v*`), never prose. These slots take exactly these tokens —
no suffixes like "downside skew", "(35.2v)", or "non-monotonic".

```yaml
Expiry     ATM      ΔATM     25d RR    ΔRR      Fly     ΔFly
---------  ------   ------   --------  ------   -----   ------
[DDMMMYY]  [X.X]v   [±X.X]v  [±X.X]v   [±X.X]v  [X.X]v  [±X.X]v
…
```

Formatting rules: ATM/RR/Fly are current (close) values, `X.Xv` precision. The Δ
columns are the window-over-window change (current − window-open), signed `+X.Xv`;
`flat` when the change rounds to zero, `n/a` when no window-open surface was
available (window-start outside the `v_vol_surface` history — deeper than the
cold backfill, or in a partition gap). Append `*` to any cell derived from
extrapolated wings (e.g. `-4.0v*`).

---

## Thin Window

(< 2h, no blocks) — output all four sections; mark empty ones `No data`.
