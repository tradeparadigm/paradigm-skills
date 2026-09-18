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
# CONCURRENCY caps the raw bodies in flight; BATCH caps the parsed tables held
# before each intermediate concat. Neither bounds the result: every object's
# rows end up resident at once, so peak grows with the window. The concat
# itself is free — pa.concat_tables is zero-copy when the schemas match.
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


def _concat(tables: list[pa.Table]) -> pa.Table:
    """Merge objects whose schemas drifted, the way union_by_name=true does.

    permissive promotion widens numerics and fills missing columns, but it
    raises on a column that is int64 in one object and string in another —
    real over a 30-day window, and DuckDB would have read both as VARCHAR.
    Fall back to exactly that: cast the column to string everywhere.
    """
    tables = [t for t in tables if t.num_rows or t.num_columns]
    if not tables:
        return pa.table({})
    try:
        return pa.concat_tables(tables, promote_options="permissive")
    except pa.ArrowTypeError:
        pass
    conflicted = set()
    seen: dict[str, pa.DataType] = {}
    for table in tables:
        for field in table.schema:
            previous = seen.setdefault(field.name, field.type)
            if previous != field.type and field.name not in conflicted:
                try:
                    pa.unify_schemas([pa.schema([pa.field(field.name, previous)]),
                                      pa.schema([field])], promote_options="permissive")
                except pa.ArrowTypeError:
                    conflicted.add(field.name)
    cast = []
    for table in tables:
        for name in conflicted:
            if name in table.column_names:
                index = table.schema.get_field_index(name)
                table = table.set_column(index, pa.field(name, pa.string()),
                                         table.column(name).cast(pa.string()))
        cast.append(table)
    return pa.concat_tables(cast, promote_options="permissive")


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
        batches.append(_concat(tables))
        del tables
    return _concat(batches)


def read_objects(paths: list[str], columns: tuple[str, ...] | None = None) -> pa.Table:
    """Read `s3://` object paths into one Arrow table."""
    keys = [(path.split(f"{BUCKET}/", 1)[1], path) for path in paths]
    return asyncio.run(_gather(keys, columns))
