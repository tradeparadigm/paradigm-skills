#!/usr/bin/env python3
"""Unit tests for the eval harness itself. Run: python3 tests/test_run_evals.py

The grader is a model, so how its answer is read decides every score. It used
to be read from prose, and a grader that reasoned and then wrote `FINAL: PASS`
was scored FAIL — 17 of one run's 21 recorded failures, which is how the same
skills flipped between 73% and 87% across runs of identical code. The verdict
is a tool call now; these pin that nothing reads prose again.
"""
import os
import pathlib
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import run_evals  # noqa: E402
from run_evals import GraderRefused, VERDICT_TOOL, grade_assertion, run_skill  # noqa: E402

_p = _f = 0


def ok(cond, msg):
    global _p, _f
    if cond:
        _p += 1
    else:
        _f += 1
        print(f"  FAIL: {msg}")


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Grader:
    """A stand-in client returning one scripted response."""

    def __init__(self, *blocks):
        self.reply = _Block(content=list(blocks))
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return self.reply


def _tool_use(**inp):
    return _Block(type="tool_use", name="record_verdict", input=inp)


def _text(body):
    return _Block(type="text", text=body)


def _graded(*blocks):
    return grade_assertion(_Grader(*blocks), "m", "the offset matches", "a response", "a prompt")


# ── the verdict is the tool's field, whatever prose came with it ──────────────
ok(_graded(_tool_use(verdict="pass"))["passed"] is True, "a recorded pass passes")
ok(_graded(_tool_use(verdict="fail", reason="no size"))["passed"] is False,
   "a recorded fail fails")
ok(_graded(_text("Let me weigh the [Fair] row."), _tool_use(verdict="pass"))["passed"] is True,
   "reasoning alongside the call does not matter")
ok(_graded(_text("FAIL: the header disagrees"), _tool_use(verdict="pass"))["passed"] is True,
   "prose contradicting the call does not matter — the call is the verdict")
ok(_graded(_tool_use(verdict="PASS"))["passed"] is True, "case does not matter")
ok(_graded(_tool_use(verdict="fail", reason="no size"))["verdict"] == "FAIL: no size",
   "the reason is kept for the report")

# ── a grader that records nothing is loud ─────────────────────────────────────
refused = False
try:
    _graded(_text("I cannot judge this without the [Fair] row."))
except GraderRefused as err:
    refused = "cannot judge" in str(err)
ok(refused, "no tool call raises, quoting what it said instead")

for bad in ({"verdict": "maybe"}, {"verdict": ""}, {}):
    raised = False
    try:
        _graded(_tool_use(**bad))
    except GraderRefused:
        raised = True
    ok(raised, f"a verdict of {bad.get('verdict', '(absent)')!r} raises")

ok(_graded(_tool_use(verdict="pass"), _tool_use(verdict="fail")).get("passed") is True,
   "the first recorded verdict is the verdict")

# ── the call the harness makes ────────────────────────────────────────────────
grader = _Grader(_tool_use(verdict="pass"))
grade_assertion(grader, "m", "the offset matches", "a response", "a prompt")
sent = grader.calls[0]
ok(sent["tool_choice"] == {"type": "tool", "name": "record_verdict"},
   "the grader is forced to record a verdict rather than asked to")
ok(sent["tools"] == [VERDICT_TOOL], "the verdict tool is the only one offered")
ok(VERDICT_TOOL["input_schema"]["properties"]["verdict"]["enum"] == ["pass", "fail"],
   "the schema admits nothing but pass or fail")
ok("the offset matches" in sent["messages"][0]["content"], "the assertion reaches the grader")

# ── an unscoreable skill is an error, and an error is never a pass ───────────
def _refusing_run(*a, **k):
    raise GraderRefused("grader recorded no verdict for 'the offset matches'")


_saved = run_evals._run_skill_cases
run_evals._run_skill_cases = _refusing_run
try:
    _result = run_skill(None, "block-analyst", "m", "m", True, False, False)
finally:
    run_evals._run_skill_cases = _saved

ok(_result["status"] == "error", "a refusal marks the skill unscoreable")
ok("no verdict" in _result["reason"], "and says why")
ok("score" not in _result, "an unscoreable skill has no score to average or compare")

# `main` exits non-zero on any errored skill — the threshold check below it
# skips errors, so without this an unscoreable run would read as a pass.
_exit_block = pathlib.Path(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "run_evals.py")
).read_text()
_errored_at = _exit_block.index('errored = [r for r in all_results if r.get("status") == "error"]')
_threshold_at = _exit_block.index("if args.fail_below is not None:")
ok(_errored_at < _threshold_at, "the error exit comes before the threshold check")
ok("sys.exit(1)" in _exit_block[_errored_at:_threshold_at], "and it exits non-zero")

print(f"\n{_p} passed, {_f} failed")
sys.exit(1 if _f else 0)
