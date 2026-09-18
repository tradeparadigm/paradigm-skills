#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3>=1.35", "polars>=1.0", "pyarrow>=17"]
# ///
"""Resolve one RFQ to the two CSVs analyze.py renders from.

Replaces the DuckDB scan of hot__paradigm_trade_tape_30d that analyze.sh used
to build inline, along with its STS bootstrap: the shared reader in
data-discovery resolves credentials through the chain, reads the exact daily
partitions, and refuses a stale or duplicate-ID publication instead of
returning rows from it.

ONE unfiltered 30-day read, filtered twice in memory:
  fill  — the target RFQ's legs, matched on the exact namespace set
  hist  — every OTHER block sharing the fill's PRODUCT and normalised
          DESCRIPTION, which is what recurrence counts. Filtering the read by
          rfq_id instead would leave hist holding only the fill's own block and
          recurrence would read 1 for every trade.
"""
import argparse
import csv
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "data-discovery" / "scripts"))
from execution_tape import AmbiguousRfqError, read_executions  # noqa: E402

HORIZON = dt.timedelta(days=30)
FILL_COLUMNS = ("PRODUCT", "DESCRIPTION", "QTY", "PRICE", "REF_PRICE", "SIDE",
                "QUOTE_CURRENCY", "RFQ_ID", "TRADE_ID", "BLOCK_TRADE_ID")
HIST_COLUMNS = ("DATE", "TIME", "PRODUCT", "DESCRIPTION", "QTY", "PRICE",
                "REF_PRICE", "SIDE", "BLOCK_TRADE_ID")


def core_id(value: str) -> str:
    return value.removeprefix("DRFQv2-").removeprefix("GRFQ-")


def quote_currency(row: dict) -> str:
    """DERIVED, not renamed — the tape has no premium-currency column.

    `asset` is the UNDERLYING. Aliasing it onto QUOTE_CURRENCY fed the wrong
    value to analyze_core.offset, whose `quote not in _STABLE_QUOTES and
    abs(ref) < 1` branch then reports a coin-priced fill as a USD one.
    """
    product = (row.get("product") or "")
    venue = product.split(" - ")[1].strip().upper() if " - " in product else ""
    asset = (row.get("asset") or "").upper()
    name = row.get("instrument_name")
    if (venue == "DBT" and asset in ("BTC", "ETH")
            and name is not None and "USDC" not in name.upper()):
        return asset
    return "USDC"


def normalised(description: str) -> str:
    return (description or "").upper().replace(" ", "")


def shaped(rows: list[dict]) -> list[dict]:
    """Tape rows in the column names analyze.py reads.

    row_type is still a live classification column and the reader applies no
    filter of its own, so keep the predicate the SQL had: any other row carries
    an rfq_id too, and would land in both fill and hist and inflate recurrence.
    """
    out = []
    for row in rows:
        if row.get("row_type") not in (None, "paradigm_trade"):
            continue
        stamp = str(row.get("traded_at_iso") or "")
        date, _, time = stamp.replace("T", " ").partition(" ")
        out.append({
            "DATE": date, "TIME": time[:8],
            "PRODUCT": row.get("product"), "DESCRIPTION": row.get("description"),
            "QTY": row.get("quantity"), "PRICE": row.get("trade_price"),
            "REF_PRICE": row.get("mark_price"), "SIDE": row.get("taker_side"),
            "QUOTE_CURRENCY": quote_currency(row), "RFQ_ID": row.get("rfq_id"),
            "TRADE_ID": row.get("trade_id"), "BLOCK_TRADE_ID": row.get("block_trade_id"),
            "_DESC_N": normalised(row.get("description")),
        })
    return out


def write(path: Path, columns: tuple[str, ...], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def collect(rfq_id: str, out_dir: Path, *, now=None, s3=None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    result = read_executions(now - HORIZON, now, s3=s3, now=now)
    rows = shaped(result["rows"])

    core = core_id(rfq_id)
    wanted = {core, f"DRFQv2-{core}", f"GRFQ-{core}"}
    fill = [r for r in rows if r["RFQ_ID"] in wanted]
    if not fill:
        # The reader reports the hourly-sync tail as incomplete rather than
        # raising. Saying "not on the tape" for a trade inside that tail is the
        # substitution its contract forbids — absence of evidence read as
        # evidence of absence.
        return {"fill": 0, "hist": 0, "blocks": 0,
                "coverage_complete": bool(result.get("coverage_complete")),
                "coverage_note": result.get("coverage_note")}
    namespaces = {r["RFQ_ID"] for r in fill}
    if len(namespaces) > 1:
        raise AmbiguousRfqError(
            f"{core} exists in {len(namespaces)} namespaces ({', '.join(sorted(namespaces))}) — "
            "re-run with the exact DRFQv2- or GRFQ- prefixed id")

    # Recurrence is about OTHER blocks of the same structure, so match on the
    # structure, not on the RFQ.
    structures = {(r["PRODUCT"], r["_DESC_N"]) for r in fill}
    hist = sorted((r for r in rows if (r["PRODUCT"], r["_DESC_N"]) in structures),
                  key=lambda r: (r["DATE"], r["TIME"]), reverse=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    write(out_dir / "fill.csv", FILL_COLUMNS, fill)
    write(out_dir / "hist.csv", HIST_COLUMNS, hist)
    return {"fill": len(fill), "hist": len(hist),
            "blocks": len({r["BLOCK_TRADE_ID"] for r in hist if r["BLOCK_TRADE_ID"]})}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("rfq_id")
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()
    try:
        counts = collect(args.rfq_id, Path(args.out_dir))
    except AmbiguousRfqError as exc:
        print(f"analyze: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        # The tape is the only source; a reader refusal is a DEAD PIPELINE, not
        # an unknown RFQ. analyze.py's missing-fill message says "not on the
        # Paradigm tape", which would blame the trade for a producer outage.
        print(f"analyze: execution tape unavailable — {exc}", file=sys.stderr)
        return 4
    if not counts["fill"]:
        if not counts.get("coverage_complete", True):
            print(f"analyze: {args.rfq_id} not found, but the tape's coverage is incomplete "
                  f"({counts.get('coverage_note') or 'no watermark'}) — absent from the read "
                  "is not absent from the market", file=sys.stderr)
            return 6
        print(f"analyze: {args.rfq_id} not found on the execution tape", file=sys.stderr)
        return 5
    print(f"fill={counts['fill']} hist={counts['hist']} blocks={counts['blocks']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
