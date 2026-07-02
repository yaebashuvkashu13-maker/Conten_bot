from __future__ import annotations

import json
from pathlib import Path

import pytest

from content_bot.config import load_config
from content_bot.instagram_ingest import InstagramPost
from content_bot.state import StateStore
from content_bot.telegram_publisher import build_caption


def test_build_caption_shows_zero_counts(tmp_path: Path) -> None:
    post = InstagramPost(
        post_id="1",
        source_name="test",
        source_url="https://instagram.com/x",
        permalink="https://instagram.com/p/1",
        caption="hi",
        media_url=None,
        thumbnail_url=None,
        view_count=0,
        like_count=0,
        uploader="u",
    )
    cap = build_caption(post)
    assert "views: 0" in cap
    assert "likes: 0" in cap


def test_state_atomic_and_recovery(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    store = StateStore(state_path)
    store.mark_published("post-a")
    assert "post-a" in store.published_ids

    store2 = StateStore(state_path)
    assert "post-a" in store2.published_ids

    store2.record_recovery("post-b")
    store3 = StateStore(state_path)
    assert "post-b" in store3.published_ids


def test_state_corrupt_uses_backup(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    backup = tmp_path / "state.json.bak"
    state_path.write_text(json.dumps({"published_ids": ["x"]}))
    backup.write_text(json.dumps({"published_ids": ["from-backup"]}))
    state_path.write_text("{not json")

    store = StateStore(state_path)
    assert "from-backup" in store.published_ids


def test_load_config_validation(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "telegram:\n  bot_token: ''\n  channel_id: '@x'\ninstagram_sources: []\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="bot_token"):
        load_config(cfg)
