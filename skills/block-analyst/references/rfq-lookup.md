# Resolve an RFQ without hot files

The first token after `/analyze` is the authoritative RFQ id. Accept only
`[A-Za-z0-9_-]` before using it in a query. Strip a leading `DRFQv2-` or
`GRFQ-` when comparing suffixes, but preserve the supplied id in the response.

There is no single current raw file that guarantees a complete executed fill
for every Paradigm RFQ. Use the available sources as evidence and stop when the
identity cannot be proved.

## Available sources

1. **Injected trade context** — if Dime supplies the cleared trade rows, use
   them directly. This is the strongest source because the id-to-fill mapping
   is already resolved.
2. **Current Paradigm RFQ tape** —
   `s3://dt-paradigm-data/paradigm_data/paradigm_rfq_tape_slim.csv.gz` carries
   `RFQ_ID`, product, structure description, quantity, quote currency, quote
   count, block count, status, and lifespan. It does **not** carry execution
   price, mark, taker side, trade id, or block id.
3. **Raw exchange trades** — use the layout and fields in
   `../../data-discovery/references/exchange-raw.md`. Deribit option trades
   carry `block_rfq_id` and `block_trade_id`; OKX carries `block_trade_id`;
   Bybit has only `is_block_trade`; Bullish carries OTC ids. An exact or
   suffix match on Deribit `block_rfq_id` is useful only when the values really
   agree.
4. **Historical executed tape** —
   `s3://dt-paradigm-data/paradigm_data/paradigm_trade_tape_slim.csv.gz`
   contains fill price, reference mark, side, trade id, and block id, but it is
   frozen at 2026-08-10. Use it only for RFQs at or before that date and label
   it historical.
5. **Venue public APIs** — use current tickers and recent venue trades to
   complete or corroborate a raw-file result. They cannot by themselves prove
   a Paradigm RFQ id that the venue does not publish.

## Model-selected lookup

Choose the smallest combination that can prove the requested trade:

- Query the current RFQ tape by exact id and suffix-tolerant core id.
- Use its authoritative `PRODUCT`, `DESCRIPTION`, `QTY`, and time to select
  narrow raw exchange partitions.
- Match a published `block_rfq_id` or real block id when possible.
- Otherwise compare time, venue, instrument/structure, size, side, and price,
  but describe the result as a candidate unless the identity is unique.
- Cluster multi-leg trades only on a real shared block id. Never reconstruct a
  package from proximity alone.

The inline description after the id is user reference, not authoritative
identity. It may help explain a mismatch after resolution, but it must not
seed an asset, instrument, fill, or block when the data did not resolve them.

## Failure behavior

If the RFQ request resolves but the execution does not, state that distinction:

```text
RFQ <id> found, but its executed fill could not be resolved from current raw exchange data.
```

If even the RFQ request does not resolve, state:

```text
RFQ <id> not resolved — no authoritative asset, structure, or fill available.
```

Do not use the hot 30-day Paradigm tape, default to BTC, reuse the inline
description as the fill, or fabricate missing price/mark/side fields.
