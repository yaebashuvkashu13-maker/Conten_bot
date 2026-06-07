from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from highlight_scorer import (  # noqa: E402
    HighlightMetrics,
    _owner_anchor_starts,
    audio_passes_shooter,
    owner_anchors_enabled,
    rule_gate,
    score_panns_audio,
    stage1_candidates,
)


def test_audio_passes_shooter_requires_panns_gun() -> None:
    ok, reason = audio_passes_shooter(
        {"panns_gun_max": 0.30, "panns_speech": 0.1, "panns_music": 0.1}
    )
    assert ok is True
    bad, reason2 = audio_passes_shooter(
        {"panns_gun_max": 0.10, "panns_speech": 0.1, "panns_music": 0.1}
    )
    assert bad is False
    assert "panns_gun_low" in reason2


def test_shooter_rule_requires_audio_and_clip() -> None:
    m = HighlightMetrics(
        start=0,
        duration=10,
        profile="pubg",
        audio_pass=True,
        visual_pass=True,
        clip_score=0.08,
        panns_gun_max=0.3,
    )
    ok, reason = rule_gate("pubg", m)
    assert ok is True
    m.clip_score = 0.01
    ok2, reason2 = rule_gate("pubg", m)
    assert ok2 is False
    assert "clip_low" in reason2


def test_score_panns_audio_mock() -> None:
    fake_scores = np.zeros(527, dtype=np.float32)
    fake_scores[427] = 0.42
    fake_scores[428] = 0.31
    with patch("highlight_scorer._panns_tagger") as tagger:
        inst = MagicMock()
        inst.inference.return_value = (np.array([fake_scores]), np.zeros((1, 2048)))
        tagger.return_value = inst
        with patch("highlight_scorer._extract_audio_32k", return_value=np.ones(32000, dtype=np.float32)):
            out = score_panns_audio(Path("x.mp4"), 0, 10)
    assert out["panns_gunshot"] == pytest.approx(0.42)
    assert out["panns_gun_max"] >= 0.31


def test_owner_anchors_not_in_stage1_by_default(monkeypatch, tmp_path: Path) -> None:
    labels = tmp_path / "labels.json"
    labels.write_text(
        '{"videos":{"testvid":[{"time_sec":515,"label":"good"}]}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("HIGHLIGHT_USE_OWNER_ANCHORS", "0")
    monkeypatch.setenv("INTELLICLIP_STAGE1", "0")
    monkeypatch.setattr("highlight_scorer.OWNER_LABELS", labels)
    monkeypatch.setattr(
        "smart_video_editor.analyze_video",
        lambda _p: {
            "window_seconds": 2.0,
            "duration": 600.0,
            "center_motion": [0.0] * 300,
            "gunfire": [0.0] * 300,
            "audio": [0.0] * 300,
        },
    )
    vod = tmp_path / "yt_testvid.mp4"
    vod.write_bytes(b"")
    starts = stage1_candidates(vod, "pubg")
    assert 510.0 not in starts
    assert not owner_anchors_enabled()
