# OHLCV candles

`/ohlcv [asset] [venue] [interval] [window]` renders open/high/low/close/volume
bars for one market, built from bounded raw exchange partitions.

The active skill starts with `scripts/run_ohlcv.sh`, which parses the command,
refuses a window it cannot serve, and hands the parsed request to
`scripts/collect_ohlcv.py`. That reads the selected partitions, buckets trades
by event time, and prints the finished table. The model relays stdout without
recalculating anything.

Bars are venue-local: trades are never merged across venues into one series.
The workflow does not read `s3://dt-exchange-venue-data/hot/` or any `hot__*`
object.

Missing data fails visibly. A period whose partition was absent and a period
with no trades are different facts, reported as separate coverage lines and
never rendered as a zero-volume bar.

Formatting lives in `references/output-format.md`. Partition paths, fields,
units and the DuckDB credential preamble live in the shared data-discovery
references.

## Validation

```bash
python3 tests/test_run_ohlcv.py
python3 tests/test_bars.py
python3 tests/test_render.py
```

These are self-running stdlib-only scripts: each executes its checks and exits
non-zero on failure. Modules that need pytest, polars or duckdb run in the
repository's dependency-equipped lane instead.
