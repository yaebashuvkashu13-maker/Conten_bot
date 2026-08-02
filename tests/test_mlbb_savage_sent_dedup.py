#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_savage_title_rescan import (  # noqa: E402
    _already_sent_near,
    _load_sent_registry,
    _mark_sent,
    _save_sent_registry,
)


def test_sent_registry_dedup(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MLBB_SAVAGE_SENT_REGISTRY", str(tmp_path / "sent.json"))
    monkeypatch.setenv("MLBB_SAVAGE_SEND_DEDUP_SEC", "45")
    reg = _load_sent_registry()
    assert reg["clips"] == []
    _mark_sent(reg, video_id="abc", sec=12.6, label="savage", source="ref", file="a.mp4")
    assert _already_sent_near(reg, "abc", 12.6) is True
    assert _already_sent_near(reg, "abc", 21.0) is True  # same fight window
    assert _already_sent_near(reg, "abc", 90.0) is False
    assert _already_sent_near(reg, "other", 12.6) is False
    # reload from disk
    reg2 = _load_sent_registry()
    assert _already_sent_near(reg2, "abc", 15.0) is True
    _save_sent_registry(reg2)
