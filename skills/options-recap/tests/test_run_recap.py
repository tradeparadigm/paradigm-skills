#!/usr/bin/env python3
"""Offline checks for the direct-data recap command and collector plan."""

import datetime as dt
import importlib.util
import os
import re
import subprocess
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "run_recap.sh")
COLLECTOR = os.path.join(ROOT, "scripts", "collect_recap.py")

# collect_recap imports duckdb at module top. Stub it ONLY when it is genuinely
# absent (the stdlib-only workflow); if this file is ever collected into a
# pytest session alongside modules that need the real duckdb, a module-level
# setdefault would leak this connect=None stub into them.
if importlib.util.find_spec("duckdb") is None:
    sys.modules["duckdb"] = types.SimpleNamespace(Error=Exception, connect=None)

spec = importlib.util.spec_from_file_location("collect_recap", COLLECTOR)
collector = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = collector
spec.loader.exec_module(collector)

passed = failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
    else:
        failed += 1
        print(f"  ✗ {name}  {detail}")


def hook(name, *args):
    env = dict(os.environ, **{name: "1"})
    result = subprocess.run(["bash", SCRIPT, *args], capture_output=True, text=True, env=env)
    return (result.stdout.strip() or result.stderr.strip()), result.returncode


def test_arguments():
    check("default is BTC 24h", hook("RECAP_PRINT_ARGS")[0] == "BTC 24h")
    check("options token is ignored", hook("RECAP_PRINT_ARGS", "eth", "options", "8h")[0] == "ETH 8h")
    check("1d normalizes to 24h", hook("RECAP_PRINT_ARGS", "btc", "1d")[0] == "BTC 24h")
    check("window-first invocation", hook("RECAP_PRINT_ARGS", "8h", "options", "eth")[0] == "ETH 8h")
    check("window-only invocation", hook("RECAP_PRINT_ARGS", "8h")[0] == "BTC 8h")


def test_windows_are_not_capped_but_are_bounded():
    """The 24h clamp is gone — partitions serve any window — but not unbounded:
    beyond 30 days the execution tape has nothing and the glob spans every hour
    of every venue."""
    check("2d remains 2d", hook("RECAP_PRINT_PLAN", "eth", "2d") == ("ETH 2d 172800 direct", 0))
    check("7d remains 7d", hook("RECAP_PRINT_PLAN", "btc", "7d") == ("BTC 7d 604800 direct", 0))
    check("30d is allowed", hook("RECAP_PRINT_PLAN", "btc", "30d")[1] == 0)
    output, code = hook("RECAP_PRINT_PLAN", "btc", "31d")
    check("31d is refused before any read", code == 2, output)
    check("refusal names the limit", "30d" in output, output)


def test_bad_arguments_fail_before_data_access():
    for value in ("0h", "3x", "-2h", "foo"):
        output, code = hook("RECAP_PRINT_PLAN", "btc", value)
        check(f"{value} exits 2", code == 2, output)
    check("unsafe asset exits 2", hook("RECAP_PRINT_PLAN", "BTC';", "8h")[1] == 2)


def test_partition_plan_is_explicit_and_hot_free():
    start = dt.datetime(2026, 8, 30, 10, 30, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)
    queries = collector.build_queries("BTC", start, end)
    paths = [path for query in queries for path in query.paths]
    check("collector exposes venue-isolated evidence groups", len(queries) == 19, [q.name for q in queries])
    check("every path is direct", all("/raw/" in p or "/normalized/" in p or "/meta/" in p for p in paths))
    check("no hot path", all("/hot/" not in p and "hot__" not in p for p in paths))
    check("hours are explicit", all("hour=*" not in p for p in paths))
    check("exclusive end covers two UTC hours", len(queries[0].paths) == 2, len(queries[0].paths))
    surface_sql = next(query.sql for query in queries if query.name == "option_surface_deribit")
    check("surface samples every expiry and type independently", "PARTITION BY observation, expirationDate, optionType, target_delta" in surface_sql)


def test_evidence_contract_names_provenance_and_freshness():
    source = collector.Query("x", ["s3://direct"], "SELECT 1", {"price": "USD"}, True)
    original = collector.duckdb.connect

    collector.duckdb.connect = fake_connection(["s3://direct/a.parquet"])
    try:
        metadata, rows = collector.run_query(source)
    finally:
        collector.duckdb.connect = original
    check("source plan retained", metadata["path_plan"] == {
        "pattern_count": 1, "first_pattern": "s3://direct", "last_pattern": "s3://direct",
        "resolved_file_count": 1, "missing_pattern_count": 0})
    check("units retained", metadata["units"] == {"price": "USD"})
    check("event freshness retained", metadata["max_event_at"] == "2026-08-30T12:00:00Z")
    check("observations are not rendered", rows[0]["price"] == 1)


