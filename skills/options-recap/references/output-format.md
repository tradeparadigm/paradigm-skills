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
Volume    $[X]M       [primary venue] (incl. Paradigm)
P/C       [X.Xx]      [calls/puts] dominant
```

**Biggest Print**

```yaml
[DDMMMYY] [structure]   [Nx]   $[X]M   [HH:MM] UTC   via [Venue] ([side], [IV]v avg)
```

**Block Flow — $[X]M / [N] blocks**

```yaml
#  Structure            Notl     Detail
-  -------------------  -------  ------------------------------------------
1  [structure]          $[X]M    [strikes] x[size] - [side] [IV]v [two-way/one-sided]
2  …
```

**Vol Surface**
Skew: front 25Δ RR [±X]v → [puts bid / calls bid] · Term: [front]v → [back]v → [contango / flat / backwardation]

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
