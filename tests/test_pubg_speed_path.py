"""Speed-path regressions for PUBG: early-stop, zero-pool cooldown, kill-discover skip."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import highlight_scorer as hs  # noqa: E402
from vod_scan_state import should_skip_vod_rescan  # noqa: E402


def test_parallel_workers_hard_capped(monkeypatch) -> None:
    monkeypatch.setenv("HIGHLIGHT_PARALLEL_WORKERS", "99")
    assert hs._parallel_workers() <= 6


def test_shooter_early_stop_default(monkeypatch) -> None:
    monkeypatch.delenv("SHOOTER_VOD_SCORE_EARLY_STOP", raising=False)
    assert hs._shooter_score_early_stop("pubg") == 2
    monkeypatch.setenv("SHOOTER_VOD_SCORE_EARLY_STOP", "0")
    assert hs._shooter_score_early_stop("pubg") == 0
    assert hs._shooter_score_early_stop("mobile_legends") == 0


def test_zero_pool_cooldown_skips_rescan(monkeypatch) -> None:
    monkeypatch.setenv("SHOOTER_VOD_ZERO_POOL_COOLDOWN_SEC", "1800")
    monkeypatch.setenv("SHOOTER_VOD_SCAN_COOLDOWN_SEC", "7200")
    entry = {
        "exhausted": False,
        "last_scan_at": time.time() - 60,
        "last_scan_sent": 0,
        "last_pool_peaks": [],
        "reject_reason": "no_combat_peaks",
    }
    assert should_skip_vod_rescan(entry, game="pubg") is True


def test_kill_discover_skipped_when_seeds(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PUBG_VOD_KILL_DISCOVER", "1")
    monkeypatch.setenv("PUBG_KILL_DISCOVER_SKIP_IF_SEEDS", "1")
    monkeypatch.setenv("HIGHLIGHT_ALLOW_SEED_STARTS", "1")
    monkeypatch.setenv("HIGHLIGHT_SEED_STARTS", "240")
    monkeypatch.setenv("SHOOTER_VOD_SCORE_MAX", "2")
    monkeypatch.setenv("SHOOTER_VOD_SCORE_EARLY_STOP", "0")
    monkeypatch.setenv("HIGHLIGHT_PARALLEL_WORKERS", "1")
    vod = tmp_path / "yt_seed.mp4"
    vod.write_bytes(b"")

    metrics = MagicMock()
    metrics.rule_pass = True
    metrics.visual_pass = True
    metrics.pass_reason = "combat_ok"
    metrics.viral_score = 0.2
    metrics.combined_score = 0.2
    metrics.to_dict.return_value = {"viral_score": 0.2}

    with patch.object(hs, "require_inference_ready", return_value=(True, "ok")):
        with patch.object(hs, "stage1_candidates", return_value=[240.0, 300.0]):
            with patch.object(hs, "stage1_panns_prefilter", side_effect=lambda *_a, **_k: list(_a[1])):
                with patch("pubg_kill_banner.discover_vod_kill_moments") as discover:
                    with patch.object(
                        hs,
                        "_evaluate_highlight_start",
                        return_value=(240.0, metrics),
                    ):
                        with patch.object(hs, "_accept_highlight_candidate", return_value=True):
                            out = hs.discover_highlight_candidates(vod, "pubg")
    discover.assert_not_called()
    assert out
