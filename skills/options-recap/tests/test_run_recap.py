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
    # The relay follows script output more closely than a rule it read earlier;
    # twice it explained the refusal and then ran 30d anyway.
    check("refusal tells the relay not to substitute", "Do not re-run at 30d" in output, output)


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
    check("exclusive end covers two UTC hours",
          queries[0].expected_hours == ("20260830T10", "20260830T11"), queries[0].expected_hours)
    # One listing per day, not per hour: the same objects for 1/24 of the S3 calls.
    check("one glob per day, not per hour", len(queries[0].paths) == 1, queries[0].paths)
    surface_sql = next(query.sql for query in queries if query.name == "option_surface_deribit")
    check("surface samples every expiry and type independently", "PARTITION BY observation, expirationDate, optionType, target_delta" in surface_sql)





def test_render_queries_keep_their_coverage_expectations():
    """The render path rebuilds each Query; dropping a field there silently
    disabled hour-level coverage on the one path /recap actually runs."""
    start = dt.datetime(2026, 8, 30, 10, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 8, 30, 13, 0, tzinfo=dt.timezone.utc)
    plain = {q.name: q for q in collector.build_queries("BTC", start, end)}
    for rendered in collector.build_queries("BTC", start, end, render=True):
        check(f"{rendered.name} keeps expected_hours",
              rendered.expected_hours == plain[rendered.name].expected_hours,
              rendered.name)
        check(f"{rendered.name} keeps its reader",
              rendered.stream == plain[rendered.name].stream, rendered.name)
        check(f"{rendered.name} keeps its projection",
              rendered.columns == plain[rendered.name].columns, rendered.name)


def test_streamed_queries_name_every_column_their_sql_reads():
    """The async reader projects to Query.columns, so a column the SQL uses but
    the tuple omits is silently absent at query time."""
    start = dt.datetime(2026, 8, 30, 10, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)
    streamed = [q for q in collector.build_queries("BTC", start, end) if q.stream]
    # The identifiers the reader must hand DuckDB are exactly the ones named
    # before FROM read_parquet(...) — read them off the SQL rather than testing
    # a hardcoded tuple, which compared nothing.
    noise = {"with", "as", "materialized", "select", "distinct", "filename",
             "arg_min", "arg_max", "min", "max", "sum", "count", "any_value"}
    for query in streamed:
        check(f"{query.name} declares its columns", bool(query.columns), query.name)
        head = query.sql.split("FROM read_parquet", 1)[0]
        head = re.sub(r"--[^\n]*", " ", head)
        skip = set(re.findall(r"\bAS\s+([a-z_][a-z0-9_]*)", head, re.I))
        skip |= set(re.findall(r"\bWITH\s+([a-z_][a-z0-9_]*)", head, re.I))
        named = set(re.findall(r"\b[a-z_][a-z0-9_]*\b", head.lower())) - noise - skip
        missing = named - set(query.columns)
        check(f"{query.name} projects every source column its SQL names",
              not missing, sorted(missing))




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
                # The async reader does not go through boto3 for its GETs, so
                # it carries the pin on its own store constructor.
                # Match the constructor AND its argument list, like the
                # client/resource guard above: checking the whole file let a
                # second, unpinned store pass in a file that already had one.
                # Code only — prose naming the constructor builds nothing.
                for call in re.findall(r"\bS3Store\([^)]*\)",
                                       body if name.endswith(".py") else ""):
                    if "endpoint=" not in call:
                        unpinned.append(os.path.relpath(path, skills))
    check("every S3 read pins the regional endpoint", not unpinned, unpinned)


def test_importing_collect_recap_makes_the_shared_reader_importable():
    """s3_async lives in data-discovery now. Every other test either stubs it in
    sys.modules or loads it by path, so nothing would notice if the module-scope
    sys.path insert went away — and every stream=True query would degrade to
    "unavailable" in production only."""
    scripts = os.path.join(ROOT, "scripts")
    # find_spec resolves the path without executing it, so this stays stdlib-only:
    # s3_async imports obstore and pyarrow, which this lane does not install.
    program = (
        "import sys, types, importlib.util as u;"
        "sys.modules.setdefault('duckdb', types.SimpleNamespace(Error=Exception, connect=None));"
        f"sys.path.insert(0, {scripts!r});"
        f"spec = u.spec_from_file_location('collect_recap', {COLLECTOR!r});"
        "mod = u.module_from_spec(spec);"
        "sys.modules['collect_recap'] = mod;"
        "spec.loader.exec_module(mod);"
        "found = u.find_spec('s3_async');"
        "print(found.origin if found else 'NOT FOUND')"
    )
    result = subprocess.run([sys.executable, "-c", program],
                            capture_output=True, text=True)
    origin = result.stdout.strip()
    check("importing collect_recap puts the shared reader on the path",
          result.returncode == 0
          and origin.endswith(os.path.join("data-discovery", "scripts", "s3_async.py")),
          origin or result.stderr.strip()[-200:])


def main():
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            function()
    print(f"{passed} checks passed, {failed} failed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()

