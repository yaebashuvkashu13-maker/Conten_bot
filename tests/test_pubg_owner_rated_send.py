from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pubg_owner_rated_send import send_owner_rated_clip  # noqa: E402


def test_refuses_send_without_rating_keyboard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_BOT_TOKEN", "tok")
    monkeypatch.setenv("TG_CHAT_ID", "1")
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"not-a-real-mp4")
    with patch("pubg_owner_rated_send.encode_for_telegram", return_value=clip), patch(
        "pubg_owner_rated_send.keyboard",
        return_value={"inline_keyboard": []},
    ):
        with pytest.raises(RuntimeError, match="rating keyboard"):
            send_owner_rated_clip(clip, caption="x", start_sec=10.0, peak_sec=12.0)


def test_send_attaches_keyboard_and_registers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_BOT_TOKEN", "tok")
    monkeypatch.setenv("TG_CHAT_ID", "1")
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"not-a-real-mp4")
    markup = {
        "inline_keyboard": [
            [
                {"text": "👍 Ок", "callback_data": "x"},
                {"text": "👎 Не ок", "callback_data": "y"},
            ]
        ]
    }
    with patch("pubg_owner_rated_send.encode_for_telegram", return_value=clip), patch(
        "pubg_owner_rated_send.keyboard",
        return_value=markup,
    ), patch("pubg_owner_rated_send.send_video_file", return_value=True) as send, patch(
        "pubg_owner_rated_send.upsert_segment"
    ) as upsert, patch("pubg_owner_rated_send.mark_feed_sent") as marked:
        out = send_owner_rated_clip(clip, caption="cap", start_sec=712.0, peak_sec=720.0)
    assert out["ok"] is True
    assert send.call_args.kwargs.get("reply_markup") == markup
    upsert.assert_called_once()
    marked.assert_called_once()
