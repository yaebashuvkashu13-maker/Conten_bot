"""Hook gate RMS parser must read modern ffmpeg astats output."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_parse_rms_db_ametadata_and_human() -> None:
    from clip_hook_gate import _parse_rms_db, _rms_db_to_unit

    meta = (
        "lavfi.astats.1.RMS_level=-23.395175\n"
        "lavfi.astats.2.RMS_level=-22.094109\n"
    )
    assert _parse_rms_db(meta) == pytest.approx(-22.094109)
    human = "[Parsed_astats_0 @ 0x1] RMS level dB: -23.395175\n"
    assert _parse_rms_db(human) == pytest.approx(-23.395175)
    assert _rms_db_to_unit(-30.0) == pytest.approx(0.5)


def test_audio_rms_uses_ametadata_print(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from clip_hook_gate import _audio_rms
    import clip_hook_gate as mod

    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"fake")

    def fake_run(cmd, capture_output=True, text=True, timeout=40):  # noqa: ARG001
        assert any("ametadata=print:file=-" in str(c) for c in cmd)
        out = "lavfi.astats.1.RMS_level=-24.0\n"
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    # (-24 + 60) / 60 = 0.6
    assert _audio_rms(clip, 0.15, 0.7) == pytest.approx(0.6)
