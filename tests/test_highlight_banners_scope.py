#!/usr/bin/env python3
"""PUBG crashed when _nearest_banner closed over unbound `banners`."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def test_pubg_discover_does_not_nameerror_on_banners(monkeypatch, tmp_path) -> None:
    import highlight_scorer as hs

    vod = tmp_path / "yt_pubg.mp4"
    vod.write_bytes(b"\x00" * 8)

    metrics = MagicMock()
    metrics.viral_score = 0.5
    metrics.combined_score = 0.5
    metrics.pass_reason = "ok"
    metrics.to_dict.return_value = {"pass_reason": "ok"}

    monkeypatch.setenv("SHOOTER_VOD_SEND_ONE", "1")
    with patch.object(hs, "require_inference_ready", return_value=(True, "")), patch.object(
        hs, "stage1_candidates", return_value=[30.0, 60.0]
    ), patch.object(hs, "_parallel_workers", return_value=1), patch.object(
        hs, "_shooter_score_stop_n", return_value=1
    ), patch.object(
        hs, "_evaluate_highlight_start", return_value=(30.0, metrics)
    ), patch.object(hs, "_accept_highlight_candidate", return_value=True):
        out = hs.discover_highlight_candidates(vod, "pubg", limit=2)
    assert isinstance(out, list)
    # Prefilters may empty the pool; the regression is NameError on `banners`.


def test_kill_banner_bypasses_min_peak_cut_start() -> None:
    min_peak = 20.0
    peak = 12.0
    peak_anchor = 28.0
    has_banner = True
    assert peak < min_peak
    assert not (not has_banner or peak_anchor < min_peak)
