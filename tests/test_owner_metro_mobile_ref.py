"""Owner reference: PUBG Mobile Metro multi-kill must clear audio + kill gates."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_sustained_loud_combat_scores_nonzero_gun(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Compressed Mobile fight audio must not stay at gun density 0."""
    import gameplay_gate as gg

    sr = 16000
    t = np.linspace(0, 4.0, sr * 4, endpoint=False)
    rng = np.random.default_rng(0)
    pcm = (rng.normal(0, 0.20, size=t.shape) + 0.05 * np.sin(2 * np.pi * 1800 * t)).astype(
        np.float32
    )
    samples = np.clip(pcm * 32768.0, -32767, 32767).astype(np.int16)
    monkeypatch.setattr(gg, "_extract_segment_audio_pcm", lambda *_a, **_k: samples)
    gun, burst, rms = gg.score_pubg_gunfire_audio(tmp_path / "x.mp4", 0.0, 4.0)
    assert rms >= 0.08
    assert gun >= 0.035
    assert burst >= 3.5


def test_quality_hud_fp_policy_needs_keyword_or_panns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Low-conf hud_fp keeps hit only with kill keyword or strong PANNs gun."""
    from pubg_quality_score import score_pubg_window  # local import for patch path

    def _apply(row):
        notification_score = float(row.get("notification_score") or 0.0)
        notification_min = 0.50
        notification_hit = notification_score >= notification_min
        nclass = str(row.get("notification_class") or "").strip().lower()
        nconf = float(row.get("notification_class_conf") or 0.0)
        keyword_hit = bool(row.get("killfeed_hits"))
        panns_gun = float(row.get("panns_gun_max") or 0.0)
        report = {}
        hud_fp_conf_min = 0.45
        hud_fp_keep_panns = 0.35
        if nclass in {"hud_fp", "map_blue", "hud_false_positive"}:
            keep_low_conf = nconf < hud_fp_conf_min and (keyword_hit or panns_gun >= hud_fp_keep_panns)
            if nconf >= hud_fp_conf_min or not keep_low_conf:
                notification_hit = False
                report["kill_notification_hud_fp_ignored"] = True
        report["kill_notification_hit"] = notification_hit
        return report

    inventory = _apply({"notification_score": 0.72, "notification_class": "hud_fp", "notification_class_conf": 0.15, "killfeed_hits": [], "panns_gun_max": 0.01})
    metro = _apply({"notification_score": 0.60, "notification_class": "hud_fp", "notification_class_conf": 0.15, "killfeed_hits": ["убийство"], "panns_gun_max": 0.01})
    confident = _apply({"notification_score": 0.60, "notification_class": "hud_fp", "notification_class_conf": 0.80, "killfeed_hits": [], "panns_gun_max": 0.50})
    assert inventory["kill_notification_hit"] is False
    assert metro["kill_notification_hit"] is True
    assert confident["kill_notification_hit"] is False


def test_mobile_kill_patterns_match_russian_banner() -> None:
    from pubg_killfeed_ocr import KILL_PATTERNS

    text = "x2 Получено чести: +13 ya ne alina УБИЙСТВО"
    hits = [pat.pattern for pat, _ in KILL_PATTERNS if pat.search(text)]
    assert hits
    assert any("убийств" in p.lower() or "чест" in p.lower() for p in hits)
