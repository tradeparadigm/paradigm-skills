#!/usr/bin/env python3
"""Fetch many small partition objects concurrently and hand back one Arrow table.

DuckDB's httpfs reader caps in-flight requests at its thread count, so a window
made of thousands of ~6KB objects is bound by how many can be in the air rather
than by CPU or bandwidth. obstore signs and fetches outside the GIL, which lifts
that ceiling from tens to hundreds.

Measured in the pod against the same file sets, tuned DuckDB vs this:
297 files 1.51s -> 0.65s, 1,912 files 6.98s -> 1.38s, 8,117 files 29.48s -> 4.82s.
boto3's async client was tried first and is 2.2x SLOWER than DuckDB: its
per-request Python work (SigV4, hooks, serializers) serialises on the event loop
at ~11.5ms each, which no amount of concurrency hides.
"""

from __future__ import annotations

import asyncio

import boto3
import obstore
import pyarrow as pa
import pyarrow.parquet as pq
from obstore.store import S3Store

BUCKET = "dt-exchange-venue-data"
# Pinned for the same reason as the DuckDB secret and the boto3 clients: the
# enclave's egress allowlist is exact-match and cannot follow a 307 from the
# global host.
S3_ENDPOINT = "https://s3.ap-northeast-1.amazonaws.com"
CONCURRENCY = 512
# Objects are resolved before they are parsed, so a batch bounds how many raw
# bodies are alive at once. Memory stays flat as the window grows.
BATCH = 2048


def _store() -> S3Store:
    """Resolve credentials through boto3 so IRSA, env and profile all still work."""
    credentials = boto3.Session().get_credentials().get_frozen_credentials()
    return S3Store(BUCKET, region="ap-northeast-1", endpoint=S3_ENDPOINT,
                   access_key_id=credentials.access_key,
                   secret_access_key=credentials.secret_key,
                   session_token=credentials.token)


def _read(body: bytes, path: str, columns: tuple[str, ...] | None) -> pa.Table:
    handle = pq.ParquetFile(pa.BufferReader(body))
    if columns is None:
        table = handle.read()
    else:
        # A column absent from one object must not fail the window; concat
        # promotes it to nulls, as union_by_name=true does in DuckDB.
        table = handle.read(columns=[c for c in columns
                                     if c in handle.schema_arrow.names])
    # read_parquet(filename=true) synthesises this, and the trade query selects
    # it as source_path.
    return table.append_column(
        "filename", pa.array([path] * table.num_rows, pa.string()))


async def _gather(keys: list[tuple[str, str]],
                  columns: tuple[str, ...] | None) -> pa.Table:
    store = _store()
    limit = asyncio.Semaphore(CONCURRENCY)

    async def one(item: tuple[str, str]) -> pa.Table:
        key, path = item
        async with limit:
            response = await obstore.get_async(store, key)
            body = bytes(await response.bytes_async())
        return _read(body, path, columns)

    batches = []
    for start in range(0, len(keys), BATCH):
        tables = await asyncio.gather(*(one(k) for k in keys[start:start + BATCH]))
        batches.append(pa.concat_tables(tables, promote_options="permissive"))
        del tables
    return pa.concat_tables(batches, promote_options="permissive")


def read_objects(paths: list[str], columns: tuple[str, ...] | None = None) -> pa.Table:
    """Read `s3://` object paths into one Arrow table."""
    keys = [(path.split(f"{BUCKET}/", 1)[1], path) for path in paths]
    return asyncio.run(_gather(keys, columns))
