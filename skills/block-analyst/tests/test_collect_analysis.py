#!/usr/bin/env python3
"""
Unit tests for collect_analysis.py — no network, no deps beyond the reader's.
Run: python3 tests/test_collect_analysis.py

These pin the two things the DuckDB scan used to do and the CSV contract
analyze.py depends on: QUOTE_CURRENCY is DERIVED (not `asset` renamed), and
hist holds OTHER blocks of the same structure rather than the fill's own.
"""
import os
import sys
import tempfile
import csv
from pathlib import Path

import importlib.util
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
sys.path.insert(0, os.path.join(HERE, "..", "..", "data-discovery", "scripts"))

# This lane is stdlib-only. execution_tape imports boto3 and polars at module
# scope, and read_executions is stubbed in every test below, so absent ones are
# replaced rather than installed — and ONLY when genuinely absent, so a pytest
# session that has the real ones is never given these.
for name in ("boto3", "polars"):
    if importlib.util.find_spec(name) is None:
        sys.modules[name] = types.SimpleNamespace(
            client=lambda *a, **k: None, DataFrame=object, concat=lambda *a, **k: None)
import collect_analysis as ca  # noqa: E402

_p = _f = 0


def ok(cond, msg):
    global _p, _f
    if cond:
        _p += 1
    else:
        _f += 1
        print(f"  ✗ {msg}")


def tape_row(**over):
    row = {"traded_at_iso": "2026-09-10T11:22:33Z", "product": "BTC OPTION - DBT",
           "description": "Call 25 Sep 26 70000", "quantity": 10, "trade_price": 0.01,
           "mark_price": 0.011, "taker_side": "BUY", "asset": "BTC",
           "instrument_name": "BTC-25SEP26-70000-C", "rfq_id": "DRFQv2-r_target",
           "trade_id": "t1", "block_trade_id": "b1"}
    row.update(over)
    return row


def collect(rows, rfq="r_target"):
    """Drive collect() with the reader stubbed — the reader has its own tests."""
    real = ca.read_executions
    ca.read_executions = lambda *a, **k: {"rows": rows}
    try:
        with tempfile.TemporaryDirectory() as d:
            counts = ca.collect(rfq, Path(d))
            out = {}
            for name in ("fill", "hist"):
                path = Path(d) / f"{name}.csv"
                out[name] = list(csv.DictReader(path.open())) if path.exists() else []
            return counts, out
    finally:
        ca.read_executions = real


# --- QUOTE_CURRENCY is derived, not renamed -------------------------------
ok(ca.quote_currency(tape_row()) == "BTC", "DBT + BTC + non-USDC instrument -> BTC")
ok(ca.quote_currency(tape_row(instrument_name="BTC_USDC-25SEP26-70000-C")) == "USDC",
   "a USDC instrument name -> USDC even on DBT")
ok(ca.quote_currency(tape_row(product="BTC OPTION - BYB")) == "USDC",
   "a non-DBT venue -> USDC")
ok(ca.quote_currency(tape_row(instrument_name=None)) == "USDC",
   "no instrument name -> USDC, never a bare asset rename")
ok(ca.quote_currency(tape_row(asset="SOL")) == "USDC", "an asset outside BTC/ETH -> USDC")

# --- hist is OTHER blocks of the same structure ---------------------------
same = tape_row(rfq_id="DRFQv2-r_other", trade_id="t2", block_trade_id="b2",
                traded_at_iso="2026-09-02T08:00:00Z")
other = tape_row(rfq_id="DRFQv2-r_third", trade_id="t3", block_trade_id="b3",
                 description="Put 25 Sep 26 50000")
counts, out = collect([tape_row(), same, other])
ok(counts["fill"] == 1, "fill holds only the target RFQ's legs")
ok(counts["hist"] == 2, "hist holds the fill plus the other block of that structure")
ok(counts["blocks"] == 2, "recurrence counts distinct blocks, not rows")
ok({r["BLOCK_TRADE_ID"] for r in out["hist"]} == {"b1", "b2"},
   "a different structure is excluded from hist")
ok(out["hist"][0]["DATE"] == "2026-09-10", "hist is newest first")

# --- the CSV contract analyze.py reads ------------------------------------
ok(list(out["fill"][0]) == list(ca.FILL_COLUMNS), "fill.csv header matches the contract")
ok(list(out["hist"][0]) == list(ca.HIST_COLUMNS), "hist.csv header matches the contract")
ok(out["fill"][0]["QUOTE_CURRENCY"] == "BTC", "fill.csv carries the derived quote currency")
ok("_DESC_N" not in out["fill"][0], "the internal match key stays out of the CSV")

