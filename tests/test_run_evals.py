#!/usr/bin/env python3
"""Unit tests for the eval harness itself. Run: python3 tests/test_run_evals.py

The grader is a model, so how its answer is read decides the score. These pin
the reading, including the shape that made block-analyst's score flip between
73% and 87% across runs of identical code.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from run_evals import _has_verdict, grade_assertion, verdict_passed  # noqa: E402

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
ok(verdict_passed("pass") is True, "case does not matter")
ok(verdict_passed("**PASS**") is True, "markdown around the verdict is fine")

# The shapes a grader actually used, counted from one CI run: 17 of that run's
# 21 recorded failures were graders answering `FINAL: PASS`, scored as failures
# because the line did not START with the word.
ok(verdict_passed("The header and the [Fair] row agree.\n\nFINAL: PASS") is True,
   "FINAL: PASS is a pass")
ok(verdict_passed("Both figures match.\nFINAL PASS") is True, "FINAL PASS is a pass")
ok(verdict_passed("Verdict: FAIL: the size is absent") is False,
   "a labelled FAIL is a failure")

# Prose is not a verdict: the word has to stand alone, or a grader narrating
# "the response passed the first half" decides the score.
ok(verdict_passed("The response passed on greeks but invented an IV.") is False,
   "prose containing the word is not a verdict")
ok(verdict_passed("PASSED: both figures are reported") is True, "PASSED is a verdict")
ok(verdict_passed("FAILED: the size is absent") is False, "FAILED is a verdict")
# The word has to BE the verdict, not start one: a grader whose last line reads
# "Passes the offset check" has commented, not answered.
ok(verdict_passed("Passes the offset check but not the size one") is False,
   "a word merely beginning with the verdict is not one")
ok(verdict_passed("It passes the first check.\nFAIL: it invents live bid/ask") is False,
   "the real verdict wins over prose above it")

# ── a grader that never answers is asked once more, for the verdict alone ─────
class _Say:
    """A stand-in client: each call returns the next scripted answer."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = []
        self.messages = self

    def create(self, model, max_tokens, messages):
        self.asked.append(messages[0]["content"])

        class _Block:
            text = self.answers.pop(0)

        class _Reply:
            content = [_Block()]

        return _Reply()


ok(_has_verdict("Let me think about the [Fair] row.") is False, "no verdict line")
ok(_has_verdict("reasoning\nFINAL: PASS") is True, "a labelled verdict counts")

_client = _Say("I need to weigh the header against the [Fair] row, and", "PASS")
_graded = grade_assertion(_client, "m", "the offset matches", "a response", "a prompt")
ok(_graded["passed"] is True, "a grader that ran long is re-asked and its answer used")
ok(len(_client.asked) == 2, "re-asked exactly once")
ok("No reasoning." in _client.asked[1], "the second ask is for the verdict alone")
ok("[re-asked for the verdict alone]" in _graded["verdict"],
   "the record shows both answers")

_client = _Say("reasoning first\nFINAL: PASS")
_graded = grade_assertion(_client, "m", "the offset matches", "a response", "a prompt")
ok(len(_client.asked) == 1, "a grader that answered is not re-asked")

print(f"\n{_p} passed, {_f} failed")
sys.exit(1 if _f else 0)
