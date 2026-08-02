"""Tests for pipeline_mode_switch."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pipeline_mode_switch as pms  # noqa: E402


def test_ensure_vod_supervisor_skips_when_running() -> None:
    with patch.object(pms, "_vod_feed_running", return_value=True):
        assert pms.ensure_vod_supervisor() is False


def test_ensure_vod_supervisor_starts_wrapper() -> None:
    wrapper = MagicMock()
    wrapper.exists.return_value = True
    log_path = MagicMock()
    log_path.parent.mkdir = MagicMock()
    log_handle = MagicMock()
    log_path.open.return_value = log_handle

    with (
        patch.object(pms, "_vod_feed_running", return_value=False),
        patch.object(pms, "Path", side_effect=lambda p: wrapper if str(p).endswith("mlbb_vod_segment_feed.sh") else log_path if str(p).endswith("vod_only_supervisor.log") else Path(p)),
        patch("pipeline_mode_switch.subprocess.Popen") as popen,
    ):
        started = pms.ensure_vod_supervisor()

    assert started is True
    popen.assert_called_once()
    args = popen.call_args[0][0]
    assert str(args[0]) == str(wrapper)


def test_activate_vod_mode_starts_supervisor(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".video_bot.env"
    env_file.write_text("MLBB_VOD_ONLY=0\n", encoding="utf-8")
    monkeypatch.setattr(pms, "ENV_PATH", env_file)
    with (
        patch.object(pms, "_pkill"),
        patch.object(pms, "ensure_vod_supervisor", return_value=True) as ensure,
    ):
        msg = pms.activate_vod_mode()
    ensure.assert_called_once()
    assert "VOD-супервизор запущен" in msg
    assert "MLBB_VOD_ONLY=1" in env_file.read_text(encoding="utf-8")
