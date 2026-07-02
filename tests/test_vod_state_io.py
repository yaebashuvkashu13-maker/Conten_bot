"""Tests for vod_state_io atomic JSON persistence."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from vod_state_io import load_json_state, save_json_state  # noqa: E402


def test_save_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    payload = {"vods": [{"id": "abc"}], "used_youtube_ids": ["abc"]}
    save_json_state(path, payload)
    save_json_state(path, {"vods": [{"id": "abc"}], "used_youtube_ids": ["abc"], "n": 2})
    loaded = load_json_state(path, {"vods": []})
    assert loaded["vods"][0]["id"] == "abc"
    assert path.with_suffix(path.suffix + ".bak").exists()


def test_corrupt_json_restores_from_bak(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    good = {"vods": [], "ok": True}
    save_json_state(path, good)
    save_json_state(path, {**good, "n": 2})
    path.write_text("{not json", encoding="utf-8")
    loaded = load_json_state(path, {"vods": []})
    assert loaded.get("ok") is True


def test_second_save_creates_bak(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_json_state(path, {"v": 1})
    save_json_state(path, {"v": 2})
    assert load_json_state(path, {"v": 0})["v"] == 2
    bak = path.with_suffix(path.suffix + ".bak")
    assert bak.exists()
    assert load_json_state(bak, {"v": 0})["v"] == 1
