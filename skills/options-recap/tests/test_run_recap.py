#!/usr/bin/env python3
"""
Tests for run_recap.sh argument normalization — no creds, no network.

run_recap.sh resolves `<asset> <window>` from positional args and drops a stray
"options"/"option" keyword that some users include (`/recap btc options 8h`).
Left in, that token would land in the window slot and break the run
(hot__recap_options.parquet doesn't exist; parse_window_ms raises). We invoke the
REAL script with RECAP_PRINT_ARGS=1, which echoes the resolved "ASSET WIN" and
exits 0 before any STS/DuckDB work — so this exercises the actual parsing in CI
with no AWS creds and no S3.

Run: python3 tests/test_run_recap.py
"""

import os
import subprocess
import sys
import time

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "run_recap.sh")

_passed = 0
_failed = 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  ✗ {name}  {detail}")


def resolve(*args):
    """Run run_recap.sh with the print-args hook; return (stdout.strip(), rc)."""
    env = dict(os.environ, RECAP_PRINT_ARGS="1")
    r = subprocess.run(["bash", SCRIPT, *args], capture_output=True, text=True,
                       env=env, timeout=20)
    return r.stdout.strip(), r.returncode


def plan(*args):
    """Run with the print-plan hook; return ("ASSET WIN SECS PRESET", rc). Also
    surfaces stderr on non-zero rc so bad-window guards are checkable."""
    env = dict(os.environ, RECAP_PRINT_PLAN="1")
    r = subprocess.run(["bash", SCRIPT, *args], capture_output=True, text=True,
                       env=env, timeout=20)
    return (r.stdout.strip() or r.stderr.strip()), r.returncode


def sources(now_s, *args):
    """Run with the print-sources hook and a pinned clock; return
    "ASSET WIN START_MS VS_COLD|-". Exercises the real surface-open source
    resolution (window-start date math → cold partition path) with no creds."""
    env = dict(os.environ, RECAP_PRINT_SOURCES="1", RECAP_NOW_S=str(now_s))
    r = subprocess.run(["bash", SCRIPT, *args], capture_output=True, text=True,
                       env=env, timeout=20)
    return r.stdout.strip()


def test_plain_args():
    out, rc = resolve("BTC", "8h")
    check("BTC 8h passes through", out == "BTC 8h", out)
    check("exit 0", rc == 0, rc)


def test_asset_uppercased():
    out, _ = resolve("btc", "4h")
    check("lowercase asset uppercased", out == "BTC 4h", out)


def test_strips_options_keyword():
    out, rc = resolve("btc", "options", "8h")
    check("'options' dropped → BTC 8h", out == "BTC 8h", out)
    check("exit 0 (not the break path)", rc == 0, rc)


def test_strips_options_case_insensitive():
    check("OPTIONS dropped", resolve("BTC", "OPTIONS", "8h")[0] == "BTC 8h")
    check("singular 'option' dropped", resolve("eth", "option", "4h")[0] == "ETH 4h")


def test_options_with_1d_window():
    # 1d→24h normalization still applies after the keyword is stripped.
    out, _ = resolve("eth", "options", "1d")
    check("eth options 1d → ETH 24h", out == "ETH 24h", out)


def test_defaults():
    check("no args → BTC 8h", resolve()[0] == "BTC 8h")
    check("asset only → BTC 8h", resolve("btc")[0] == "BTC 8h")


# ── Dynamic-window parsing + preset gating (RECAP_PRINT_PLAN) ────────────────
# The old preset `case` silently defaulted any non-preset window (e.g. 3h) to
# 8h, so surface deltas were computed against the wrong window-open and the
# hot__recap_<win>.parquet read missed. Windows are now parsed generically.

def test_preset_window_plan():
    # Preset: SECS from the window, PRESET=1 (label only — same rolling-file path).
    check("8h → 28800s, preset", plan("btc", "8h") == ("BTC 8h 28800 1", 0))
    check("1h → 3600s, preset", plan("btc", "1h") == ("BTC 1h 3600 1", 0))


def test_dynamic_window_resolves_correctly():
    # The bug: 3h must resolve to 10800s (not the old 8h/28800 default), PRESET=0.
    check("3h → 10800s, non-preset", plan("btc", "3h") == ("BTC 3h 10800 0", 0))
    check("90m → 5400s, non-preset", plan("btc", "90m") == ("BTC 90m 5400 0", 0))
    check("2d → 172800s, non-preset", plan("eth", "2d") == ("ETH 2d 172800 0", 0))
    check("6h → 21600s, non-preset", plan("btc", "6h") == ("BTC 6h 21600 0", 0))


def test_1d_normalizes_to_preset():
    # 1d → 24h happens before parsing, so it stays on the fast preset path.
    check("1d → 24h, 86400s, preset", plan("btc", "1d") == ("BTC 24h 86400 1", 0))


# ── Surface-open source resolution (RECAP_PRINT_SOURCES) ────────────────────
# ΔATM/ΔRR/ΔFly need a window-open surface. Windows ≤1h read it from _hot only
# (VS_COLD "-"); >1h windows also target the cold hour-partition containing
# window-start. The bash date math must be UTC and zero-padded on both GNU and
# BSD date — a wrong partition path silently degrades every Δ column to n/a,
# which is exactly the bug that shipped when the cold store was empty.

COLD_FMT = ("s3://dt-paradigm-data/paradigm_data/v_vol_surface/"
            "base={a}/year={t.tm_year:04d}/month={t.tm_mon:02d}/day={t.tm_mday:02d}/"
            "hour={t.tm_hour:02d}/v_vol_surface.parquet")


def expect(asset, win, now_s, secs, cold):
    start = now_s - secs
    c = COLD_FMT.format(a=asset, t=time.gmtime(start)) if cold else "-"
    return f"{asset} {win} {start * 1000} {c}"


def test_sources_hot_only_up_to_1h():
    now = 1_784_536_200  # 2026-07-20 08:30:00 UTC
    check("30m stays on _hot", sources(now, "btc", "30m") == expect("BTC", "30m", now, 1800, False))
    check("1h stays on _hot", sources(now, "btc", "1h") == expect("BTC", "1h", now, 3600, False))


def test_sources_cold_partition_over_1h():
    now = 1_784_536_200  # 2026-07-20 08:30:00 UTC
    out = sources(now, "btc", "8h")
    check("8h resolves cold partition", out == expect("BTC", "8h", now, 28800, True), out)
    check("8h cold path zero-padded/UTC",
          "base=BTC/year=2026/month=07/day=20/hour=00/" in out, out)
    out = sources(now, "eth", "90m")
    check("90m (61–120min) also targets cold", out == expect("ETH", "90m", now, 5400, True), out)


def test_sources_day_boundary():
    # Window-start crosses midnight UTC: day/hour must roll back correctly.
    now = 1_784_514_600  # 2026-07-20 02:30:00 UTC
    out = sources(now, "btc", "8h")
    check("8h across midnight → day=19 hour=18",
          "year=2026/month=07/day=19/hour=18/" in out, out)
    check("8h across midnight full line", out == expect("BTC", "8h", now, 28800, True), out)


def test_bad_window_exits_2():
    for w in ("3x", "foo", "0h", "h", "-2h"):
        out, rc = plan("btc", w)
        check(f"bad window '{w}' exits 2", rc == 2, f"rc={rc}")
        check(f"bad window '{w}' names it", "bad window" in out, out)


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    print(f"Running {len(tests)} test functions...")
    for t in tests:
        t()
    print(f"\n{_passed} checks passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
