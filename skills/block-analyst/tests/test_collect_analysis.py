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
    # row_type is set, as the real tape sets it: the SQL this replaces read
    # `WHERE row_type='paradigm_trade'`, which drops a NULL rather than keeping
    # it, so a fixture without one is not a row the query would have returned.
    row = {"traded_at_iso": "2026-09-10T11:22:33Z", "product": "BTC OPTION - DBT",
           "description": "Call 25 Sep 26 70000", "quantity": 10, "trade_price": 0.01,
           "mark_price": 0.011, "taker_side": "BUY", "asset": "BTC",
           "row_type": "paradigm_trade",
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

# --- main(): every exit code and message it introduces -------------------
# main() had no executed lines: the subprocess uses above all exit before
# Python runs. 17 mutations survived, including `return 4 -> return 5`, which
# reports a dead producer as an unknown RFQ — the one substitution the module
# docstring forbids.
import io  # noqa: E402
import contextlib  # noqa: E402


def run_main(rfq, *, rows=None, raises=None, coverage=None, tmp=None):
    """main() end to end with the reader stubbed. Returns (code, stdout, stderr)."""
    real_read = ca.read_executions
    real_argv = sys.argv

    def fake(start, end, s3=None, now=None):
        if raises is not None:
            raise raises
        base = {"rows": rows or [], "coverage_complete": True,
                "source_watermark_ms": int(end.timestamp() * 1000)}
        base.update(coverage or {})
        return base

    ca.read_executions = fake
    sys.argv = ["collect_analysis.py", rfq, "--out-dir", str(tmp or tempfile.mkdtemp())]
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ca.main()
    finally:
        ca.read_executions = real_read
        sys.argv = real_argv
    return code, out.getvalue(), err.getvalue()


_code, _out, _err = run_main("r_target", rows=[tape_row()])
ok(_code == 0, f"a found block exits 0 [{_code}]")
ok("fill=1" in _out, f"and prints the counts on STDOUT [{_out.strip()}]")

_code, _out, _err = run_main("r_missing", rows=[tape_row()])
ok(_code == 5, f"an unknown id exits 5, not 0 [{_code}]")
ok("not found on the execution tape" in _err, f"on stderr [{_err.strip()[:70]}]")
ok(_out.strip() == "", "and prints nothing on stdout")

_code, _out, _err = run_main("r_missing", rows=[tape_row()],
                             coverage={"coverage_complete": False,
                                       "source_watermark_ms": 0,
                                       "coverage_note": "sync trails"})
ok(_code == 6, f"an unknown id under a stale tail exits 6, not 5 [{_code}]")
ok("not absence from" in _err, f"and says so rather than blaming the id [{_err.strip()[:70]}]")

_code, _out, _err = run_main("r_target", raises=RuntimeError("partition missing"))
ok(_code == 4, f"a reader refusal exits 4, NOT 5 [{_code}]")
ok("execution tape unavailable" in _err,
   "a dead producer is never reported as an unknown RFQ")

_amb = [tape_row(rfq_id="DRFQv2-r_dup", trade_id="t1"),
        tape_row(rfq_id="GRFQ-r_dup", trade_id="t2")]
_code, _out, _err = run_main("r_dup", rows=_amb)
ok(_code == 3, f"an id in two namespaces exits 3 [{_code}]")
ok("ambiguous" in _err or "namespaces" in _err,
   f"and says why, on STDERR so it cannot be relayed as the analysis [{_err.strip()[:60]}]")
ok(_out.strip() == "", "with nothing on stdout")
# Following the remediation must RESOLVE it, not reproduce it.
_code, _out, _err = run_main("GRFQ-r_dup", rows=_amb)
ok(_code == 0, f"and the prefixed id it tells you to use then works [{_code}] {_err.strip()[:60]}")

# Recurrence under a stale tail is a floor, on the path where a block WAS found.
_code, _out, _err = run_main("r_target", rows=[tape_row()],
                             coverage={"coverage_complete": False,
                                       "source_watermark_ms": 0,
                                       "coverage_note": "sync trails"})
ok(_code == 0, "a found block under a stale tail still succeeds")
ok("recurrence is a FLOOR" in _err,
   f"but says the count is a floor [{_err.strip()[:70]}]")

# A NULL row_type is DROPPED, as `WHERE row_type='paradigm_trade'` drops it.
# Keeping it put the same rfq_id in both fill and hist and inflated recurrence.
_null_rt = dict(tape_row(trade_id="t_null"))
_null_rt.pop("row_type")
# The guard must fire on the regression that actually happens: polars keeps the
# key when the value is NULL or the column is renamed, so a key-presence test
# passes while shaped() returns nothing and every id reports as never traded.
for _broken in ({"row_type": None}, {"row_type": "paradigm_trade_v2"}):
    try:
        ca.shaped([tape_row(**_broken)])
        ok(False, f"a tape whose row_type is {_broken['row_type']!r} raises")
    except KeyError:
        ok(True, f"a tape whose row_type is {_broken['row_type']!r} raises")

_mixed = ca.shaped([tape_row(trade_id="t_real"), _null_rt])
ok([r["TRADE_ID"] for r in _mixed] == ["t_real"],
   f"a row with no row_type is dropped, as the SQL dropped it {[r['TRADE_ID'] for r in _mixed]}")
# A column absent from EVERY row is a different failure — a schema change —
# and must raise rather than silently return nothing.
try:
    ca.shaped([_null_rt])
    ok(False, "a tape with no row_type column at all raises")
except KeyError:
    ok(True, "a tape with no row_type column at all raises")
_counts, _ = collect([tape_row(trade_id="t1"), _null_rt])
ok(_counts["hist"] == 1, f"recurrence counts only classified rows [{_counts}]")

# --- the tape -> CSV field mapping ----------------------------------------
# Swapping PRICE and REF_PRICE left all 176 tests green, which would invert
# every bps offset the skill publishes.
_mapped = ca.shaped([tape_row(quantity=7, trade_price=0.02, mark_price=0.011,
                              taker_side="SELL", product="ETH OPTION - DBT",
                              description="Put 25 Sep 26 3000")])[0]
ok(_mapped["PRICE"] == 0.02, f"PRICE is the TRADE price [{_mapped['PRICE']}]")
ok(_mapped["REF_PRICE"] == 0.011, f"REF_PRICE is the MARK price [{_mapped['REF_PRICE']}]")
ok(_mapped["PRICE"] > _mapped["REF_PRICE"],
   "so a fill above mark reads as above mark, not below")
ok(_mapped["QTY"] == 7, f"QTY is the quantity [{_mapped['QTY']}]")
ok(_mapped["SIDE"] == "SELL", f"SIDE is the TAKER side [{_mapped['SIDE']}]")
ok(_mapped["PRODUCT"] == "ETH OPTION - DBT", "PRODUCT is the product")
ok(_mapped["DESCRIPTION"] == "Put 25 Sep 26 3000", "DESCRIPTION is the description")

import datetime as dt  # noqa: E402

# main() stripped the id for the VALIDITY check and then passed the unstripped
# one to collect(), so a padded id reported "not found … covered the full
# requested window" — the substitution this module exists to prevent.
for _padded in (" r_target ", "\tDRFQv2-r_target\n"):
    _code, _out, _err = run_main(_padded, rows=[tape_row()])
    ok(_code == 0, f"a padded id {_padded!r} resolves [{_code}] {_err.strip()[:50]}")

# `blocks` counts DISTINCT block ids. Every other fixture is one leg per block,
# which is not the multi-leg case this skill exists for, so `len({ids})` ->
# `len(hist)` survived: a two-leg straddle sharing b1 would count as two.
_two_leg = [tape_row(trade_id="t1", block_trade_id="b1",
                     description="Call 25 Sep 26 70000"),
            tape_row(trade_id="t2", block_trade_id="b1",
                     description="Call 25 Sep 26 70000")]
_counts, _ = collect(_two_leg)
ok(_counts["hist"] == 2, f"both legs land in hist [{_counts}]")
ok(_counts["blocks"] == 1, f"but they are ONE block, not two [{_counts}]")

# The 30-day window is the skill's documented horizon and nothing pinned it —
# the reader is stubbed everywhere, so HORIZON 30->7 passed unnoticed.
_window = {}


def _capture_window(start, end, s3=None, now=None):
    _window["days"] = round((end - start).total_seconds() / 86400)
    return {"rows": [], "coverage_complete": True}


_real_read = ca.read_executions
ca.read_executions = _capture_window
try:
    with tempfile.TemporaryDirectory() as _d:
        ca.collect("r_x", Path(_d))
finally:
    ca.read_executions = _real_read
ok(_window.get("days") == 30,
   f"collect reads the documented 30-day horizon [{_window.get('days')}]")

# The coverage predicate, at the watermarks that actually occur. Both earlier
# fixtures used a 1970 watermark — the one value where every candidate threshold
# agrees, and one the real reader can never emit (execution_tape.py:117 maps
# falsy to None).
_recent = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=40)).timestamp() * 1000)
ok(ca.coverage_edge({"coverage_complete": False, "source_watermark_ms": _recent}) != "",
   "a 40-minute-old watermark is still a short read, and says so")
