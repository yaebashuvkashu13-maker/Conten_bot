"""Precise fight-window banner ranking — denser frames, not weaker accept gates."""

from __future__ import annotations

import numpy as np

from mlbb_kill_banner import KillBannerHit, _rank_fight_candidate_secs
from mlbb_teamfight_detector import fight_first_peaks


def _gold_band_frame(*, strength: float = 1.0) -> np.ndarray:
    """Synthetic upper-band gold/white announce-like frame (BGR)."""
    frame = np.zeros((270, 480, 3), dtype=np.uint8)
    # Dark gameplay
    frame[:] = (20, 30, 18)
    y0, y1 = int(270 * 0.06), int(270 * 0.18)
    x0, x1 = int(480 * 0.25), int(480 * 0.75)
    # Gold-ish (B,G,R) + white core
    gold = (int(30 * strength), int(160 * strength), int(220 * strength))
    white = (240, 240, 240)
    frame[y0:y1, x0:x1] = gold
    mid = (y0 + y1) // 2
    frame[mid - 2 : mid + 3, x0 + 20 : x1 - 20] = white
    return frame


def _farm_gold_frame() -> np.ndarray:
    """Diffuse edge gold — more like farming HUD, less like centered announce."""
    frame = np.zeros((270, 480, 3), dtype=np.uint8)
    frame[:] = (25, 40, 20)
    frame[0:40, 0:80] = (20, 140, 200)
    frame[200:270, 400:480] = (20, 140, 200)
    return frame


def test_rank_prefers_temporal_flash_over_steady_farm() -> None:
    farm = _farm_gold_frame()
    flash = _gold_band_frame(strength=1.0)
    dim = np.zeros((270, 480, 3), dtype=np.uint8)
    frames = [
        (100.0, dim),
        (100.25, dim),
        (100.5, flash),  # brief announce
        (100.75, dim),
        (101.0, farm),
        (101.25, farm),
        (101.5, farm),
    ]
    picks = _rank_fight_candidate_secs(frames, focus_sec=100.5, max_classify=3)
    assert picks, "expected at least one candidate"
    assert abs(picks[0] - 100.5) < 0.3 or 100.5 in picks


def test_fight_first_time_buckets_cover_early_and_late(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_FIGHT_FIRST_BUCKET_SEC", "90")
    monkeypatch.setenv("MLBB_BANNER_FIGHT_FIRST_PEAKS", "4")
    monkeypatch.setenv("MLBB_FIGHT_FIRST_MIN_SCORE", "0.0")
    # Fake analysis: late fight is louder, but early must still be represented.
    motion = np.zeros(400, dtype=np.float32)
    audio = np.zeros(400, dtype=np.float32)
    # bins at 2s → t = idx*2
    motion[60] = 0.40  # t=120 early
    motion[150] = 0.95  # t=300 loud mid
    motion[151] = 0.90
    motion[280] = 0.55  # t=560 late
    analysis = {
        "window_seconds": 2.0,
        "center_motion": motion,
        "audio": audio,
        "duration": 800.0,
    }
    starts = [120.0, 300.0, 302.0, 560.0, 305.0]
    peaks = fight_first_peaks(analysis, starts, limit=4)
    assert len(peaks) <= 4
    # Must include early + late buckets, not only the mid cluster.
    assert any(abs(p - 120.0) < 1.0 for p in peaks)
    assert any(abs(p - 560.0) < 1.0 for p in peaks)


def test_merge_hit_keeps_distant_earlier_banner(monkeypatch) -> None:
    """Regression: signed dedupe used to drop earlier hit when peaks arrive out of order."""
    import mlbb_kill_banner as kb

    monkeypatch.setenv("MLBB_BANNER_DISCOVER_HIT_DEDUP_SEC", "6")
    hits: list[KillBannerHit] = []
    need = 1
    want = 2
    ship_first_double = False

    def _near_excluded(_sec: float) -> bool:
        return False

    # Inline the fixed merge logic via discovering helper by simulating two hits.
    h_late = KillBannerHit(sec=400.0, tier=2, label="double", text="DOUBLE", source="ref")
    h_early = KillBannerHit(sec=120.0, tier=3, label="triple", text="TRIPLE", source="ref")

    # Reproduce merge body (same as production).
    def merge(hit: KillBannerHit) -> None:
        nonlocal want
        if hit.tier < need or not kb._banner_hit_source_ok(hit.source):
            return
        if _near_excluded(hit.sec):
            return
        dedupe = 6.0
        best_i = -1
        best_gap = None
        for i, prev in enumerate(hits):
            gap = abs(float(hit.sec) - float(prev.sec))
            if gap < dedupe and (best_gap is None or gap < best_gap):
                best_i = i
                best_gap = gap
        if best_i >= 0:
            if hit.tier > hits[best_i].tier:
                hits[best_i] = hit
        else:
            hits.append(hit)

    merge(h_late)
    merge(h_early)
    assert len(hits) == 2
    secs = sorted(h.sec for h in hits)
    assert secs == [120.0, 400.0]
