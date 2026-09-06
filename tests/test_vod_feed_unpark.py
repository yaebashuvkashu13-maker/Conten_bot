from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from vod_feed_recover import unpark_ready_vods  # noqa: E402


def test_unpark_ready_vods_moves_largest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inbox = tmp_path / "inbox"
    parked = tmp_path / "parked"
    inbox.mkdir()
    parked.mkdir()
    big = parked / "yt_AAAAAAAAAAA.mp4"
    small = parked / "yt_BBBBBBBBBBB.mp4"
    big.write_bytes(b"x" * 50_000_000)
    small.write_bytes(b"y" * 45_000_000)
    state_path = tmp_path / "state.json"
    state_path.write_text('{"vods":[]}', encoding="utf-8")

    class Spec:
        def inbox(self):
            return inbox

    monkeypatch.setattr("vod_feed_recover.spec", lambda _g: Spec())
    monkeypatch.setattr("vod_feed_recover.load_state", lambda _g: json.loads(state_path.read_text()))
    monkeypatch.setattr(
        "vod_feed_recover.save_state",
        lambda _g, state: state_path.write_text(json.dumps(state), encoding="utf-8"),
    )

    moved = unpark_ready_vods("pubg", limit=1)
    assert moved == 1
    assert (inbox / "yt_AAAAAAAAAAA.mp4").exists()
    assert not big.exists()
    assert small.exists()