ok("covers through" in ca.coverage_edge(
       {"coverage_complete": False, "source_watermark_ms": _recent}),
   "naming the boundary rather than judging which side of it the trade is on")
ok(ca.coverage_edge({"coverage_complete": True}) == "",
   "a complete read names no boundary")
# A single partition predating the watermark field makes the whole read unknown.
_unknown = ca.coverage_edge({"coverage_complete": False, "source_watermark_ms": None})
ok("unknown" in _unknown, f"a missing watermark is UNKNOWN coverage, not complete [{_unknown}]")
ok(_unknown != "", "and still routes to exit 6 rather than a confident not-found")

# A malformed id is exit 2, the code the table documents — not the outage code.
for _bad in ("DRFQv2-", "GRFQ-", "   "):
    _code, _out, _err = run_main(_bad, rows=[tape_row()])
    ok(_code == 2, f"a malformed id {_bad!r} exits 2, not 4 [{_code}]")
    ok("invalid rfq_id" in _err, f"and says so rather than blaming the tape [{_err.strip()[:50]}]")

# --- analyze.sh's failure dispatch, driven end to end ----------------------
# The suite only grepped this file, so five of six mutations survived the gate:
# `exit "$status"` -> `exit 0`, the whole case deleted, arms swapped, `-ne` ->
# `-eq`, and a revert of the exit-4 message. A stub `uv` on PATH exercises the
# real script with no production change.
import subprocess  # noqa: E402

