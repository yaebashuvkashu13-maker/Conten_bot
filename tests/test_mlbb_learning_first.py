from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from mlbb_learning_first import (  # noqa: E402
    _holdout_segment_ids,
    can_send,
    dislike_feedback_report,
    enabled,
    max_daily_sends,
)


def test_learning_first_blocks_send_by_default(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_LEARNING_FIRST", "1")
    state_path = Path("/tmp/test_learning_state.json")
    monkeypatch.setenv("MLBB_LEARNING_STATE", str(state_path))
    state_path.write_text(json.dumps({"transition_passed": False, "daily_sends": {}}))

    assert enabled() is True
    ok, reason = can_send(1)
    assert ok is False
    assert reason == "learning_first_gate"


def test_daily_cap_after_transition(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_LEARNING_FIRST", "1")
    monkeypatch.setenv("MLBB_LEARNING_MAX_DAILY", "6")
    state_path = Path("/tmp/test_learning_state_cap.json")
    monkeypatch.setenv("MLBB_LEARNING_STATE", str(state_path))
    state_path.write_text(
        json.dumps({"transition_passed": True, "daily_sends": {"2099-01-01": 6}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("mlbb_learning_first._today_key", lambda: "2099-01-01")
    monkeypatch.setattr("mlbb_learning_first.precision_7d", lambda: 0.30)

    ok, reason = can_send(1)
    assert ok is False
    assert "daily_cap" in reason
    assert max_daily_sends() == 6


def test_holdout_ids_stable_size(monkeypatch, tmp_path: Path) -> None:
    labels = tmp_path / "vod_segment_labels.json"
    feedback = [
        {"segment_id": f"vid_{i}", "owner_label": "yes" if i % 2 else "no", "at": f"2026-06-{i+1:02d} 12:00:00"}
        for i in range(40)
    ]
    labels.write_text(json.dumps({"feedback": feedback, "good": [], "bad": []}), encoding="utf-8")
    monkeypatch.setenv("MLBB_VOD_SEGMENT_LABELS", str(labels))

    holdout = _holdout_segment_ids(20)
    assert len(holdout) == 20


def test_dislike_report_mentions_block_zone(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_LEARNING_FIRST", "1")
    monkeypatch.setenv("HIGHLIGHT_OWNER_BAD_PAD_SEC", "90")
    text = dislike_feedback_report("qa2iNyoPO2Q_508", vod_id="qa2iNyoPO2Q", peak_sec=508, reason="freeze")
    assert "qa2iNyoPO2Q_508" in text
    assert "418" in text or "508" in text
    assert "sendVideo ЗАБЛОКИРОВАН" in text or "precision_7d" in text
