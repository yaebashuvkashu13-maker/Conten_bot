"""Atomic JSON store helpers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from json_atomic_store import atomic_write_json, read_json  # noqa: E402


def test_atomic_write_json(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "data.json"
    atomic_write_json(path, {"a": 1, "b": [2]})
    assert path.exists()
    loaded = read_json(path, {})
    assert loaded == {"a": 1, "b": [2]}


def test_read_json_missing_returns_default(tmp_path: Path) -> None:
    assert read_json(tmp_path / "nope.json", {"x": 0}) == {"x": 0}
