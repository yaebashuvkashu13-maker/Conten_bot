"""Owner-anchor montage seeding for shooter feeds."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from shooter_owner_montage import (  # noqa: E402
    owner_good_fight_peaks,
    owner_good_pool,
    peak_near_owner_good,
    soft_allow_owner_montage_part,
    vod_has_owner_montage_anchors,
)


def test_n97c_rejected_anchors_not_hinted(tmp_path: Path) -> None:
    """Owner rejected today's n97c склейка — do not boost those peaks."""
    vod = tmp_path / "yt_n97cHIR9Qow.mp4"
    vod.write_bytes(b"x")
    peaks = owner_good_fight_peaks("pubg", vod)
    assert 1845.0 not in peaks
    assert 2150.0 not in peaks
    assert 2470.0 not in peaks
    assert vod_has_owner_montage_anchors("pubg", vod, min_clips=3) is False
    assert owner_good_pool("pubg", vod) == []


def test_merge_owner_hints_boosts_nearby(tmp_path: Path) -> None:
    from shooter_owner_montage import merge_owner_hints_into_pool

    pool = [{"start": 1840.0, "score": 0.2, "highlight_metrics": {"clip_score": 0.2}}]
    hints = [{"start": 1845.0, "score": 0.55, "owner_anchor": True, "highlight_metrics": {"clip_score": 0.55}}]
    merged = merge_owner_hints_into_pool(pool, hints)
    assert len(merged) == 1
    assert merged[0]["owner_anchor"] is True
    assert float(merged[0]["score"]) > 0.2


def test_soft_allow_never_forgives_talk_or_loot(tmp_path: Path, monkeypatch) -> None:
    vod = tmp_path / "yt_goodFightABC.mp4"
    vod.write_bytes(b"x")
    monkeypatch.setenv("SHOOTER_VOD_MONTAGE_SOFT_GATE", "1")
    monkeypatch.setenv("SHOOTER_VOD_OWNER_ANCHOR_MONTAGE", "1")
    # Even with soft-gate env and gun metrics, talk/loot must stay rejected.
    metrics = {"gunfire_density": 0.09, "burst_ratio": 6.0, "panns_gun_max": 0.5}
    for reason in (
        "streamer_talk=rms0.0476:gun0.063",
        "loot_walk=density0.021",
        "run_fake_gun=motion0.1:gun0.04",
        "no_shots=density0.01:burst1.0:ownerx",
    ):
        ok, out = soft_allow_owner_montage_part(
            "pubg",
            vod,
            120.0,
            False,
            reason,
            montage_part=True,
            metrics=metrics,
        )
        assert ok is False, reason
        assert out == reason


def test_soft_allow_borderline_near_live_hint(tmp_path: Path, monkeypatch) -> None:
    """Borderline weak_shots near a non-rejected owner hint + gun evidence may pass."""
    from shooter_owner_montage import PUBG_BRAWL_ANCHORS_BY_VOD

    vod = tmp_path / "yt_liveHintVod99.mp4"
    vod.write_bytes(b"x")
    monkeypatch.setenv("SHOOTER_VOD_OWNER_ANCHOR_MONTAGE", "1")
    monkeypatch.setenv("SHOOTER_VOD_OWNER_ANCHOR_SOFT_ALLOW", "1")
    PUBG_BRAWL_ANCHORS_BY_VOD["liveHintVod99"] = [500.0]
    try:
        assert peak_near_owner_good("pubg", vod, 500.0)
        ok, reason = soft_allow_owner_montage_part(
            "pubg",
            vod,
            500.0,
            False,
            "weak_shots=gun0.06",
            montage_part=True,
            metrics={"gunfire_density": 0.06, "burst_ratio": 5.0, "panns_gun_max": 0.1},
        )
        assert ok is True
        assert "owner_hint_soft" in reason
    finally:
        PUBG_BRAWL_ANCHORS_BY_VOD.pop("liveHintVod99", None)


def test_soft_allow_still_blocks_owner_bad(tmp_path: Path) -> None:
    vod = tmp_path / "yt_n97cHIR9Qow.mp4"
    vod.write_bytes(b"x")
    ok, reason = soft_allow_owner_montage_part(
        "pubg", vod, 1845.0, False, "owner_bad_window"
    )
    assert ok is False
    assert reason == "owner_bad_window"


def test_soft_gate_env_cannot_ship_talk(tmp_path: Path, monkeypatch) -> None:
    vod = tmp_path / "yt_FpMs48XOnq0.mp4"
    vod.write_bytes(b"x")
    monkeypatch.setenv("SHOOTER_VOD_MONTAGE_SOFT_GATE", "1")
    ok, reason = soft_allow_owner_montage_part(
        "pubg",
        vod,
        230.0,
        False,
        "streamer_talk=rms0.0476:gun0.063",
        montage_part=True,
        metrics={"gunfire_density": 0.06, "burst_ratio": 3.8, "panns_gun_max": 0.05},
    )
    assert ok is False
    assert "streamer_talk" in reason
