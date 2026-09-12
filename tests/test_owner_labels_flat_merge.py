"""Flat `labels` TG feedback must feed the same map as `videos`."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def test_read_labels_file_merges_flat_tg_feedback(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "pubg_owner_labels.json"
    path.write_text(
        json.dumps(
            {
                "videos": {
                    "DGsoEt9znls": [
                        {"time_sec": 606.0, "label": "good", "source": "vod_segment"},
                    ]
                },
                "labels": [
                    {
                        "vod": "yt_DGsoEt9znls.mp4",
                        "video_id": "DGsoEt9znls",
                        "t": 663.0,
                        "time_sec": 663.0,
                        "label": "bad",
                        "note": "loot_run",
                        "source": "owner_tg_feedback",
                        "clip": "seg_DGsoEt9znls_660",
                    },
                    {
                        "vod": "yt_DGsoEt9znls.mp4",
                        "video_id": "DGsoEt9znls",
                        "t": 262.0,
                        "time_sec": 262.0,
                        "label": "bad",
                        "note": "no_combat",
                        "source": "owner_tg_feedback",
                    },
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    import pubg_owner_calibration as cal

    monkeypatch.setattr(cal, "LABELS_PATH", path)
    monkeypatch.setattr(cal, "LEGACY_LABELS_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(cal, "REPO_LABELS_PATH", tmp_path / "missing2.json")
    rows = cal.load_owner_labels()["DGsoEt9znls"]
    times = {(float(r["time_sec"]), r["label"]) for r in rows}
    assert (606.0, "good") in times
    assert (663.0, "bad") in times
    assert (262.0, "bad") in times


def test_dgso_hardcoded_rejected_peaks() -> None:
    from shooter_owner_montage import PUBG_OWNER_REJECTED_PEAKS

    assert 663.0 in PUBG_OWNER_REJECTED_PEAKS["DGsoEt9znls"]
    assert 262.0 in PUBG_OWNER_REJECTED_PEAKS["DGsoEt9znls"]
    assert 1030.0 in PUBG_OWNER_REJECTED_PEAKS["DGsoEt9znls"]
