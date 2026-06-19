#!/usr/bin/env python3
"""Quick self-test for picked→delivery delay analysis (synthetic rows)."""

from __future__ import annotations

import csv
import tempfile
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from research_picked_delivery_analysis import analyze_rows, format_report


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sample.csv"
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f,
                fieldnames=["order_id", "store", "courier_id", "Picked_date", "дата начала доставки"],
            )
            w.writeheader()
            w.writerow(
                {
                    "order_id": "1",
                    "store": "StoreA",
                    "courier_id": "C1",
                    "Picked_date": "2026-06-19 10:00:00",
                    "дата начала доставки": "2026-06-19 10:12:00",
                }
            )
            w.writerow(
                {
                    "order_id": "2",
                    "store": "StoreA",
                    "courier_id": "C2",
                    "Picked_date": "2026-06-19 10:00:00",
                    "дата начала доставки": "2026-06-19 10:03:00",
                }
            )
        headers = list(csv.DictReader(path.open(encoding="utf-8")).fieldnames or [])
        rows = list(csv.DictReader(path.open(encoding="utf-8")))
        summary = analyze_rows(headers, rows)
        assert summary["rows_analyzed"] == 2
        assert summary["over_counts"][10] == 1
        print(format_report(path, summary))
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