# --- description normalisation drives the match ---------------------------
spaced = tape_row(rfq_id="DRFQv2-r_other", trade_id="t4", block_trade_id="b4",
                  description="Call  25 Sep 26  70000")
counts, _ = collect([tape_row(), spaced])
ok(counts["blocks"] == 2, "spacing differences still match the same structure")

# --- an id in two namespaces is refused, not silently merged --------------
try:
    collect([tape_row(rfq_id="DRFQv2-r_target"), tape_row(rfq_id="GRFQ-r_target", trade_id="t9")])
    ok(False, "two namespaces for one core id raises")
except ca.AmbiguousRfqError as exc:
    ok("DRFQv2-" in str(exc) and "GRFQ-" in str(exc),
       "the ambiguity names both namespaces so the user can re-run")

# --- row_type is still filtered, as the SQL did ---------------------------
noise = tape_row(rfq_id="DRFQv2-r_target", trade_id="t8", block_trade_id="b8",
                 row_type="quote_update")
counts, _ = collect([tape_row(), noise])
ok(counts["fill"] == 1, "a non-paradigm_trade row carrying an rfq_id stays out of fill")

# --- an empty instrument_name takes the coin branch, as the SQL did -------
ok(ca.quote_currency(tape_row(instrument_name="")) == "BTC",
   "empty instrument_name is NOT NULL in SQL, so it derives the coin")

# --- an unknown id is not an error ----------------------------------------
counts, out = collect([tape_row()], rfq="r_missing")
ok(counts["fill"] == 0 and out["fill"] == [], "an unmatched RFQ writes nothing")

# --- analyze.sh routes each failure to its own message ---------------------
import subprocess  # noqa: E402

SH = os.path.join(HERE, "..", "scripts", "analyze.sh")

ok(subprocess.run(["bash", "-n", SH], capture_output=True).returncode == 0,
   "analyze.sh parses")
printed = subprocess.run(["bash", SH, "DRFQv2-r_Abc"], capture_output=True, text=True,
                         env={**os.environ, "ANALYZE_PRINT_ID": "1"})
ok(printed.stdout.strip() == "r_Abc", "the id hook strips the namespace prefix")
bad = subprocess.run(["bash", SH, "r_a;rm -rf /"], capture_output=True, text=True)
ok(bad.returncode == 2 and "invalid rfq_id" in bad.stdout,
   "a shell metacharacter in the id is refused before anything runs")

body = Path(SH).read_text()
# Comments still NAME what was removed, which is the point of them. Assert on
# the executable lines only.
code = "\n".join(l for l in body.splitlines() if not l.lstrip().startswith("#"))
ok("SecretAccessKey" not in code and "s3_access_key_id" not in code and "sts." not in code,
   "no credentials are handled in the shell any more")
ok("s3://" not in code and "read_parquet" not in code and "duckdb" not in code.lower(),
   "the shell no longer reads S3 or runs SQL")
# `status=$?` straight after `if ! cmd` reads the negation, not the command.
ok("if ! uv run" not in code, "the exit status is captured from the command, not a negation")

# --- absence under incomplete coverage is not "not found" ------------------
real = ca.read_executions
ca.read_executions = lambda *a, **k: {"rows": [tape_row()], "coverage_complete": False,
                                      "coverage_note": "sync trails by 40 min"}
try:
    import tempfile as _t
    with _t.TemporaryDirectory() as d:
        c = ca.collect("r_absent", Path(d))
    ok(c["fill"] == 0 and c.get("coverage_complete") is False,
       "an unmatched id under incomplete coverage carries the coverage state out")
    ok(c.get("coverage_note") == "sync trails by 40 min", "the note survives for the message")
finally:
    ca.read_executions = real

# --- no skill file directs a read at a hot object or v_vol_surface ---------
import re  # noqa: E402

SKILL = Path(HERE).parent
for doc in sorted(SKILL.glob("*.md")) + sorted((SKILL / "references").glob("*.md")):
    text = doc.read_text()
    # A prohibition names the object to forbid it; a READ puts it in a path.
    reads = re.findall(r"s3://\S*(?:hot/|hot__|v_vol_surface)\S*", text)
    reads += re.findall(r"`[^`]*(?:hot__|v_vol_surface)[^`]*\.parquet`", text)
    ok(not reads, f"{doc.name} directs no read at a hot object: {reads}")

print(f"\n{_p} passed, {_f} failed")
sys.exit(1 if _f else 0)
