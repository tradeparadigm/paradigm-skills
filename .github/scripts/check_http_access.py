#!/usr/bin/env python3
"""Every HTTP and S3 call in a skill script goes through
skills/data-discovery/scripts/http_client.py, whose header maps are
case-insensitive. Fails on any other HTTP client, boto3 client or shell fetch.

Usage: check_http_access.py [skills_dir]   (default: skills/)
"""

import ast
import re
import sys
from pathlib import Path

CLIENT = Path("data-discovery/scripts/http_client.py")
HTTP_MODULES = {"urllib.request", "urllib3", "http.client", "requests", "httpx", "aiohttp"}
FIX = "use get() / s3_client() from skills/data-discovery/scripts/http_client.py"


def _boto3_client(call):
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr in ("client", "resource")):
        return False
    owner = func.value
    if isinstance(owner, ast.Call):  # boto3.Session(...).client(...)
        owner = owner.func.value if isinstance(owner.func, ast.Attribute) else owner.func
    return isinstance(owner, ast.Name) and owner.id in ("boto3", "Session")


def python_violations(path, source):
    for node in ast.walk(ast.parse(source, filename=str(path))):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
        elif isinstance(node, ast.Call) and _boto3_client(node):
            yield node.lineno, "boto3 client built directly"
            continue
        else:
            continue
        for name in names:
            if name in HTTP_MODULES or name.split(".")[0] in HTTP_MODULES - {"urllib.request", "http.client"}:
                yield node.lineno, f"imports {name}"
                break


def shell_violations(source):
    for lineno, line in enumerate(source.splitlines(), 1):
        if re.search(r"(^|[\s;|&(])(curl|wget)\s", line.split("#", 1)[0]):
            yield lineno, "fetches over HTTP from shell"


def violations(skills):
    for path in sorted(skills.glob("*/scripts/**/*")):
        rel = path.relative_to(skills)
        if rel == CLIENT or not path.is_file():
            continue
        if path.suffix == ".py":
            found = python_violations(rel, path.read_text())
        elif path.suffix == ".sh":
            found = shell_violations(path.read_text())
        else:
            continue
        for lineno, what in found:
            yield f"skills/{rel}:{lineno}: {what} — {FIX}"


def main():
    skills = Path(sys.argv[1] if len(sys.argv) > 1 else "skills")
    found = list(violations(skills))
    for line in found:
        print(line)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
