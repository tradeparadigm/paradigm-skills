---
name: paradigm-block-analyst
description: >
  Analyze a Paradigm RFQ or pasted block trade using raw exchange venue files,
  source tapes, and live venue data. Use for /analyze RFQ_ID, /analyst RFQ_ID, fill-vs-market
  benchmarking, net greeks, prior block flow, and vol-surface impact. The model
  uses the established script to calculate and render without Dime hot files.
metadata:
  author: tradeparadigm
  version: "2.0"
---

# Paradigm Block Trade Analyst

## Trigger

Use for `/analyze <rfq_id> <reference description>`, a pasted block-trade
object, or a request to benchmark or explain a specific Paradigm execution.
`/analyst` is also accepted. For a bare `/analyze`, `/analyst` or
`/paradigm_block_analyst` without supplied trade context, ask only:
"Which RFQ ID or block trade would you like me to analyze?" Do not search
memory, other sessions, or recent trades to choose an input for the user.

## Live execution

Run `bash scripts/analyze.sh <RFQ_ID>` from this skill's directory and relay
stdout verbatim as the entire answer. The script resolves partitioned legs,
uses the existing live market reads and calculations, and renders the analysis.
Do not recompute or reformat the result or launch optional follow-up scans.
Report a command error and stop; a stale or unreadable execution source must
not be bypassed by reading its objects directly. The remaining rules document
the calculation contract and apply to supplied/injected trades as well.

## Hard rules

1. **Do not use `s3://dt-exchange-venue-data/hot/` or any `hot__*` object.**
2. For an id lookup, run `bash scripts/analyze.sh <RFQ_ID>` once and return its
   finished analysis, not the intermediate collector evidence.
3. For an id lookup, read
   [references/rfq-lookup.md](references/rfq-lookup.md) and the
   [raw exchange catalog](../data-discovery/references/exchange-raw.md), then
   consult them when explaining sources or handling injected evidence; the live
   script already performs the lookup.
4. The RFQ id is authoritative. Text after it is a user label, not permission
   to invent the asset, structure, fill, or instrument.
5. Never default to BTC, invent a block grouping, pair legs without a real
   block id, or present a candidate raw trade as a proven RFQ fill.
6. Fail visibly when identity or a required field cannot be established.

## Resolve the trade

The collector performs the first authoritative lookup and distinguishes a
resolved venue execution, a historical execution, a request with unresolved
execution, and a missing request. Treat its rows, provenance, confidence, and
gaps as evidence for the established calculations. Available evidence includes:

- injected cleared-trade context;
- the current Paradigm RFQ tape for request metadata;
- the daily partitioned Paradigm execution tape for all id-linked legs in the
  trailing 30 days (`execution_candidates.paradigm_tape`); see the linked
  execution contract for publication lag, native units and deployment gates;
- raw venue `option_trade`/perp rows and their real block/RFQ/OTC ids;
- the frozen non-hot executed Paradigm tape for trades at or before
  2026-08-10 only;
- venue public trade APIs for corroboration.

Prefer a direct id match. When only time/product/structure/size evidence is
available, require a unique match and label residual uncertainty. If the RFQ
request is found but its execution is not, emit only:

```text
RFQ <id> found, but its executed fill could not be resolved from current raw exchange data.
```

If the RFQ request itself is not found, emit only:

```text
RFQ <id> not resolved — no authoritative asset, structure, or fill available.
```

## Choose market evidence

Answer the requested question first: fill-versus-mark needs the proven legs
and their execution-time benchmark, not an automatic 7/30-day history or live
surface scan. Fetch history, greeks or current marks only when requested or
necessary to support the answer; optional unavailable fields must not stall it.

After the trade resolves, use any bounded combination the model judges useful:

- latest raw `option_summary` rows for each leg's mark, bid/ask, IV, greeks,
  OI, and underlying price;
- raw `option_trade` rows for 24h/7d/30d prior prints, real block clusters,
  traded IV, and flow impact;
- raw `perp_summary` or spot/perp trades for spot, hedge legs, and funding;
- event-time-applicable instrument metadata for contract, IV, OI, and premium
  units; follow the catalog's 30-day metadata-history limit and report gaps;
