from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pipeline_inbox import inbox_sources_for_game, pick_inbox_source, youtube_id_from_name  # noqa: E402


def test_youtube_id_from_telegram_pending_name() -> None:
    assert youtube_id_from_name("20260608_youtube_ou2CbjDp2Yc.mp4") == "ou2CbjDp2Yc"
    assert youtube_id_from_name("yt_ou2CbjDp2Yc.mp4") == "ou2CbjDp2Yc"


def test_standoff_picks_newest_unclaimed_inbox(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    pubg = inbox / "yt_pubg_old.mp4"
    standoff_old = inbox / "yt_ou2CbjDp2Yc.mp4"
    standoff_new = inbox / "yt_newStandoff1.mp4"
    for path in (pubg, standoff_old, standoff_new):
        path.write_bytes(b"x")
    standoff_new.touch()

    games = [
        {"id": "pubg", "sources": ["yt_pubg_old.mp4"]},
        {"id": "standoff", "sources": ["yt_ou2CbjDp2Yc.mp4"]},
        {"id": "mobile_legends", "sources": []},
    ]
    standoff_game = games[1]
    sources = inbox_sources_for_game(standoff_game, inbox=inbox, all_games=games)
    names = [p.name for p in sources]
    assert names[0] == "yt_ou2CbjDp2Yc.mp4"
    assert "yt_newStandoff1.mp4" in names
    assert "yt_pubg_old.mp4" not in names

    first = pick_inbox_source(standoff_game, 1, inbox=inbox, all_games=games)
    second = pick_inbox_source(standoff_game, 2, inbox=inbox, all_games=games)
    assert first is not None and first.name == "yt_ou2CbjDp2Yc.mp4"
    assert second is not None and second.name == "yt_newStandoff1.mp4"
