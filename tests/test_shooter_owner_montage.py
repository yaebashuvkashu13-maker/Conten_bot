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


def test_n97c_brawl_anchors(tmp_path: Path) -> None:
    vod = tmp_path / "yt_n97cHIR9Qow.mp4"
    vod.write_bytes(b"x")
    peaks = owner_good_fight_peaks("pubg", vod)
    assert 1845.0 in peaks
    assert 2150.0 in peaks
    assert 2470.0 in peaks
    assert 2005.0 not in peaks  # sniper skip
    assert vod_has_owner_montage_anchors("pubg", vod, min_clips=3)
    pool = owner_good_pool("pubg", vod)
    assert len(pool) >= 3
    assert all(c.get("owner_anchor") for c in pool)


def test_merge_owner_hints_boosts_nearby(tmp_path: Path) -> None:
    from shooter_owner_montage import merge_owner_hints_into_pool

    pool = [{"start": 1840.0, "score": 0.2, "highlight_metrics": {"clip_score": 0.2}}]
    hints = [{"start": 1845.0, "score": 0.55, "owner_anchor": True, "highlight_metrics": {"clip_score": 0.55}}]
    merged = merge_owner_hints_into_pool(pool, hints)
    assert len(merged) == 1
    assert merged[0]["owner_anchor"] is True
    assert float(merged[0]["score"]) > 0.2


def test_soft_allow_owner_near_anchor(tmp_path: Path) -> None:
    vod = tmp_path / "yt_n97cHIR9Qow.mp4"
    vod.write_bytes(b"x")
    assert peak_near_owner_good("pubg", vod, 1845.0)
    ok, reason = soft_allow_owner_montage_part(
        "pubg", vod, 1845.0, False, "loot_walk=density0.04"
    )
    assert ok is True
    assert "owner_hint" in reason
    ok2, reason2 = soft_allow_owner_montage_part(
        "pubg", vod, 100.0, False, "loot_walk=density0.04"
    )
    assert ok2 is False
    assert reason2.startswith("loot_walk")


def test_soft_allow_still_blocks_owner_bad(tmp_path: Path) -> None:
    vod = tmp_path / "yt_n97cHIR9Qow.mp4"
    vod.write_bytes(b"x")
    ok, reason = soft_allow_owner_montage_part(
        "pubg", vod, 1845.0, False, "owner_bad_window"
    )
    assert ok is False
    assert reason == "owner_bad_window"


def test_montage_soft_gate_without_owner_labels(tmp_path: Path, monkeypatch) -> None:
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
    )
    assert ok is True
    assert reason.startswith("montage_soft_gate=")
    # Soft gate must not apply to non-montage singles.
    ok2, reason2 = soft_allow_owner_montage_part(
        "pubg",
        vod,
        230.0,
        False,
        "loot_walk=density0.021",
        montage_part=False,
    )
    assert ok2 is False
    assert reason2.startswith("loot_walk")
