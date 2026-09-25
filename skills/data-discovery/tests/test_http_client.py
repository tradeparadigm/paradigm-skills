"""http_client against a real local server: names arrive in whatever casing the
wire carried, and every lookup must hit regardless."""

import http.server
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import http_client  # noqa: E402

# Casings seen in the wild: Go-canonical, all-lower, and an exchange's own mix.
WIRE = [("X-Amz-Meta-Generated_at_ms", "1790044201499"),
        ("x-amz-meta-row_count", "4211"),
        ("Timenow", "1790051596708"),
        ("x-mbx-used-weight-1m", "2"),
        ("X-Ratelimit-Remaining", "9"),
        ("Link", "<a>"), ("Link", "<b>")]


@pytest.fixture
def server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            status = 429 if self.path.startswith("/limited") else 200
            self.send_response(status)
            for name, value in WIRE:
                self.send_header(name, value)
            body = b'{"ok": true}'
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_headers_is_case_insensitive_and_iterates_lowercase():
    h = http_client.Headers({"Timenow": "1", "x-mbx-used-weight-1m": "2"})
    assert h["TIMENOW"] == h["timenow"] == h.get("TimeNow") == "1"
    assert "X-MBX-USED-WEIGHT-1M" in h
    assert h.get("absent", 0) == 0
    assert dict(h) == {"timenow": "1", "x-mbx-used-weight-1m": "2"}


@pytest.mark.parametrize("name", ["Timenow", "timenow", "TIMENOW"])
def test_get_finds_a_header_by_any_casing(server, name):
    r = http_client.get(f"{server}/ok", {"q": "1"})
    assert r.status == 200 and r.json() == {"ok": True}
    assert r.headers[name] == "1790051596708"
    assert r.headers.get("X-MBX-USED-WEIGHT-1M", 0) == "2"
    assert r.headers["link"] == "<a>, <b>"


def test_an_error_status_still_carries_case_insensitive_headers(server):
    with pytest.raises(http_client.HTTPStatusError) as caught:
        http_client.get(f"{server}/limited")
    assert caught.value.response.status == 429
    assert caught.value.response.headers["x-ratelimit-remaining"] == "9"


@pytest.mark.parametrize("name", ["generated_at_ms", "Generated_at_ms", "GENERATED_AT_MS"])
def test_s3_metadata_is_found_by_any_casing(server, name):
    """botocore builds Metadata from each x-amz-meta-* name exactly as it
    arrived; the shared client must make that casing irrelevant."""
    pytest.importorskip("boto3")
    from botocore import UNSIGNED
    from botocore.config import Config

    s3 = http_client.s3_client(
        region_name="us-east-1", endpoint_url=server,
        config=Config(signature_version=UNSIGNED, s3={"addressing_style": "path"}))
    md = s3.get_object(Bucket="b", Key="k")["Metadata"]
    assert md[name] == "1790044201499"
    assert md.get("ROW_COUNT") == "4211"
