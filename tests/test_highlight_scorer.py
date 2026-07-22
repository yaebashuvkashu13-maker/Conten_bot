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
    PANN_GUN_INFERENCE_FLOOR,
    _owner_anchor_starts,
    audio_passes_shooter,
    calibrated_pann_gun_min,
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
    talk, reason3 = audio_passes_shooter(
        {"panns_gun_max": 0.30, "panns_speech": 5.0, "panns_music": 0.1}
    )
    assert talk is False
    assert "speech_dominant" in reason3


def test_shooter_rule_delegates_to_combat_gate() -> None:
    pytest.importorskip("cv2")
    m = HighlightMetrics(
        start=0,
        duration=10,
        profile="pubg",
        audio_pass=True,
        visual_pass=True,
        clip_score=0.12,
        panns_gun_max=0.22,
        center_motion=0.13,
        panns_gun_threshold=0.18,
    )
    with patch("pubg_combat_gate.pubg_passes_combat_gate", return_value=(True, "combat_ok", {})):
        ok, reason = rule_gate("pubg", m, video_path=Path("x.mp4"), start_sec=0, duration_sec=10)
    assert ok is True
    assert reason == "combat_ok"

    with patch("pubg_combat_gate.pubg_passes_combat_gate", return_value=(False, "no_shots", {})):
        ok2, reason2 = rule_gate("pubg", m, video_path=Path("x.mp4"), start_sec=0, duration_sec=10)
    assert ok2 is False
    assert reason2 == "no_shots"


def test_accept_combat_ok_relaxes_hook_never_raises(monkeypatch) -> None:
    from highlight_scorer import _accept_highlight_candidate

    monkeypatch.setenv("VIRAL_SEGMENT_HOOK_MIN", "0.14")
    # Misconfigured high combat min must not tighten the bar past segment min.
    monkeypatch.setenv("VIRAL_COMBAT_HOOK_MIN", "0.20")
    m = HighlightMetrics(
        start=204.4,
        duration=10,
        profile="pubg",
        audio_pass=True,
        visual_pass=True,
        rule_pass=True,
        hook_score=0.095,
        clip_score=0.227,
        panns_gun_max=0.538,
        pass_reason="combat_ok=gun0.538:burst3.628",
    )
    assert _accept_highlight_candidate(Path("x.mp4"), 204.4, m, "pubg") is True

    m_low_panns = HighlightMetrics(
        start=10.0,
        duration=10,
        profile="pubg",
        audio_pass=True,
        visual_pass=True,
        rule_pass=True,
        hook_score=0.095,
        clip_score=0.10,
        panns_gun_max=0.10,
        pass_reason="ok",
    )
    assert _accept_highlight_candidate(Path("x.mp4"), 10.0, m_low_panns, "pubg") is False


def test_shooter_rule_requires_video_path() -> None:
    m = HighlightMetrics(
        start=0,
        duration=10,
        profile="pubg",
        audio_pass=True,
        visual_pass=True,
    )
    ok, reason = rule_gate("pubg", m)
    assert ok is False
    assert reason == "combat_gate_no_video"


def test_calibrated_pann_gun_min_has_inference_floor(monkeypatch, tmp_path: Path) -> None:
    labels = tmp_path / "labels.json"
    labels.write_text(
        '{"videos":{"testvid":[{"time_sec":515,"label":"good"},{"time_sec":600,"label":"bad"}]}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "highlight_scorer._owner_labels_path",
        lambda _profile: labels,
    )
    with patch("highlight_scorer.score_panns_audio") as panns_mock:
        panns_mock.side_effect = [
            {"panns_gun_max": 0.07},
            {"panns_gun_max": 0.05},
        ]
        floor = calibrated_pann_gun_min(tmp_path / "yt_testvid.mp4", "pubg")
    assert floor >= PANN_GUN_INFERENCE_FLOOR


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
    pytest.importorskip("cv2")
    labels = tmp_path / "labels.json"
    labels.write_text(
        '{"videos":{"testvid":[{"time_sec":515,"label":"good"}]}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("HIGHLIGHT_USE_OWNER_ANCHORS", "0")
    monkeypatch.setenv("INTELLICLIP_STAGE1", "0")
    monkeypatch.setattr(
        "highlight_scorer._owner_labels_path",
        lambda _profile: labels,
    )
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
