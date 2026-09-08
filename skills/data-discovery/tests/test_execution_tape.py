"""Offline read-path checks with real typed Parquet and S3 metadata."""

import importlib.util
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

spec = importlib.util.spec_from_file_location(
    "execution_tape", Path(__file__).resolve().parents[1] / "scripts/execution_tape.py"
)
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)
NOW = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)


class S3:
    def __init__(self, *, age=0, missing=False):
        self.calls = []
        self.age = age
        self.missing = missing

    def get_object(self, Bucket, Key):
        self.calls.append(Key)
        if self.missing:
            raise RuntimeError("AccessDenied")
        buf = io.BytesIO()
        pl.DataFrame(
            {
                "trade_id": [f"leg-{i}" for i in range(150)],
                "rfq_id": ["DRFQv2-r_test"] * 150,
                "traded_at": [int((NOW - timedelta(hours=1)).timestamp() * 1000)] * 150,
            }
        ).write_parquet(buf)
        return {
            "Body": io.BytesIO(buf.getvalue()),
            "Metadata": {
                "generated_at_ms": str(
                    int((NOW - timedelta(minutes=self.age)).timestamp() * 1000)
                ),
                "coverage_start_ms": str(
                    int((NOW - timedelta(days=31)).timestamp() * 1000)
                ),
                "coverage_end_ms": str(int(NOW.timestamp() * 1000)),
            },
        }


def test_all_legs_exact_path_and_id():
    s3 = S3()
    result = reader.read_executions(
        NOW - timedelta(hours=2), NOW, rfq_id="r_test", s3=s3, now=NOW
    )
    assert len(result["rows"]) == 150
    assert s3.calls == [
        "paradigm_trade_tape/year=2026/month=09/day=08/paradigm_trade_tape__20260908.parquet"
    ]
    assert (
        reader.read_executions(
            NOW - timedelta(hours=2), NOW, rfq_id="GRFQ-r_test", s3=s3, now=NOW
        )["rows"]
        == []
    )


@pytest.mark.parametrize("s3", [S3(age=21), S3(missing=True)])
def test_unavailable_is_not_empty(s3):
    with pytest.raises(RuntimeError):
        reader.read_executions(NOW - timedelta(hours=2), NOW, s3=s3, now=NOW)


def test_outside_retention_fails_before_read():
    s3 = S3()
    with pytest.raises(ValueError):
        reader.read_executions(NOW - timedelta(days=32), NOW, s3=s3, now=NOW)
    assert s3.calls == []
