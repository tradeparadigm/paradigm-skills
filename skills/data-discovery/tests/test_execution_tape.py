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
    def __init__(self, *, age=0, missing=False, watermark_lag=0, watermark=True,
                 build_start_days=31, duplicate_ids=False, null_id=False):
        self.calls = []
        self.age = age
        self.missing = missing
        # How far the observed source watermark trails `now` (minutes), and
        # whether the object carries the field at all (pre-watermark objects).
        self.watermark_lag = watermark_lag
        self.watermark = watermark
        # How far back the producer claims its build window reached, and whether
        # the legs it wrote carry usable ids — each has a guard of its own.
        self.build_start_days = build_start_days
        self.duplicate_ids = duplicate_ids
        self.null_id = null_id

    def get_object(self, Bucket, Key):
        self.calls.append(Key)
        if self.missing:
            raise RuntimeError("AccessDenied")
        buf = io.BytesIO()
        pl.DataFrame(
            {
                "trade_id": (["leg-0"] * 150 if self.duplicate_ids
                             else [None] + [f"leg-{i}" for i in range(1, 150)] if self.null_id
                             else [f"leg-{i}" for i in range(150)]),
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
                "build_window_start_ms": str(
                    int((NOW - timedelta(days=self.build_start_days)).timestamp() * 1000)
                ),
                "build_window_end_ms": str(int(NOW.timestamp() * 1000)),
                **(
                    {
                        "source_watermark_ms": str(
                            int(
                                (NOW - timedelta(minutes=self.watermark_lag)).timestamp()
                                * 1000
                            )
                        )
                    }
                    if self.watermark
                    else {}
                ),
            },
        }


def test_all_legs_exact_path_and_id():
    s3 = S3()
    result = reader.read_executions(
        NOW - timedelta(hours=2), NOW, rfq_id="r_test", s3=s3, now=NOW
    )
    assert len(result["rows"]) == 150
    assert result["build_window_end_ms"] == int(NOW.timestamp() * 1000)
    assert s3.calls == [
        "paradigm_trade_tape/year=2026/month=09/day=08/paradigm_trade_tape__20260908.parquet"
    ]
    assert (
        reader.read_executions(
            NOW - timedelta(hours=2), NOW, rfq_id="GRFQ-r_test", s3=s3, now=NOW
        )["rows"]
        == []
    )


@pytest.mark.parametrize("s3", [S3(age=46), S3(missing=True)])
def test_unavailable_is_not_empty(s3):
    with pytest.raises(RuntimeError):
        reader.read_executions(NOW - timedelta(hours=2), NOW, s3=s3, now=NOW)


def test_publication_gate_tolerates_the_producers_own_worst_case():
    """15-minute cadence + a 10-minute deadline must not read as stale.

    The previous 20-minute rule rejected healthy partitions after a single
    slow tick, which is what motivated reading around the guard.
    """
    result = reader.read_executions(
        NOW - timedelta(hours=2), NOW, s3=S3(age=26), now=NOW
    )
    assert len(result["rows"]) == 150


def test_lagging_watermark_reports_a_gap_instead_of_raising():
    """The hourly upstream sync makes a short shortfall the normal case."""
    result = reader.read_executions(
        NOW - timedelta(hours=2), NOW, s3=S3(watermark_lag=40), now=NOW
    )
    assert result["coverage_complete"] is False
    assert result["coverage_shortfall_seconds"] == 40 * 60
    assert result["coverage_end_ms"] == int((NOW - timedelta(minutes=40)).timestamp() * 1000)
    assert "NOT as zero activity" in result["coverage_note"]
    # The build clock is NOT the coverage bound: it is still fully current.
    assert result["build_window_end_ms"] > result["coverage_end_ms"]
    assert len(result["rows"]) == 150


def test_full_coverage_is_reported_positively():
    result = reader.read_executions(
        NOW - timedelta(hours=2), NOW, s3=S3(), now=NOW
    )
    assert result["coverage_complete"] is True
    assert result["coverage_shortfall_seconds"] == 0
    assert result["source_watermark_ms"] == int(NOW.timestamp() * 1000)


def test_missing_watermark_field_is_unknown_never_covered():
    """A pre-watermark object must not fall back to the build clock."""
    result = reader.read_executions(
        NOW - timedelta(hours=2), NOW, s3=S3(watermark=False), now=NOW
    )
    assert result["source_watermark_ms"] is None
    assert result["coverage_end_ms"] is None
    assert result["coverage_complete"] is False
    assert result["coverage_shortfall_seconds"] is None
    assert "UNKNOWN" in result["coverage_note"]


def test_one_unknown_day_does_not_average_against_a_healthy_day():
    """Mixed generations during publication/replication must fail closed."""

    class MixedGenerations(S3):
        def get_object(self, Bucket, Key):
            # Only the OLDER day carries a watermark; today's predates it.
            self.watermark = Key.endswith("20260907.parquet")
            obj = super().get_object(Bucket=Bucket, Key=Key)
            # Distinct legs per day, or the reader's own grain gate fires
            # before the coverage logic under test is reached.
            day = Key[-16:-8]
            buf = io.BytesIO()
            pl.DataFrame(
                {
                    "trade_id": [f"leg-{day}"],
                    "rfq_id": ["DRFQv2-r_test"],
                    "traded_at": [int((NOW - timedelta(hours=2)).timestamp() * 1000)],
                }
            ).write_parquet(buf)
            obj["Body"] = io.BytesIO(buf.getvalue())
            return obj

    result = reader.read_executions(
        NOW - timedelta(days=1), NOW, s3=MixedGenerations(), now=NOW
    )
    assert result["coverage_complete"] is False
    assert result["coverage_end_ms"] is None


