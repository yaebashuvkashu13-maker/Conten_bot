from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from highlight_scorer import HighlightMetrics  # noqa: E402
from intelliclip_scorer import enrich_highlight_metrics  # noqa: E402


def test_intelliclip_does_not_rescue_failed_rule_gate() -> None:
    metrics = HighlightMetrics(
        start=100.0,
        duration=10.0,
        profile="pubg",
        rule_pass=False,
        audio_pass=True,
        visual_pass=False,
        combined_score=0.0,
    )
    with patch("intelliclip_scorer.load_analysis") as la, patch(
        "intelliclip_scorer.window_bin_signals"
    ) as wbs, patch("intelliclip_scorer.intelliclip_score", return_value=0.9):
        la.return_value = {}
        wbs.return_value = {"hook_energy": 0.8, "visual_dynamics": 0.5}
        enrich_highlight_metrics(metrics, Path("x.mp4"), "pubg")
    assert metrics.rule_pass is False


def test_stage1_panns_prefilter_no_bypass(monkeypatch, tmp_path: Path) -> None:
    from highlight_scorer import stage1_panns_prefilter

    vod = tmp_path / "yt_test.mp4"
    vod.write_bytes(b"")
    labels = tmp_path / "labels.json"
    labels.write_text('{"videos":{}}', encoding="utf-8")
    monkeypatch.setattr("vod_owner_learning.owner_labels_path", lambda *_a, **_k: labels)
    monkeypatch.setattr("highlight_scorer._owner_labels_path", lambda _profile: labels)
    with patch("highlight_scorer.score_panns_audio", return_value={"panns_gun_max": 0.01}):
        kept = stage1_panns_prefilter(vod, [100.0, 200.0], "pubg")
    assert kept == []
