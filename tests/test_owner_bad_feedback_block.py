"""Tests for owner 👎 feedback blocking nearby peaks."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_owner_rejected_peak_uses_bad_feedback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from shooter_owner_montage import _is_owner_rejected_peak  # noqa: E402

    root = tmp_path / "pubg"
    inbox = root / "youtube_nightly" / "inbox"
    inbox.mkdir(parents=True)
    vod = inbox / "yt_testvid1234.mp4"
    vod.write_bytes(b"x")

    labels_path = root / "vod_segment_labels.json"
    labels_path.write_text(
        json.dumps(
            {
                "feedback": [
                    {
                        "segment_id": "testvid1234_63",
                        "owner_label": "no",
                        "reason": "no_kill",
                        "vod": str(vod),
                    }
                ],
                "good": [],
                "bad": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(root))
    assert _is_owner_rejected_peak("pubg", vod, 63.0) is True
    assert _is_owner_rejected_peak("pubg", vod, 120.0) is False
