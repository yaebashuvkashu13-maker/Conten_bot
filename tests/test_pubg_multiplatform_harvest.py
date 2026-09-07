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
