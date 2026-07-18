"""Strong PANNs combat should not die on tiny viral hook scores."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from highlight_scorer import _accept_highlight_candidate  # noqa: E402


def test_strong_panns_skips_low_hook(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VIRAL_SEGMENT_HOOK_MIN", "0.10")
    monkeypatch.setenv("VIRAL_COMBAT_HOOK_MIN", "0.04")
    monkeypatch.setenv("VIRAL_COMBAT_PANN_HOOK_BYPASS", "0.18")
    monkeypatch.setenv("VIRAL_COMBAT_PANN_HOOK_SKIP", "0.40")
    vod = tmp_path / "yt_hookskip.mp4"
    vod.write_bytes(b"x")
    metrics = SimpleNamespace(
        rule_pass=True,
        visual_pass=True,
        panns_gun_max=0.517,
        clip_score=0.24,
        hook_score=0.024,
        viral_score=0.004,
        heatmap_intensity=0.0,
        pass_reason="combat_ok=gun0.517:burst5.05",
    )
    assert _accept_highlight_candidate(vod, 90.0, metrics, "pubg") is True
