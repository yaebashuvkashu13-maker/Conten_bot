
"""Owner timestamps calibrate the bot — they must not drive Telegram sends."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def test_owner_peaks_empty_when_seed_sends_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHOOTER_VOD_OWNER_ANCHOR_MONTAGE", "1")
    monkeypatch.setenv("PUBG_OWNER_LABEL_SEED_SENDS", "0")
    from shooter_owner_montage import owner_good_fight_peaks

    vod = tmp_path / "yt_6mWLqNBX1pE.mp4"
    vod.write_bytes(b"")
    assert owner_good_fight_peaks("pubg", vod) == []


def test_owner_peaks_available_only_with_explicit_seed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHOOTER_VOD_OWNER_ANCHOR_MONTAGE", "1")
    monkeypatch.setenv("PUBG_OWNER_LABEL_SEED_SENDS", "1")
    import shooter_owner_montage as m

    monkeypatch.setattr(m, "_peaks_from_pubg_calibration", lambda vod: [6.0, 147.0, 718.0])
    monkeypatch.setattr(m, "style_reference_peaks", lambda vod: [], raising=False)
    # patch import path used inside function
    monkeypatch.setitem(__import__("sys").modules, "pubg_owner_style", type("X", (), {"style_reference_peaks": staticmethod(lambda vod: [])})())
    vod = tmp_path / "yt_6mWLqNBX1pE.mp4"
    vod.write_bytes(b"")
    peaks = m.owner_good_fight_peaks("pubg", vod)
    assert 6.0 in peaks
    assert 718.0 in peaks


def test_singles_used_gap_default_blocks_near_duplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PUBG_SINGLES_USED_GAP_SEC", raising=False)
    from vod_peak_gap import peak_too_close
    import os
    gap = float(os.environ.get("PUBG_SINGLES_USED_GAP_SEC", "45"))
    assert gap >= 45
    assert peak_too_close(36.8, [52.0], gap) is True
    assert peak_too_close(6.0, [52.0], gap) is False
