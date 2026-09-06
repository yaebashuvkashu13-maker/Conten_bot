"""Owner fight-act calibration: do not skip real Metro sprays."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def test_owner_labels_include_6mw_acts() -> None:
    data = json.loads((ROOT / "data" / "pubg_owner_labels.json").read_text(encoding="utf-8"))
    rows = data["videos"]["6mWLqNBX1pE"]
    secs = {int(r["time_sec"]) for r in rows}
    assert {6, 147, 211, 234, 265, 287, 371, 406, 430, 446, 470, 518, 561, 606, 718} <= secs
    assert all(r["label"] == "good" for r in rows)
    assert any(r.get("role") == "style_ref" for r in rows)


def test_burst_escape_beats_fake_gun(monkeypatch: pytest.MonkeyPatch) -> None:
    from pubg_owner_calibration import pubg_passes_owner_heuristics

    monkeypatch.setenv("PUBG_FAKE_GUN_BURST_ESCAPE", "5.5")
    monkeypatch.setenv("PUBG_FAKE_GUN_BURST_ESCAPE_GUN", "0.028")
    ok, reason = pubg_passes_owner_heuristics(
        0.040,
        9.0,
        0.030,
        0.150,  # strafing
        panns_gun_max=0.10,
    )
    assert ok, reason
    assert ("burst_act" in reason or "fight" in reason or "light" in reason
            or "combat_act" in reason or "metro_act" in reason)


def test_owner_good_trusts_payoff_without_redo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import pubg_owner_calibration as cal
    from pubg_quality_score import _owner_redo_trusted

    monkeypatch.setenv("PUBG_OWNER_REDO", "0")
    monkeypatch.setenv("PUBG_OWNER_GOOD_TRUST_PAYOFF", "1")
    labels_path = tmp_path / "labels.json"
    monkeypatch.setattr(cal, "LABELS_PATH", labels_path)
    labels = {
        "videos": {
            "abc123XYZ01": [
                {"tc": "1:00", "time_sec": 60, "label": "good", "role": "style_ref"},
            ]
        }
    }
    labels_path.write_text(json.dumps(labels), encoding="utf-8")
    vod = tmp_path / "yt_abc123XYZ01.mp4"
    vod.write_bytes(b"")
    assert _owner_redo_trusted(vod, 50.0, 20.0) is True
    assert _owner_redo_trusted(vod, 200.0, 20.0) is False


def test_owner_good_fight_peaks_keep_early_acts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import pubg_owner_calibration as cal
    from shooter_owner_montage import owner_good_fight_peaks

    monkeypatch.setenv("SHOOTER_VOD_OWNER_ANCHOR_MONTAGE", "1")
    monkeypatch.setenv("SHOOTER_OWNER_GOOD_MIN_PEAK_SEC", "0")
    labels_path = tmp_path / "labels.json"
    monkeypatch.setattr(cal, "LABELS_PATH", labels_path)
    labels = {
        "videos": {
            "earlyActVod01": [
                {"tc": "0:06", "time_sec": 6, "label": "good"},
                {"tc": "2:27", "time_sec": 147, "label": "good"},
            ]
        }
    }
    labels_path.write_text(json.dumps(labels), encoding="utf-8")
    vod = tmp_path / "yt_earlyActVod01.mp4"
    vod.write_bytes(b"")
    peaks = owner_good_fight_peaks("pubg", vod)
    assert 6.0 in peaks
    assert 147.0 in peaks