def fake_connection(glob_files):
    """A duckdb.connect stand-in whose glob() returns exactly `glob_files`."""

    class Connection:
        description = [("max_event_at",), ("price",)]

        def execute(self, sql):
            self._glob = sql.lstrip().startswith("SELECT file FROM glob(")
            self.description = [("file",)] if self._glob else [("max_event_at",), ("price",)]
            return self

        def fetchall(self):
            if self._glob:
                return [(name,) for name in glob_files]
            return [("2026-08-30T12:00:00Z", 1)]

        def close(self):
            pass

    return Connection


def test_absent_partition_does_not_erase_the_rest_of_the_window():
    """One unwritten hour must not take the whole window's evidence down."""
    patterns = [f"s3://bucket/hour={hour:02d}/**/*.parquet" for hour in (17, 18, 19)]
    source = collector.Query("trades", patterns, "SELECT * FROM read_parquet(__PATHS__)", {}, True)
    original = collector.duckdb.connect

    # Hours 17 and 18 landed; the current hour 19 has not been written yet.
    collector.duckdb.connect = fake_connection(
        ["s3://bucket/hour=17/a.parquet", "s3://bucket/hour=18/b.parquet"])
    try:
        metadata, rows = collector.run_query(source)
    finally:
        collector.duckdb.connect = original
    check("partial window still reads", metadata["status"] == "ok" and rows)
    check("resolved only the present partitions", metadata["path_plan"]["resolved_file_count"] == 2)
    check("absent partition counted", metadata["path_plan"]["missing_pattern_count"] == 1)
    check("absent partition named", metadata["path_plan"]["missing_patterns"] == [patterns[2]])


def test_fully_absent_window_reports_unavailable_not_a_quiet_market():
    patterns = ["s3://bucket/hour=17/**/*.parquet", "s3://bucket/hour=18/**/*.parquet"]
    source = collector.Query("trades", patterns, "SELECT * FROM read_parquet(__PATHS__)", {}, True)
    original = collector.duckdb.connect
    collector.duckdb.connect = fake_connection([])
    try:
        metadata, rows = collector.run_query(source)
    finally:
        collector.duckdb.connect = original
    check("no objects means unavailable", metadata["status"] == "unavailable")
    check("no rows fabricated", rows == [] and metadata["row_count"] == 0)
    check("error names the pattern span", "2 partition patterns" in metadata["error"])


def test_s3_reads_pin_the_regional_endpoint():
    """#37 pinned ENDPOINT so the S3 authority stays deterministic: the global
    host answers cross-region with a 307 that the enclave's exact-match egress
    allowlist cannot follow. Rewriting run_recap.sh dropped it once already."""
    skills = os.path.dirname(ROOT)
    unpinned = []
    for skill in ("data-discovery", "options-recap"):
        for directory, _, names in os.walk(os.path.join(skills, skill)):
            for name in names:
                if not name.endswith((".py", ".sh", ".md")):
                    continue
                path = os.path.join(directory, name)
                with open(path, encoding="utf-8") as handle:
                    body = handle.read()
                # Require the argument list: a bare prose mention of the phrase
                # would otherwise start a match that swallows the next real
                # statement, hiding an unpinned one.
                for statement in re.findall(
                        r"CREATE (?:OR REPLACE )?(?:PERSISTENT )?SECRET\s+\w+\s*\([^)]*\)", body):
                    if "CREDENTIAL_CHAIN" in statement and "ENDPOINT" not in statement:
                        unpinned.append(os.path.relpath(path, skills))
                # boto3 resolves its own endpoint, and AWS_ENDPOINT_URL or a
                # profile setting overrides that resolution. Match on the
                # constructor and its first argument, so boto3.resource,
                # session.client and a bare client() are all covered.
                for call in re.findall(
                        r"\b(?:client|resource)\(\s*[\'\"]s3[\'\"][^)]*\)", body):
                    if "endpoint_url" not in call:
                        unpinned.append(os.path.relpath(path, skills))
    check("every S3 read pins the regional endpoint", not unpinned, unpinned)


def main():
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            function()
    print(f"{passed} checks passed, {failed} failed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
