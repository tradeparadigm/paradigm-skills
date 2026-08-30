#!/usr/bin/env python3
"""Offline checks for the direct-data recap command and collector plan."""

import datetime as dt
import importlib.util
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "run_recap.sh")
COLLECTOR = os.path.join(ROOT, "scripts", "collect_recap.py")

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


def test_windows_are_not_capped():
    check("2d remains 2d", hook("RECAP_PRINT_PLAN", "eth", "2d") == ("ETH 2d 172800 direct", 0))
    check("31d remains 31d", hook("RECAP_PRINT_PLAN", "btc", "31d") == ("BTC 31d 2678400 direct", 0))


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
    check("window covers three UTC hours", len(queries[0].paths) == 3, len(queries[0].paths))


def test_evidence_contract_names_provenance_and_freshness():
    source = collector.Query("x", ["s3://direct"], "SELECT 1", {"price": "USD"}, True)
    original = collector.subprocess.run
    class Result:
        returncode = 0
        stdout = '[{"max_event_at":"2026-08-30T12:00:00Z","price":1}]'
        stderr = ""
    collector.subprocess.run = lambda *args, **kwargs: Result()
    try:
        metadata, rows = collector.run_query(source)
    finally:
        collector.subprocess.run = original
    check("source plan retained", metadata["path_plan"] == {
        "pattern_count": 1, "first_pattern": "s3://direct", "last_pattern": "s3://direct"})
    check("units retained", metadata["units"] == {"price": "USD"})
    check("event freshness retained", metadata["max_event_at"] == "2026-08-30T12:00:00Z")
    check("observations are not rendered", rows[0]["price"] == 1)


def main():
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            function()
    print(f"{passed} checks passed, {failed} failed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
