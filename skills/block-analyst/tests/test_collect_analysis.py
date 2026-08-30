#!/usr/bin/env python3
"""Offline contract checks for the direct-data RFQ collector."""

import importlib.util
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "analyze.sh")
COLLECTOR = os.path.join(ROOT, "scripts", "collect_analysis.py")
spec = importlib.util.spec_from_file_location("collect_analysis", COLLECTOR)
collector = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = collector
spec.loader.exec_module(collector)


def test_wrapper_contract():
    env = dict(os.environ, ANALYZE_PRINT_ID="1")
    result = subprocess.run(["bash", SCRIPT, "DRFQv2-r_test-1"], capture_output=True, text=True, env=env)
    assert result.returncode == 0
    assert result.stdout.strip() == "r_test-1"
    with open(SCRIPT) as handle:
        source = handle.read()
    assert "collect_analysis.py" in source
    assert "/hot/" not in source and "hot__" not in source and "--render" not in source


def test_invalid_id_fails_before_data_access():
    result = subprocess.run(["bash", SCRIPT, "r_bad'id"], capture_output=True, text=True)
    assert result.returncode == 2


def test_suffix_predicate_is_id_only():
    predicate = collector.suffix_predicate("RFQ_ID", "r_test-1")
    assert "RFQ_ID" in predicate and "r_test-1" in predicate
    assert "DESCRIPTION" not in predicate and "PRODUCT" not in predicate


def test_raw_lookup_is_bounded_to_event_hours():
    paths = collector.raw_deribit_paths({
        "DATE": "2026-08-30", "TIME": "00:30:00",
        "PRODUCT": "BTC OPTION - DBT", "DESCRIPTION": "Call",
    })
    assert len(paths) == 3
    assert all("/raw/" in path and "level=5m" in path and "hour=*" not in path for path in paths)
    assert any("day=29/hour=23" in path for path in paths)
    assert any("day=30/hour=01" in path for path in paths)


def test_unresolved_anchor_does_not_scan_bucket():
    assert collector.raw_deribit_paths({"DESCRIPTION": "user supplied guess"}) == []


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"{len(tests)} tests passed")