SH = Path(HERE).parent / "scripts" / "analyze.sh"


def run_sh(code, note="analyze: stub said so", rfq="r_target"):
    """Drive analyze.sh with `uv` stubbed to a chosen exit code and stderr."""
    with tempfile.TemporaryDirectory() as bin_dir:
        # The stub distinguishes its two callers. analyze.sh runs `uv` twice —
        # collect first, then analyze.py — and a stub returning the same code
        # for both hides whether the script actually STOPPED on the failure.
        stub = Path(bin_dir) / "uv"
        marker = Path(bin_dir) / "second-call"
        stub.write_text(
            "#!/bin/sh\n"
            f"if [ -f {shlex.quote(str(marker))} ]; then\n"
            f"  echo REACHED_ANALYZE_PY\n"
            "  exit 0\n"
            "fi\n"
            f"touch {shlex.quote(str(marker))}\n"
            # uv's OWN stderr, which a cold package cache really does emit.
            "printf 'Installed 13 packages in 106ms\\n' >&2\n"
            f"printf '%s\\n' {shlex.quote(note)} >&2\n"
            f"exit {code}\n")
        stub.chmod(0o755)
        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
        done = subprocess.run(["bash", str(SH), rfq], capture_output=True,
                              text=True, env=env)
        return done.returncode, done.stdout, done.stderr


