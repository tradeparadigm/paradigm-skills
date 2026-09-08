# Options recap

`/recap [asset] [options] [window]` renders four sections from direct market
data: Snapshot, Biggest Print, Block Flow, and Vol Surface.

The active skill starts with `scripts/run_recap.sh`. Bounded raw and normalized
partitions replace the hot-file inputs; `direct_inputs.py` maps them to the
existing calculations and four-section renderer. The command prints the finished
recap, which the model relays verbatim without extra scans or arithmetic.

The workflow does not read `s3://dt-exchange-venue-data/hot/`, use `hot__*`
objects. Snapshot Volume is USD premium turnover; Biggest Print and Block Flow
retain their underlying-USD notional ranking. Instrument conversions use
at-or-before metadata snapshots; unresolved conversions are visible gaps.

Missing data fails visibly: the recap names the unavailable field or section
instead of simulating values, treating a failed read as quiet flow, or silently
changing the requested window.

Formatting details live in `references/output-format.md`. Raw paths, fields,
units, freshness rules, and query patterns live in the shared exchange-raw
reference.

## Validation

```bash
python3 tests/test_recap.py
python3 tests/test_run_recap.py
python3 tests/test_vol_math.py
```

These tests cover source selection, calculation and output contracts. Run the
pytest adapter tests with `python -m pytest tests/test_direct_inputs.py` as well.
