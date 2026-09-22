"""The one way skill scripts make HTTP and S3 calls (enforced by
.github/scripts/check_http_access.py). Header names are case-insensitive and
change casing in transit, so every header map returned here — and every S3
object's user Metadata — is a `Headers`: any casing finds the same entry."""

import json
import urllib.error
import urllib.request
from collections.abc import MutableMapping
from urllib.parse import urlencode


class Headers(MutableMapping):
    """Case-insensitive; iterates, and so copies, with lowercase names."""

    def __init__(self, items=()):
        self._items = {}
        self.update(items)

    def __getitem__(self, name):
        return self._items[name.lower()]

    def __setitem__(self, name, value):
        self._items[name.lower()] = value

    def __delitem__(self, name):
        del self._items[name.lower()]

    def __contains__(self, name):
        return isinstance(name, str) and name.lower() in self._items

    def __iter__(self):
        return iter(self._items)

    def __len__(self):
        return len(self._items)

    def __repr__(self):
        return f"Headers({self._items!r})"


class Response:
    def __init__(self, url, status, headers, body):
        self.url, self.status, self.headers, self.body = url, status, headers, body

    def json(self):
        return json.loads(self.body)


class HTTPStatusError(Exception):
    """A 4xx/5xx; `.response` carries its status, Headers and body."""

    def __init__(self, response):
        super().__init__(f"HTTP {response.status} from {response.url}")
        self.response = response


def _headers(message):
    # A repeated field combines comma-separated (RFC 9110 §5.3).
    merged = Headers()
    for name in message.keys():
        merged[name] = ", ".join(message.get_all(name))
    return merged


def get(url, params=None, headers=None, timeout=15):
    if params:
        url = f"{url}?{urlencode(params)}"
    request = urllib.request.Request(url, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as r:
            return Response(url, r.status, _headers(r.headers), r.read())
    except urllib.error.HTTPError as e:
        raise HTTPStatusError(Response(url, e.code, _headers(e.headers), e.read())) from e


def s3_client(*, endpoint_url, **kwargs):
    # endpoint_url is required: AWS_ENDPOINT_URL or a profile would otherwise redirect reads.
    import boto3

    client = boto3.client("s3", endpoint_url=endpoint_url, **kwargs)
    client.meta.events.register("after-call.s3", _case_insensitive_metadata)
    return client


def _case_insensitive_metadata(parsed, **_):
    # botocore keys Metadata by each x-amz-meta-* name exactly as it arrived.
    if isinstance(parsed.get("Metadata"), dict):
        parsed["Metadata"] = Headers(parsed["Metadata"])