import shlex  # noqa: E402
import os  # noqa: E402

for _code in (3, 4, 5, 6, 127):
    _rc, _out, _ = run_sh(_code)
    ok(_rc == _code, f"analyze.sh propagates exit {_code} [{_rc}]")
    ok("stub said so" in _out,
       f"and relays collect's own message on stdout for {_code} [{_out.strip()[:50]}]")
    # The point of the gate: a failed resolve must not go on to render a block
    # from an empty directory.
    ok("REACHED_ANALYZE_PY" not in _out,
       f"and stops before analyze.py on exit {_code}")

# The FLOOR case: exit 0 WITH a note. The note is part of the answer.
_rc, _out, _ = run_sh(0, note="analyze: recurrence is a FLOOR — the read covers through X")
ok(_rc == 0, f"a successful run still exits 0 [{_rc}]")
ok("REACHED_ANALYZE_PY" in _out, "and DOES go on to render the block")
ok("recurrence is a FLOOR" in _out,
   f"and its note reaches stdout, not just stderr [{_out.strip()[:60]}]")

# analyze.sh must pass the id AS GIVEN. Handing collect the namespace-stripped
# CORE instead reinstates the ambiguity defect closed in round 2: the prefixed
# id the error message tells you to re-run with would stop being honoured.
_seen = Path(tempfile.mkdtemp()) / "argv"
def run_sh_argv(rfq):
    with tempfile.TemporaryDirectory() as bin_dir:
        stub = Path(bin_dir) / "uv"
        out = Path(bin_dir) / "argv.txt"
        stub.write_text("#!/bin/sh\n"
                        f"for a in \"$@\"; do printf '%s\\n' \"$a\"; done > {shlex.quote(str(out))}\n"
                        "exit 4\n")
        stub.chmod(0o755)
        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
        subprocess.run(["bash", str(SH), rfq], capture_output=True, text=True, env=env)
        return out.read_text().splitlines() if out.exists() else []

_argv = run_sh_argv("DRFQv2-r_target")
ok("DRFQv2-r_target" in _argv,
   f"analyze.sh hands collect the id as given, not the stripped core {_argv}")
ok("r_target" not in _argv, f"and not the bare core {_argv}")

# uv's own chatter must never reach stdout: SKILL.md tells the model stdout is
# its entire reply, so a cold cache would have put "Installed 13 packages in
# 106ms" at the top of a block analysis.
for _c in (0, 5):
    _rc, _out, _ = run_sh(_c)
    ok("Installed 13 packages" not in _out,
       f"uv's own stderr is not relayed on exit {_c} [{_out.strip()[:60]}]")

# No note, no noise.
_rc, _out, _ = run_sh(0, note="")
ok(_rc == 0 and "analyze:" not in _out, f"a clean run adds nothing [{_out.strip()[:40]}]")

# --- no skill file directs a read at a hot object or v_vol_surface ---------
import re  # noqa: E402

SKILL_DOCS = (sorted(Path(HERE).parent.glob("*.md"))
              + sorted((Path(HERE).parent / "references").glob("*.md")))

# rfq-lookup.md's manual fallback hardcoded year=2026/month=09; from 1 October
# that reads an empty set and answers every id "not found".
for doc in SKILL_DOCS:
    pinned = re.findall(r"year=\d{4}/month=\d{2}", doc.read_text())
    ok(not pinned, f"{doc.name} pins no calendar month in a read path: {pinned}")

SKILL = Path(HERE).parent
for doc in SKILL_DOCS:
    text = doc.read_text()
    # A prohibition names the object to forbid it; a READ puts it in a path.
    reads = re.findall(r"s3://\S*(?:hot/|hot__|v_vol_surface)\S*", text)
    reads += re.findall(r"`[^`]*(?:hot__|v_vol_surface)[^`]*\.parquet`", text)
    ok(not reads, f"{doc.name} directs no read at a hot object: {reads}")

print(f"\n{_p} passed, {_f} failed")
sys.exit(1 if _f else 0)
