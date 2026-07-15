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

**Snapshot**

```yaml
Spot      $[X]        [up/down X%] (from $[Y], low $[Z])
DVOL      [X]v        [flat/rising/falling] ([open] -> [close])
RV 7d     [X]v        implied [CHEAP/RICH/IN LINE] vs realized
VRP       [±X]v       vol [underpriced/overpriced] vs delivered
Activity  [Nk]        trades — [Venue X% · Venue Y% · ...] (by trade count)
Volume    $[X]M       Deribit only (cross-venue $ pending)
P/C       [X.Xx]      [descriptor] (all venues, by trades)
```

**Biggest Print**

```yaml
[DDMMMYY] [structure]   [Nx]   $[X]M   [HH:MM] UTC   via [Venue] ([Buy/Sell, ][IV]v avg)
```

The side word appears only when the whole block is one-directional (Buy/Sell).
Mixed-direction structures (any spread) carry no side tag — never write
"two-way" here; that means "aggressor undisclosed", which this is not.

**Block Flow — $[X]M / [N] blocks / [M] structures[ (top 8 by notional)]**

```yaml
#  Structure            Notl     Blocks  Detail
-  -------------------  -------  ------  -----------------------------------
1  [structure]          $[X]M    [n]     bought [K1][C/P] / sold [K2][C/P] x[size] [IV|IV–IV]v
2  …
```

Two granularities, both always stated: tape **blocks** (block_trade_ids, the
industry term for the individual prints) and **structures** (clips of one
worked order — same legs, directions, and size ratio — grouped into one row).
Rows are structures and `#` numbers them; the Blocks column carries each
row's print count, so the Blocks column sums to the header `[N]` and the row
count equals `[M]`. When more than 8 structures qualify, the header gains the
`(top 8 by notional)` suffix — truncation is disclosed in the header and the
table body never changes shape.

Detail rules: per-leg `bought`/`sold` verbs appear only when the tape discloses
every leg's direction; otherwise legs render neutrally (`[K1]P vs [K2]P`)
tagged ` two-way`. Multi-block rows show the clip IV range (`36.5–37.0v`)
when clips printed at different vols, a single value otherwise.

**Vol Surface**
Skew: front 25Δ RR [±X]v → [puts bid / calls bid] · Term: [front]v → [back]v → [contango / flat / backwardation / humped — peak at [DDMMMYY] / dished — trough at [DDMMMYY]]

Term reads the whole listed curve, front to last expiry — monotonic (±0.2v
tolerance) with >1v span is contango/backwardation; non-monotonic curves are
humped/dished and name the interior peak/trough. `[back]` is the LAST listed
expiry's ATM, not the second.

```yaml
Expiry     ATM      ΔATM     25d RR    ΔRR      Fly     ΔFly
---------  ------   ------   --------  ------   -----   ------
[DDMMMYY]  [X.X]v   [±X.X]v  [±X.X]v   [±X.X]v  [X.X]v  [±X.X]v
…
```

Formatting rules: ATM/RR/Fly are current (close) values, `X.Xv` precision. The Δ
columns are the window-over-window change (current − window-open), signed `+X.Xv`;
`flat` when the change rounds to zero, `n/a` when no window-open surface was
available. Append `*` to any cell derived from extrapolated wings (e.g. `-4.0v*`).

---

## Thin Window

(< 2h, no blocks) — output all four sections; mark empty ones `No data`.
