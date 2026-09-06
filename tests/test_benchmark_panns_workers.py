from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from benchmark_panns_workers import _offsets  # noqa: E402


def test_worker_benchmark_offsets_span_vod() -> None:
    offsets = _offsets(5400.0, 60.0, 8.0)
    assert offsets[0] >= 15
    assert offsets[-1] > 5200
    assert len(offsets) >= 80
