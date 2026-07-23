#!/usr/bin/env python3
"""Validate skill version bumps in a pull request.

For every ``skills/*/SKILL.md`` touched by the PR, compare the
``metadata.version`` at the merge base against the version at HEAD and enforce
the repository's version policy (see AGENTS.md):

  * A version may stay the same (draft / WIP edits do not require a bump).
  * When it changes it must be a single, clean, forward step of **exactly one**
    in exactly one component — major, minor, or patch:
        major:  X.Y.Z -> (X+1).0.0
        minor:  X.Y.Z -> X.(Y+1).0
        patch:  X.Y.Z -> X.Y.(Z+1)
  * A jump of two or more (e.g. 1.5 -> 1.7 or 1.5 -> 3.0), a multi-component
    change (1.5 -> 2.1), or a decrease (1.5 -> 1.4) is rejected.

Versions may be written with two components (``"1.5"``) or three (``"1.5.1"``);
they are normalised to three components (padding with zeros) before comparison,
so ``"1.5" -> "1.5.1"`` is a valid patch bump.

Base and head revisions are taken from the ``BASE_SHA`` / ``HEAD_SHA`` env vars
when set (as they are in CI); otherwise they default to ``origin/main`` and
``HEAD`` for local use.

Exit code 0 = all good, 1 = at least one violation, 2 = the tool itself failed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - CI installs pyyaml
    print("error: PyYAML is required (pip install pyyaml)", file=sys.stderr)
    sys.exit(2)


SKILL_GLOB = "skills/*/SKILL.md"


def run(*args: str) -> str:
    """Run a git command, returning stdout. Raises on non-zero exit."""
    return subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def git_show(rev: str, path: str) -> str | None:
    """Return the contents of *path* at *rev*, or None if it does not exist."""
    result = subprocess.run(
        ["git", "show", f"{rev}:{path}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def parse_version(content: str | None) -> str | None:
    """Extract metadata.version from a SKILL.md's YAML frontmatter."""
    if not content:
        return None
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return None
    metadata = fm.get("metadata") or {}
    version = metadata.get("version")
    return None if version is None else str(version).strip()


def normalize(version: str) -> tuple[int, int, int]:
    """Parse a version string into a (major, minor, patch) tuple.

    Accepts two- or three-component numeric versions and pads to three.
    Raises ValueError on anything else.
    """
    parts = version.split(".")
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"expected 1-3 numeric components, got '{version}'")
    nums = []
    for p in parts:
        if not p.isdigit():
            raise ValueError(f"non-numeric component in '{version}'")
        nums.append(int(p))
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)  # type: ignore[return-value]


def classify(old: tuple[int, int, int], new: tuple[int, int, int]) -> str:
    """Describe the transition from *old* to *new*.

    Returns one of: 'same', 'major', 'minor', 'patch' for allowed transitions,
    or an error message string prefixed with 'ERROR:' for disallowed ones.
    """
    if new == old:
        return "same"
    om, omi, op = old
    if new == (om + 1, 0, 0):
        return "major"
    if new == (om, omi + 1, 0):
        return "minor"
    if new == (om, omi, op + 1):
        return "patch"

    # Not an allowed single-step increment — explain why.
    if new < old:
        return "ERROR: version decreased"
    return (
        "ERROR: version must increase by exactly one component "
        "(major, minor, or patch) at a time"
    )


def changed_skill_files(base: str, head: str) -> list[str]:
    """SKILL.md files that differ between the merge base of base/head and head.

    Deleted files are excluded (--diff-filter=d). Uses three-dot semantics so
    the comparison is against the merge base, matching how PRs are reviewed.
    """
    out = run(
        "diff",
        "--name-only",
        "--diff-filter=d",
        f"{base}...{head}",
        "--",
        SKILL_GLOB,
    )
    return [line for line in out.splitlines() if line.strip()]


def main() -> int:
    base = os.environ.get("BASE_SHA", "origin/main")
    head = os.environ.get("HEAD_SHA", "HEAD")

    try:
        merge_base = run("merge-base", base, head).strip()
    except subprocess.CalledProcessError as exc:
        print(f"error: could not find merge base of {base} and {head}", file=sys.stderr)
        print(exc.stderr, file=sys.stderr)
        return 2

    try:
        files = changed_skill_files(base, head)
    except subprocess.CalledProcessError as exc:
        print(f"error: git diff failed\n{exc.stderr}", file=sys.stderr)
        return 2

    if not files:
        print("No SKILL.md files changed in this PR — version check skipped.")
        return 0

    failures: list[str] = []
    checked = 0

    for path in files:
        skill = Path(path).parent.name
        new_content = git_show(head, path)
        old_content = git_show(merge_base, path)

        new_version = parse_version(new_content)

        # Brand-new skill (did not exist at the merge base): nothing to compare.
        if old_content is None:
            print(f"{skill}: new skill (version {new_version or '—'}) — no prior version to compare.")
            continue

        old_version = parse_version(old_content)

        if old_version is None and new_version is None:
            # Neither side declares a version; not this check's concern.
            print(f"{skill}: no metadata.version on either side — skipped.")
            continue
        if new_version is None:
            failures.append(f"{skill}: metadata.version was removed (was '{old_version}')")
            continue
        if old_version is None:
            print(f"{skill}: version '{new_version}' added (was unversioned) — OK.")
            continue

        try:
            old_norm = normalize(old_version)
        except ValueError as exc:
            failures.append(f"{skill}: unparseable prior version '{old_version}' ({exc})")
            continue
        try:
            new_norm = normalize(new_version)
        except ValueError as exc:
            failures.append(f"{skill}: unparseable new version '{new_version}' ({exc})")
            continue

        checked += 1
        verdict = classify(old_norm, new_norm)

        if verdict.startswith("ERROR:"):
            reason = verdict[len("ERROR:"):].strip()
            failures.append(
                f"{skill}: {old_version} -> {new_version} is not allowed — {reason}. "
                f"Bump exactly one of major/minor/patch by 1 (e.g. "
                f"{old_norm[0]}.{old_norm[1]}.{old_norm[2] + 1}, "
                f"{old_norm[0]}.{old_norm[1] + 1}.0, or "
                f"{old_norm[0] + 1}.0.0)."
            )
        elif verdict == "same":
            print(f"{skill}: version unchanged at '{new_version}' — OK.")
        else:
            print(f"{skill}: {old_version} -> {new_version} ({verdict} bump) — OK.")

    print()
    if failures:
        print("Version check FAILED:")
        for msg in failures:
            print(f"  ✗ {msg}")
        return 1

    print(f"Version check passed ({checked} changed skill(s) with a version bump validated).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
