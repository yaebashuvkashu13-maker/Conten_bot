#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_vod_yield_memory import (  # noqa: E402
    ally_trap_uploaders,
    candidate_bonus,
    load_memory,
    memory_path,
    pick_penalty,
    preferred_heroes,
    record_feedback,
    record_scan,
    record_send,
    row_score,
)


def test_yield_memory_prefer_liked_own_kill(tmp_path, monkeypatch) -> None:
    path = tmp_path / "vod_yield_memory.json"
    monkeypatch.setenv("MLBB_VOD_YIELD_MEMORY", str(path))
    monkeypatch.setenv("MLBB_VOD_YIELD_MEMORY_ENABLED", "1")
    monkeypatch.setenv("MLBB_DATA_ROOT", str(tmp_path))

    record_scan(
        youtube_id="GOODVIDEO01",
        uploader="GoodChan",
        title="Chou savage ranked full match",
        banner_hits=3,
        own_kill_hits=2,
        own_kill_rejects=1,
    )
    record_send(youtube_id="GOODVIDEO01", uploader="GoodChan", title="Chou savage", sent=1)
    record_feedback(youtube_id="GOODVIDEO01", is_good=True, uploader="GoodChan", title="Chou")

    record_scan(
        youtube_id="TRAPVIDEO001",
        uploader="TrapChan",
        title="Hayabusa ranked full match",
        banner_hits=5,
        own_kill_hits=0,
        own_kill_rejects=5,
    )
    record_feedback(youtube_id="TRAPVIDEO001", is_good=False, uploader="TrapChan", reason="ally")

    good = candidate_bonus({"id": "GOODVIDEO01", "uploader": "GoodChan", "title": "Chou ranked"})
    trap = candidate_bonus({"id": "TRAPVIDEO001", "uploader": "TrapChan", "title": "Hayabusa ranked"})
    assert good > trap
    assert pick_penalty(youtube_id="GOODVIDEO01", uploader="GoodChan") < pick_penalty(
        youtube_id="TRAPVIDEO001", uploader="TrapChan"
    )
    assert "trapchan" in ally_trap_uploaders(min_rejects=4)
    assert "chou" in preferred_heroes(8)
    assert memory_path() == path
    data = load_memory()
    assert data["videos"]["GOODVIDEO01"]["likes"] == 1
    assert data["videos"]["TRAPVIDEO001"]["dislikes"] == 1
    assert row_score(data["videos"]["GOODVIDEO01"]) > row_score(data["videos"]["TRAPVIDEO001"])


def test_yield_memory_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MLBB_VOD_YIELD_MEMORY", str(tmp_path / "m.json"))
    monkeypatch.setenv("MLBB_VOD_YIELD_MEMORY_ENABLED", "0")
    record_scan(youtube_id="X", banner_hits=9, own_kill_hits=9)
    assert candidate_bonus({"id": "X", "uploader": "u"}) == 0.0
    assert not Path(os.environ["MLBB_VOD_YIELD_MEMORY"]).exists()
