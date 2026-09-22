#!/usr/bin/env python3
"""Each forbidden shape is caught, and the sanctioned ones pass."""

import subprocess
import sys
import tempfile
from pathlib import Path

CHECK = Path(__file__).with_name("check_http_access.py")

CASES = {
    "import urllib.request": True,
    "from urllib.request import urlopen": True,
    "from urllib import request": True,
    "import requests": True,
    "import httpx": True,
    "from aiohttp import ClientSession": True,
    "import http.client": True,
    "import boto3\ns3 = boto3.client('s3', endpoint_url='x')": True,
    "import boto3\ns3 = boto3.Session().client('s3', endpoint_url='x')": True,
    "import boto3\ns3 = boto3.resource('s3', endpoint_url='x')": True,
    "from http_client import get, s3_client": False,
    "import boto3\ncreds = boto3.Session().get_credentials()": False,
    "from urllib.parse import urlencode": False,
    "import json": False,
}
SHELL = {"curl -si https://x | grep -i foo": True, "  wget https://x": True,
         "uv run scripts/x.py  # not curl here": False, "echo curling": False}

failed = 0
for body, bad in [*((b, v) for b, v in CASES.items()),
                  *((b, v) for b, v in SHELL.items())]:
    with tempfile.TemporaryDirectory() as d:
        scripts = Path(d, "some-skill", "scripts")
        scripts.mkdir(parents=True)
        (scripts / ("x.py" if body in CASES else "x.sh")).write_text(body + "\n")
        client = Path(d, "data-discovery", "scripts")
        client.mkdir(parents=True)
        (client / "http_client.py").write_text("import urllib.request\nimport boto3\nboto3.client('s3')\n")
        rc = subprocess.run([sys.executable, str(CHECK), d], capture_output=True).returncode
    ok = (rc == 1) == bad
    failed += not ok
    print(f"  {'✓' if ok else '✗'} {'caught' if bad else 'allowed'}: {body.splitlines()[-1]}")
print(f"{len(CASES) + len(SHELL) - failed} passed, {failed} failed")
sys.exit(1 if failed else 0)
