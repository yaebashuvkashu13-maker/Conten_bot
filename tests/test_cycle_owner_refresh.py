"""Owner refresh + recycle must not resurrect dead banner VODs."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def test_collapse_duplicates_prefers_exhausted() -> None:
    from cycle_owner_refresh import _collapse_duplicates

    rows = [
        {"id": "abc", "exhausted": False, "reject_reason": ""},
        {"id": "abc", "exhausted": True, "reject_reason": "banner_probe_0_real_double"},
        {"id": "xyz", "exhausted": False},
    ]
    out = _collapse_duplicates(rows)
    by_id = {r["id"]: r for r in out}
    assert len(by_id) == 2
    assert by_id["abc"]["exhausted"] is True
    assert "banner_probe_0" in by_id["abc"]["reject_reason"]


def test_harden_dead_vods(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cycle_owner_refresh import harden_dead_vods

    root = tmp_path / "mlbb"
    root.mkdir()
    state = root / "vod_segment_state.json"
    state.write_text(
        json.dumps(
            {
                "vods": [
                    {
                        "id": "dead1",
                        "path": str(root / "yt_dead1.mp4"),
                        "exhausted": False,
                        "reject_reason": "banner_probe_0_real_double",
                        "soft_reopen_count": 0,
                    },
                    {
                        "id": "dead1",
                        "path": str(root / "yt_dead1.mp4"),
                        "exhausted": True,
                        "reject_reason": "banner_probe_0_real_double",
                    },
                    {"id": "ok1", "exhausted": False, "reject_reason": ""},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("cycle_owner_refresh._data_root", lambda g: tmp_path / g)
    n = harden_dead_vods("mlbb")
    assert n >= 1
    data = json.loads(state.read_text(encoding="utf-8"))
    ids = [r["id"] for r in data["vods"]]
    assert ids.count("dead1") == 1
    dead = next(r for r in data["vods"] if r["id"] == "dead1")
    assert dead["exhausted"] is True
    assert int(dead["soft_reopen_count"]) >= 1


def test_recycle_does_not_blank_banner_exhaust(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cycle_self_heal import recycle_parked_batch

    game_root = tmp_path / "mlbb"
    inbox = game_root / "youtube_nightly" / "inbox"
    parked = game_root / "youtube_nightly" / "park_timeout"
    inbox.mkdir(parents=True)
    parked.mkdir(parents=True)
    state = game_root / "vod_segment_state.json"
    state.write_text(
        json.dumps(
            {
                "vods": [
                    {
                        "id": "dead1",
                        "path": str(inbox / "yt_dead1.mp4"),
                        "exhausted": True,
                        "reject_reason": "banner_probe_0_real_double",
                        "recycle_count": 0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTENT_BOT_DATA", str(tmp_path))
    monkeypatch.setattr("cycle_self_heal._data_root", lambda g: game_root)
    monkeypatch.setattr("cycle_self_heal._inbox_and_parked", lambda g: (inbox, parked))
    monkeypatch.setattr("cycle_self_heal._ffprobe_duration", lambda p: 0.0)
    moved = recycle_parked_batch("mlbb", limit=2)
    assert moved == 0
    data = json.loads(state.read_text(encoding="utf-8"))
    assert data["vods"][0]["exhausted"] is True
    assert data["vods"][0]["reject_reason"] == "banner_probe_0_real_double"


def test_format_refresh_reply_includes_remaining() -> None:
    from cycle_owner_refresh import format_refresh_reply

    text = format_refresh_reply(
        {
            "cleared_stalls": ["mlbb"],
            "hardened_mlbb": 2,
            "kicked": 1,
            "wrapper_restarted": False,
            "summary": {
                "active_game": "pubg",
                "sends": {"mlbb": 2, "pubg": 1, "standoff": 0, "genshin": 0, "wot": 0},
                "remaining": {"mlbb": 3, "pubg": 2, "standoff": 3, "genshin": 5, "wot": 3},
            },
        }
    )
    assert "Обновление запущено" in text
    assert "pubg" in text
    assert "mlbb=3" in text
