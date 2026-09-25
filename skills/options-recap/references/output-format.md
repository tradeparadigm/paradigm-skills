# Output Format — FIXED

This is the existing rendering contract, now backed by non-hot inputs. The live
script renders it; consult it directly only for supplied/injected evidence.
Never fabricate a value to fill the template; state a specific field or section
as unavailable when the selected data cannot establish it.

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
⚠ [one line per gap, when there are any]
Coverage  [N]/[M] venues  [per-venue state, or "all venue feeds complete"]
Spot      $[X]        [up/down X%, or flat] (from $[Y], low $[Z])
DVOL      [X]v        [flat/rising/falling] ([open] -> [close])
RV 7d     [X]v        implied [CHEAP/RICH/IN LINE] vs realized
VRP       [±X]v       vol [underpriced/overpriced/roughly fair] vs delivered
Activity  [Nk]        trades — [Venue X% · Venue Y% · ...] (by trade count)
Volume    $[X]M       observed valued trades · USD premium
P/C       [X.Xx]      [descriptor] (observed trades · see ⚠ lines)
```

The `⚠` lines are the FIRST lines inside the fence, not above it: on
2026-09-08 a relaying model kept every figure in the fence and deleted all
three warning lines that sat outside it. `RV 7d` and `VRP` print
`unavailable` when the Deribit close history cannot be fetched, rather than
being dropped.

`Coverage` leads the figures because every one of them is a function of how
much of the window was read, and that cannot be inferred from the figures. `M`
counts venues by DISPLAY LABEL, the same folding the Activity line uses, so the
two lines always agree; `N` is those whose trade data was read and proven. The
detail names any venue that is not `complete`:

- `quiet hours` — the venue's continuous `option_summary` feed covered every
  hour, and some of those hours carried no prints. NOT "this venue never
  traded": it is a claim about hours, and a venue can be 40% of the tape and
  still have quiet hours.
- `feed gap` — hours missing from BOTH feeds, so trade data was genuinely lost.
- `quote gap` — hours missing only from `option_summary`. The trade tape covered
  them, so nothing below is understated; only the coverage proof is short.
- `READ FAILED` — the trade read itself failed.
- `unverified` — the companion listing could not be read or came back empty, so
  coverage could not be established either way.

Exactly three of those mean the venue's trades are missing or unproven: `feed
gap`, `READ FAILED` and `unverified`. `quiet hours` and `quote gap` do not —
both describe a venue whose trades were read in full.

A venue in one of those three is dropped from the Activity split and named after
it instead — `(by trade count; Deribit unread — shares are of what was read)`.
Its own share is unknowable, so it is not printed; and the remaining percentages
are computed over the READ venues only, so they sum to 100 and the sentence is
true. `N` in `Coverage N/M` counts by the same three, so the two lines always
agree about the same venue. An unrecognised state renders `state not recognised`,
counts as unread, and never raises.

`ATM`, `25d RR` and `Fly` carry a trailing `*` when the value was reached by
clamping to an endpoint of a thin chain rather than interpolated, and the Term
label carries one when any expiry's ATM was. `Fly` is `(c25 + p25)/2 - atm`, so
it takes the star if EITHER input was clamped.

Every `⚠` line names venues with the SAME words the Snapshot uses — `Bybit`,
`OKX`, `Deribit`, `Deribit USDC`, `Bullish` — never the raw partition id
(`okex-options`). The two Deribit ids stay apart in a `⚠` line, which describes
one venue, and fold together in Coverage and Activity, which describe shares.
Money in a `⚠` line is `$X.XXM`, the same unit as the Block Flow header.

Block Flow states every exclusion with its size, because the section's subject is
how much flow there was:

- blocks excluded for an unprovable Paradigm overlap, with their notional and
  coin — these are real prints withheld rather than absent;
- blocks below the $250k floor, with their combined notional;
- blocks dropped for want of event-time unit metadata, with how many of the
  venue's blocks remain;
- Bybit, which publishes a block flag with no group id and so can never appear.

`Spot` is normally Deribit's own index. When that feed is unreachable it falls
back to the venue tape's last trade-time index and says so in a `⚠` line; treat
`Spot` and any block priced without its own index as approximate on that run.

Volume is the valued subset, not a market total: trades whose USD premium
cannot be proven are counted in a gap line instead of being estimated into the
number. Never write `all venues` — a venue's trade source can be a gap — and
never combine `amount_native` across venues.

**Biggest Print**

```yaml
[DDMMMYY] [structure]   $[X]M   [HH:MM] UTC   via Paradigm/[Venue]   [legs]
```

The single largest **proven block** in the window, ranked by underlying USD
notional, as in Block Flow. Snapshot Volume is USD premium turnover: never
substitute one measure for the other. Group legs only on a real venue block/OTC id. The
`via …` tag names the source and venue. `[legs]` is the same leg list the
Block Flow Detail column shows. A raw venue block without provable leg
geometry renders as
`[Venue] Block   $[X]M   ~[HH:MM] UTC   via venue tape   x[coin] [IV]v — [n] legs`
(`~` = 5-min bucket resolution; `x[coin]` is its total coin size).

Legs are listed as traded, one per instrument: `[±size] [expiry] [K][C/P]`,
e.g. `-500 25SEP26 80KC / +1000 30OCT26 90KC`. The sign is the taker's side
(`+` bought, `-` sold) and the size is that instrument's net quantity in the
block, so a ratio is visible in the sizes. A leg whose side the tape does not
carry prints its size unsigned. The expiry prefix appears only on multi-expiry
structures. Never write "two-way": the side is disclosed per leg. A block the
tape describes only as a named package, without per-leg sizes, keeps the older
`[K1][C/P] / [K2][C/P] x[unit] ([Buy/Sell])` form.

Two-leg spreads, calendars and diagonals with unequal leg sizes are ratios and
are named so: `Call Ratio Spread`, `Put Ratio Diagonal`, `Call Ratio Calendar`.

Strike labels abbreviate at 10K and above (`68K`, `62.5K`); below 10K they
stay raw (`1875`, `2000` — never `2K`), so one table never mixes conventions.
Multi-expiry structure labels are chronological: `near/far` when those two
ARE the complete expiry set (calendar, diagonal), `near→far` when interior
tenors are elided (3+ expiries) — each leg's own expiry always appears in
the Detail column.

**Block Flow — $[X]M / [N] blocks / [M] structures[ (top 8 by notional)]**

```yaml
#  Structure                  Notl     Blocks  Detail (+ taker bought, - taker sold)
-  -------------------------  -------  ------  -----------------------------------
1  [structure]                $[X]M    [n]     [±size] [K1][C/P] / [±size] [K2][C/P] [IV]v
2  OKX Block                  $[X]M    1       x[size] [IV]v — [n] legs (venue tape)
…
```

Raw venue blocks rank in the same pool and count toward the header totals. When
their rows do not prove leg geometry, use `[Venue] Block`, carry a
`(venue tape)` note, and count the real venue block id once.

The Structure column has a 27-char floor but stretches to the longest label in
the window (a typed cross-expiry label like `24JUL26/31JUL26 Call Diagonal`
runs past 27), so the header and rows stay aligned to whatever width the widest
structure needs. There is no per-row venue column — the Biggest Print line's
`via Paradigm/<venue>` tag is where the venue shows, and a venue-tape row
carries its venue in the structure label (`OKX Block`).

Two granularities, both always stated: tape **blocks** (`BLOCK_TRADE_ID`s, the
industry term for the individual prints) and **structures** (clips of one worked
order — the blocks sharing an `RFQ_ID` — grouped into one row). Rows are
structures and `#` numbers them; the Blocks column carries each row's block
count, so it sums to the header `[N]` and the row count equals `[M]`. When more
than 8 structures qualify, the header gains the `(top 8 by notional)` suffix.

Detail: the legs as traded (see Biggest Print), then the average `[IV]v`
(Deribit blocks only). The column header reads
`Detail (+ taker bought, - taker sold)`.

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

`*` marks a figure reached by extrapolating past the listed chain rather than
interpolating within it — on the ATM column as well as the wings, since a thin
chain clamps ATM to an endpoint and that value also drives the term-structure
label.

Formatting rules: ATM/RR/Fly are current (close) values, `X.Xv` precision. The Δ
columns are the window-over-window change (current − window-open), signed `+X.Xv`;
`flat` when the change rounds to zero, `n/a` when no window-open surface was
available (window-start outside the `v_vol_surface` history — deeper than the
cold backfill, or in a partition gap). Append `*` to any cell derived from
extrapolated wings (e.g. `-4.0v*`).

---

## Thin Window

(< 2h, no blocks) — output all four sections. An empty one states a specific
source and reason — `Unavailable — no block cleared the $250k floor in this
window` — never a bare `No data`, which reads as a quiet market when it may be a
missing feed.
