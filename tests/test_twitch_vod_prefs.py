"""Tests for Twitch VOD discovery prefs."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from twitch_vod_prefs import (  # noqa: E402
    channel_logins,
    parse_flat_playlist_line,
    title_ok,
    twitch_vod_enabled,
    vod_discovery_search_cycle,
)


def test_twitch_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TWITCH_VOD_ENABLED", raising=False)
    assert twitch_vod_enabled("pubg") is False


def test_channel_logins_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWITCH_PUBG_CHANNELS", "foo,bar")
    assert channel_logins("pubg") == ["foo", "bar"]


def test_parse_flat_playlist_line() -> None:
    row = parse_flat_playlist_line("12345|PUBG Metro Royale|3600.0|by_owl|not_live")
    assert row is not None
    assert row["id"] == "12345"
    assert row["url"] == "https://www.twitch.tv/videos/12345"


def test_title_ok_pubg_metro() -> None:
    assert title_ok("pubg", "PUBG Mobile Metro Royale ranked")
    assert not title_ok("pubg", "Just Chatting with friends")


def test_discovery_cycle_rotates_channels() -> None:
    params = vod_discovery_search_cycle(0, "pubg", {})
    assert params["urls"]
    assert "twitch.tv" in params["urls"][0]
