
from __future__ import annotations
import sys
from pathlib import Path
from unittest.mock import patch
import pytest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

def test_strong_rms_raises_menu_ceiling(monkeypatch, tmp_path):
    monkeypatch.setenv("CLIP_HOOK_MAX_MENU", "0.55")
    monkeypatch.setenv("CLIP_HOOK_COMBAT_RMS", "0.35")
    monkeypatch.setenv("CLIP_HOOK_COMBAT_MAX_MENU", "0.94")
    import clip_hook_gate as g
    fake = tmp_path / "x.mp4"
    fake.write_bytes(b"0" * 6000)
    with patch.object(g, "_audio_rms", side_effect=[0.67, 0.65]), \
         patch.object(g, "_frame_yavg", side_effect=[140.0, 141.0, 145.0]), \
         patch.object(g, "_overlay_proxy", side_effect=[0.91, 0.90]):
        ok, reason, report = g.hook_gate_clip(fake)
    assert ok, reason
    assert report.get("combat_audio_menu_ceil") == 0.94
