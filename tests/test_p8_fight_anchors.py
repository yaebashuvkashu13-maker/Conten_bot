"""Probe8 owner labels steer short firefight cuts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_parse_pubg_segment_sid_strips_p8() -> None:
    from vod_owner_learning import is_probe8_segment_id, parse_pubg_segment_sid

    assert parse_pubg_segment_sid("1tGY_WQoj9c_562_p8") == ("1tGY_WQoj9c", 562.0)
    assert parse_pubg_segment_sid("seg_88r7cP4WPzY_774_p8") == ("88r7cP4WPzY", 774.0)
    assert parse_pubg_segment_sid("abc12345678_120") == ("abc12345678", 120.0)
    assert is_probe8_segment_id("vid_10_p8")
    assert not is_probe8_segment_id("vid_10")


def test_short_fight_cap_near_p8_yes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from shooter_owner_montage import (
        _is_owner_rejected_peak,
        owner_probe8_good_times,
        short_fight_cluster_cap,
    )

    root = tmp_path / "pubg"
    inbox = root / "youtube_nightly" / "inbox"
    inbox.mkdir(parents=True)
    vod = inbox / "yt_abcdefghijk.mp4"
    vod.write_bytes(b"x")
    (root / "vod_segment_labels.json").write_text(
        json.dumps(
            {
                "good": [
                    {
                        "segment_id": "abcdefghijk_600_p8",
                        "vod": str(vod),
                        "start": 600.0,
                        "peak_start": 604.0,
                    }
                ],
                "bad": [
                    {
                        "segment_id": "abcdefghijk_700_p8",
                        "vod": str(vod),
                        "start": 700.0,
                        "peak_start": 704.0,
                        "reason": "loot_run",
                    }
                ],
                "feedback": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(root))
    monkeypatch.setenv("PUBG_P8_SHORT_FIGHT", "1")
    monkeypatch.setenv("PUBG_P8_SHORT_FIGHT_MAX_SEC", "8")

    goods = owner_probe8_good_times("pubg", vod)
    assert goods == [604.0]
    assert short_fight_cluster_cap("pubg", vod, 605.0) == 8.0
    assert short_fight_cluster_cap("pubg", vod, 800.0) is None
    assert _is_owner_rejected_peak("pubg", vod, 704.0) is True
    assert _is_owner_rejected_peak("pubg", vod, 604.0) is False


def test_boost_prefers_p8_fight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from shooter_owner_montage import boost_pool_near_owner_labels

    root = tmp_path / "pubg"
    inbox = root / "youtube_nightly" / "inbox"
    inbox.mkdir(parents=True)
    vod = inbox / "yt_abcdefghijk.mp4"
    vod.write_bytes(b"x")
    (root / "vod_segment_labels.json").write_text(
        json.dumps(
            {
                "good": [
                    {
                        "segment_id": "abcdefghijk_600_p8",
                        "vod": str(vod),
                        "start": 600.0,
                        "peak_start": 604.0,
                    }
                ],
                "bad": [],
                "feedback": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(root))
    pool = [
        {"start": 605.0, "score": 1.0},
        {"start": 900.0, "score": 1.5},
    ]
    out = boost_pool_near_owner_labels("pubg", vod, pool)
    assert out[0].get("owner_p8_fight") is True
    assert out[0]["start"] == 605.0
    assert float(out[0]["score"]) == pytest.approx(1.38)