- direct Deribit/OKX/Bybit/Bullish APIs for fresher or missing current marks.

Read only the relevant venues, symbols, and event-time partitions. Query legs
in parallel when practical, but choose correctness and a complete answer over
a fixed fetch sequence. See [references/venues.md](references/venues.md) for
instrument naming and venue-specific limitations.

## Analysis constraints

- Derive asset and venue from resolved `PRODUCT` or proven raw instrument data,
  never from the inline description alone.
- Resolve taker direction from authoritative leg sides when available. For a
  combined strategy row, use its side plus the structure convention in
  [references/strategy-codes.md](references/strategy-codes.md), and sanity-check
  against debit/credit.
- A prior print means the whole structure matched: same real block group and
  full leg set/ratios. Loose matching legs are context, not recurrence.
- One RFQ id can resolve to several blocks with opposite taker sides: a GRFQ is
  a broadcast, and whoever crosses is the taker on that block. The script groups
  blocks by signed structure and prints one analysis per direction. Relay every
  analysis it prints; never net them into one package, one size, or one edge.
- Convert raw native units with instrument metadata before comparing venues.
- Net greeks over proven legs and ratios, scaled to the full position:

```text
net_greek = sum(position_sign * leg_ratio * instrument_greek) * quantity
```

- Delta is in underlying coin; vega is USD per vol point; theta is USD per
  day; gamma is coin per USD move. Deribit does not publish vanna; omit it.
- For multi-leg option packages, calculate fill and mark on the same signed,
  ratio-weighted legs. Exclude perp hedge legs from option-premium offset.
- When resolved tape evidence supplies one row per leg, use the smallest
  absolute option-leg `QTY` as the package count and weight every option row by
  `abs(QTY) / package_count`. Per-unit cash flow times package count is the
  full-size cash flow; they are not identical unless the count is one.
  `BUY` is premium paid and `SELL` is premium
  received; authoritative row sides override shorthand signs in DESCRIPTION.
- Net the package before reporting it: `signed_price = sum(BUY price * ratio) -
  sum(SELL price * ratio)`, and apply the identical weights/sides to
  `REF_PRICE`. Positive is `Paid`; negative is `Recd`. Report the absolute
  package value. Fill-versus-mark in bps is
  `(abs(signed_fill) - abs(signed_ref)) * 10,000`; never reuse a per-leg
  `OFFSET_BPS` as the package offset.
- Keep the resolved tape `REF_PRICE` as the execution benchmark. A newer live
  mark belongs in `[Live]`; it must not replace or contradict the tape-based
  fill-versus-mark calculation.
- Before rendering, compute the signed fill, signed reference and offset in
  code from the actual leg rows; check the header against those results.
  Row `SIDE` controls cash flow even when DESCRIPTION has opposite signs.
  Never reverse authoritative BUY/SELL sides to fit a strategy label.
  Multiplying a BTC premium difference by 10,000 expresses it in units of
  0.0001 BTC, not a relative percentage of the reference premium.
- Attribute a vol-surface move only when raw trade IV and later summary IV,
  timing, and size support it.

## Output

For a focused natural-language question, answer it directly with units and
material coverage gaps. For the full `/analyze` command, the successful
response is two plain-text lines followed by one YAML
code block. Keep it terse and omit unestablished fields rather than padding:

```text
<ASSET> <structure> | <Buyer|Seller> | <size> | <Paid|Recd> <price> | <fill-vs-mark>

Spot <price> · <moneyness/exposure> · <key risk level> · <rfq type/venue>
```

```yaml
[Greeks]  Δ <coin> · Vega <$/v> · Γ <value/direction> · Θ <$/d>
[Fair]    <fill-vs-mark> · <per-leg IV> · <spread/edge>
[History] <24h/7d/30d structure recurrence and flow verdict>
[Live]    <per-leg bid/ask> · <screen level> · <fill vs screen>
```

Use only numbers the script or a tool call in this turn actually produced. A
field with no such source is written `unavailable` — never an estimate, a
range, an "est." or a `~` value; supplied tape rows carry fill, mark, size and
side, not greeks, IV, bid/ask or history. Drop a bracket row only when all of
its data is unavailable. Do not show working, tool narration, a data trace, or
follow-up commentary.
