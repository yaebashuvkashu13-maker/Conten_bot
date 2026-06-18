#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from youtube_mlbb_vod_prefs import (  # noqa: E402
    passes_mlbb_vod_filters,
    rank_mlbb_vod_candidate,
)


def _meta(title: str, *, uploader: str = "MLBB Ranked", duration: float = 1500.0) -> dict:
    return {
        "id": "abc123",
        "title": title,
        "uploader": uploader,
        "duration": duration,
    }


def test_rejects_montage_and_tutorial() -> None:
    assert not passes_mlbb_vod_filters(_meta("MLBB Savage Montage Best Plays 2024"))
    assert not passes_mlbb_vod_filters(_meta("Mobile Legends Beginner Guide Mythic Rank"))
    assert not passes_mlbb_vod_filters(_meta("MLBB #shorts funny moments"))


def test_accepts_ranked_match_titles() -> None:
    assert passes_mlbb_vod_filters(_meta("MLBB Mythic Ranked Full Match Gameplay 20 min"))
    assert passes_mlbb_vod_filters(_meta("Paquito vs Masha Mythic Ranked Match | MLBB"))
    assert passes_mlbb_vod_filters(_meta("Mobile Legends Legend Solo Queue Full Game Replay"))


def test_rank_prefers_full_match_over_montage_hint() -> None:
    good = _meta("MLBB Mythic Ranked Full Match Gameplay Solo Queue", duration=1500)
    weak = _meta("MLBB Mythic Ranked Match Gameplay Highlights", duration=1500)
    assert rank_mlbb_vod_candidate(good) > rank_mlbb_vod_candidate(weak)


def test_rank_prefers_target_duration() -> None:
    ideal = _meta("MLBB Mythic Ranked Full Match", duration=1500)
    longish = _meta("MLBB Mythic Ranked Full Match", duration=2400)
    assert rank_mlbb_vod_candidate(ideal) > rank_mlbb_vod_candidate(longish)
