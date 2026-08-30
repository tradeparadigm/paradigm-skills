# Options recap

`/recap [asset] [options] [window]` renders four sections from direct market
data: Snapshot, Biggest Print, Block Flow, and Vol Surface.

The active skill starts with `scripts/run_recap.sh`, a deterministic collector
over bounded raw and normalized per-message partitions. It emits one general
JSON evidence document; the model decides which evidence matters and may make
narrower follow-up reads.

The workflow does not read `s3://dt-exchange-venue-data/hot/`, use `hot__*`
objects, or render a fixed answer inside the command.

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

These tests cover the collector contract and retained deterministic analytical
helpers; they do not prescribe the model's interpretation of the evidence.
