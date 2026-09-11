from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def test_id_from_url() -> None:
    from pubg_multiplatform_harvest import _id_from_url

    assert _id_from_url("https://www.tiktok.com/@x/video/1234567890123456789", "tt").startswith("tt_")
    assert len(_id_from_url("https://www.instagram.com/reel/AbCdEfGhIjK/", "ig")) > 5


def test_harvest_instagram_skips_without_cookies(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("INSTAGRAM_COOKIES_PATH", str(tmp_path / "missing.txt"))
    monkeypatch.delenv("PUBG_INSTAGRAM_HARVEST", raising=False)
    from pubg_multiplatform_harvest import harvest_instagram

    out = harvest_instagram({}, {}, limit=2)
    assert out["saved"] == 0
    assert out["skipped"] == "no_instagram_cookies"


def test_harvest_vk_skips_without_user_token(monkeypatch) -> None:
    monkeypatch.setenv("PUBG_VK_HARVEST", "1")
    monkeypatch.setenv("PUBG_VK_CHANNELS", "pubgkotleta")
    monkeypatch.delenv("PUBG_VK_VIDEO_URLS", raising=False)
    monkeypatch.delenv("PUBG_VK_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("VK_USER_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("VK_ACCESS_TOKEN", raising=False)
    from pubg_multiplatform_harvest import harvest_vk

    out = harvest_vk({}, {}, limit=2)
    assert out["saved"] == 0
    assert out["skipped"] == "vk_needs_user_token"
    assert "pubgkotleta" in out["channels"]


def test_vk_channels_parse_url() -> None:
    from pubg_multiplatform_harvest import _vk_channels, _vk_owner_id

    assert _vk_channels({"PUBG_VK_CHANNELS": "https://vk.ru/pubgkotleta"}) == ["pubgkotleta"]
    assert "metroshop_pubg1" in _vk_channels({})
    assert _vk_owner_id("club239893669", token="") == -239893669
    assert _vk_owner_id("pubgmgasper0", token="") == -228773005


def test_proxy_alive_false_for_closed_port() -> None:
    from pubg_multiplatform_harvest import _proxy_alive

    assert _proxy_alive("socks5://127.0.0.1:1") is False


def test_metro_title_re() -> None:
    from pubg_multiplatform_harvest import METRO_TITLE_RE

    assert METRO_TITLE_RE.search("Metro Royale clutch")
    assert METRO_TITLE_RE.search("метро роял перестрелка")
    assert not METRO_TITLE_RE.search("cooking mukbang")
