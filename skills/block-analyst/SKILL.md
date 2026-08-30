---
name: paradigm-block-analyst
description: >
  Analyze a Paradigm RFQ or pasted block trade using raw exchange venue files,
  source tapes, and live venue data. Use for /analyze RFQ_ID, fill-vs-market
  benchmarking, net greeks, prior block flow, and vol-surface impact. The model
  selects the raw evidence and never uses Dime hot files.
metadata:
  author: tradeparadigm
  version: "2.0"
---

# Paradigm Block Trade Analyst

## Trigger

Use for `/analyze <rfq_id> <reference description>`, a pasted block-trade
object, or a request to benchmark or explain a specific Paradigm execution.

## Hard rules

1. **Do not use `s3://dt-exchange-venue-data/hot/` or any `hot__*` object.**
2. For an id lookup, run `bash scripts/analyze.sh <RFQ_ID>` once. It returns a
   `dime.analysis.evidence.v1` JSON document from direct source tapes and any
   bounded raw venue lookup the resolved request permits; it does not render
   the analysis.
3. For an id lookup, read
   [references/rfq-lookup.md](references/rfq-lookup.md) and the
   [raw exchange catalog](../data-discovery/references/exchange-raw.md), then
   choose the evidence needed for this RFQ.
4. The RFQ id is authoritative. Text after it is a user label, not permission
   to invent the asset, structure, fill, or instrument.
5. Never default to BTC, invent a block grouping, pair legs without a real
   block id, or present a candidate raw trade as a proven RFQ fill.
6. Fail visibly when identity or a required field cannot be established.

## Resolve the trade

The collector performs the first authoritative lookup and distinguishes a
resolved venue execution, a historical execution, a request with unresolved
execution, and a missing request. Treat its rows, provenance, confidence, and
gaps as evidence rather than a fixed output schema. The model owns any
follow-up lookup plan. Available evidence includes:

- injected cleared-trade context;
- the current Paradigm RFQ tape for request metadata;
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

After the trade resolves, use any bounded combination the model judges useful:

- latest raw `option_summary` rows for each leg's mark, bid/ask, IV, greeks,
  OI, and underlying price;
- raw `option_trade` rows for 24h/7d/30d prior prints, real block clusters,
  traded IV, and flow impact;
- raw `perp_summary` or spot/perp trades for spot, hedge legs, and funding;
- newest instrument metadata for contract, IV, OI, and premium units;
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
- Convert raw native units with instrument metadata before comparing venues.
- Net greeks over proven legs and ratios, scaled to the full position:

```text
net_greek = sum(position_sign * leg_ratio * instrument_greek) * quantity
```

- Delta is in underlying coin; vega is USD per vol point; theta is USD per
  day; gamma is coin per USD move. Deribit does not publish vanna, so omit it
  or mark an estimate with `~`.
- For multi-leg option packages, calculate fill and mark on the same signed,
  ratio-weighted legs. Exclude perp hedge legs from option-premium offset.
- When resolved tape evidence supplies one row per leg, use the smallest
  absolute option-leg `QTY` as the package unit and weight every option row by
  `abs(QTY) / package_unit`. `BUY` is premium paid and `SELL` is premium
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
- Attribute a vol-surface move only when raw trade IV and later summary IV,
  timing, and size support it.

## Output

The entire successful response is two plain-text lines followed by one YAML
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

Use real numbers only. Drop a bracket row only when all of its data is
unavailable. Do not show working, tool narration, a data trace, or follow-up
commentary.