def test_weakest_watermark_bounds_a_multi_day_read():
    """Two generations mid-publication: the OLDER watermark wins, never the mean."""

    class TwoGenerations(S3):
        def get_object(self, Bucket, Key):
            self.watermark_lag = 90 if Key.endswith("20260907.parquet") else 0
            obj = super().get_object(Bucket=Bucket, Key=Key)
            day = Key[-16:-8]
            buf = io.BytesIO()
            pl.DataFrame(
                {
                    "trade_id": [f"leg-{day}"],
                    "rfq_id": ["DRFQv2-r_test"],
                    "traded_at": [int((NOW - timedelta(hours=2)).timestamp() * 1000)],
                }
            ).write_parquet(buf)
            obj["Body"] = io.BytesIO(buf.getvalue())
            return obj

    result = reader.read_executions(
        NOW - timedelta(days=1), NOW, s3=TwoGenerations(), now=NOW
    )
    assert result["source_watermark_ms"] == int((NOW - timedelta(minutes=90)).timestamp() * 1000)
    assert result["coverage_shortfall_seconds"] == 90 * 60
    assert result["coverage_complete"] is False


def test_outside_retention_fails_before_read():
    s3 = S3()
    with pytest.raises(ValueError):
        reader.read_executions(NOW - timedelta(days=32), NOW, s3=s3, now=NOW)
    assert s3.calls == []


def test_old_metadata_contract_is_rejected():
    class OldMetadata(S3):
        def get_object(self, **kwargs):
            obj = super().get_object(**kwargs)
            obj["Metadata"]["coverage_start_ms"] = obj["Metadata"].pop("build_window_start_ms")
            obj["Metadata"]["coverage_end_ms"] = obj["Metadata"].pop("build_window_end_ms")
            return obj

    # RuntimeError naming the object, not a bare KeyError: in production this
    # reached the reader as the recap line "Paradigm executions unavailable —
    # 'generated_at_ms'", which names neither the object nor the problem.
    with pytest.raises(RuntimeError, match="build_window_start_ms"):
        reader.read_executions(NOW - timedelta(hours=2), NOW, s3=OldMetadata(), now=NOW)


def test_a_partition_without_its_provenance_names_the_object():
    """Seen in production on 2026-09-18: one object in the window carried no
    generated_at_ms, and the whole Paradigm tape was reported unavailable with
    the key name as the entire explanation."""
    class NoProvenance(S3):
        def get_object(self, **kwargs):
            obj = super().get_object(**kwargs)
            obj["Metadata"].pop("generated_at_ms")
            return obj

    with pytest.raises(RuntimeError) as caught:
        reader.read_executions(NOW - timedelta(hours=2), NOW, s3=NoProvenance(), now=NOW)
    message = str(caught.value)
    assert "generated_at_ms" in message
    assert "paradigm_trade_tape" in message, message
    assert "freshness" in message, message


def test_ambiguous_bare_id_fails_but_qualified_id_preserves_legs():
    class Namespaces(S3):
        def get_object(self, **kwargs):
            obj = super().get_object(**kwargs)
            buf = io.BytesIO()
            pl.DataFrame({
                'trade_id': ['a', 'b'],
                'rfq_id': ['DRFQv2-r_AbC', 'GRFQ-r_AbC'],
                'traded_at': [int((NOW - timedelta(hours=1)).timestamp() * 1000)] * 2,
            }).write_parquet(buf)
            obj['Body'] = io.BytesIO(buf.getvalue())
            return obj

    with pytest.raises(reader.AmbiguousRfqError):
        reader.read_executions(NOW - timedelta(hours=2), NOW, rfq_id='r_AbC',
                               s3=Namespaces(), now=NOW)
    result = reader.read_executions(NOW - timedelta(hours=2), NOW,
                                    rfq_id='DRFQv2-r_AbC', s3=Namespaces(), now=NOW)
    assert [row['trade_id'] for row in result['rows']] == ['a']


def test_a_future_dated_publication_is_refused():
    """A negative age means the producer's clock ran ahead of ours. The bound is
    two-sided for that reason; a one-sided one would accept it silently."""
    with pytest.raises(RuntimeError, match="stale or future-dated"):
        reader.read_executions(NOW - timedelta(hours=2), NOW, rfq_id="r_test",
                               s3=S3(age=-5), now=NOW)


def test_a_partition_that_starts_after_the_request_is_refused():
    """Covering only part of the window and saying nothing would understate the
    flow rather than fail, which is the whole reason this raises."""
    with pytest.raises(RuntimeError, match="does not cover requested start"):
        reader.read_executions(NOW - timedelta(days=10), NOW, rfq_id="r_test",
                               s3=S3(build_start_days=2), now=NOW)


@pytest.mark.parametrize("stub", [S3(duplicate_ids=True), S3(null_id=True)],
                         ids=["duplicate", "null"])
def test_unusable_trade_ids_are_refused(stub):
    """trade_id is the execution grain and tape_block_key's fallback, so a
    duplicate or null one silently collapses legs into a single block."""
    with pytest.raises(RuntimeError, match="duplicate/null trade IDs"):
        reader.read_executions(NOW - timedelta(hours=2), NOW, rfq_id="r_test",
                               s3=stub, now=NOW)
