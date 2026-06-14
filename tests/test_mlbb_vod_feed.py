from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from mlbb_vod_segment_feed import _vod_unreliable  # noqa: E402


def test_vod_unreliable_blocked(monkeypatch, tmp_path: Path) -> None:
    data_root = tmp_path / "mlbb"
    data_root.mkdir()
    blocked = data_root / "blocked_vods.json"
    blocked.write_text('{"vods": ["qa2iNyoPO2Q"]}', encoding="utf-8")
    monkeypatch.setenv("MLBB_DATA_ROOT", str(data_root))
    monkeypatch.setenv("MLBB_BLOCKED_VODS", str(blocked))

    vod = tmp_path / "yt_qa2iNyoPO2Q.mp4"
    vod.write_bytes(b"fake")
    assert _vod_unreliable(vod, dur=1000.0) is True
