#!/usr/bin/env python3
"""Unit tests for the eval harness itself. Run: python3 tests/test_run_evals.py

The grader is a model, so how its answer is read decides the score. These pin
the reading, including the shape that made block-analyst's score flip between
73% and 87% across runs of identical code.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from run_evals import verdict_passed  # noqa: E402

_p = _f = 0


def ok(cond, msg):
    global _p, _f
    if cond:
        _p += 1
    else:
        _f += 1
        print(f"  FAIL: {msg}")


ok(verdict_passed("PASS") is True, "a bare PASS passes")
ok(verdict_passed("FAIL: the offset is missing") is False, "a bare FAIL fails")
ok(verdict_passed("\n\nPASS\n") is True, "leading blank lines are skipped")
ok(verdict_passed("The response quotes both figures.\nPASS") is True,
   "reasoning before the verdict is fine")
ok(verdict_passed("PASS\nThe header and the [Fair] row agree.") is True,
   "reasoning after the verdict is fine")

# The observed self-correction, verbatim in shape: committed to FAIL, reasoned,
# then corrected itself. Read from the first line this scored FAIL.
ok(verdict_passed(
    "FAIL: the header says -6 bps but the [Fair] row's math disagrees\n"
    "Wait — reconsider: header states \"-6 bps below mark\" and the [Fair] row "
    "states \"-6 bps below mark.\" Both agree.\n"
    "PASS"
) is True, "a grader that corrects itself to PASS passes")

ok(verdict_passed(
    "PASS\nWait, let me reconsider — the response never states the size.\n"
    "FAIL: the structure size is absent"
) is False, "a grader that corrects itself to FAIL fails")

# A grader that reasoned past its budget and never answered has not passed.
ok(verdict_passed("Let me work through the [Fair] row line by line.") is False,
   "no verdict line at all is a failure")
ok(verdict_passed("") is False, "an empty answer is a failure")
ok(verdict_passed("passed: it reports both figures") is True,
   "case and punctuation do not matter")

print(f"\n{_p} passed, {_f} failed")
sys.exit(1 if _f else 0)
