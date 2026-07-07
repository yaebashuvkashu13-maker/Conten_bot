#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_feedback_gate_tune import (  # noqa: E402
    apply_feedback_gates,
    clear_patterns_cache,
    feedback_rank_boost,
    feedback_reject_row,
)
from mlbb_feedback_pattern_miner import mine_patterns, save_patterns  # noqa: E402


def _write_fixture_labels(tmp_path: Path) -> None:
    labels = {
        "good": [
            {"segment_id": "v1_100", "hook_score": 0.27, "score": 0.01, "clip_score": 0.26, "fight_dur": 55, "reason": ""},
            {"segment_id": "v1_200", "hook_score": 0.30, "score": 0.02, "clip_score": 0.28, "fight_dur": 60, "reason": ""},
        ],
        "bad": [
            {"segment_id": "v1_300", "hook_score": 0.04, "score": 0.001, "clip_score": 0.22, "fight_dur": 30, "reason": "boring"},
            {"segment_id": "v1_400", "hook_score": 0.05, "score": 0.002, "clip_score": 0.23, "fight_dur": 26, "reason": "boring"},
        ],
        "feedback": [],
    }
    (tmp_path / "vod_segment_labels.json").write_text(json.dumps(labels), encoding="utf-8")
    index = {
        "segments": [
            {"segment_id": "v1_100", "clip_score": 0.26, "fight_dur": 55, "peak_start": 100},
            {"segment_id": "v1_300", "clip_score": 0.22, "fight_dur": 30, "peak_start": 300},
        ]
    }
    (tmp_path / "vod_segment_index.json").write_text(json.dumps(index), encoding="utf-8")


def test_mine_patterns_and_gates(tmp_path: Path, monkeypatch) -> None:
    _write_fixture_labels(tmp_path)
    monkeypatch.setenv("MLBB_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("MLBB_VOD_SEGMENT_LABELS", str(tmp_path / "vod_segment_labels.json"))
    monkeypatch.setenv("MLBB_VOD_SEGMENT_INDEX", str(tmp_path / "vod_segment_index.json"))
    monkeypatch.setenv("MLBB_FEEDBACK_PATTERNS_PATH", str(tmp_path / "feedback_patterns.json"))

    payload = mine_patterns()
    assert payload["sources"]["vod_good"] == 2
    assert payload["sources"]["vod_bad"] == 2
    assert payload["precision"] == 0.0  # no feedback bucket rated
    assert "boring" in dict(payload["bad_reasons_vod"])
    assert payload["gates"]["VIRAL_MLBB_HOOK_MIN"] >= 0.08

    save_patterns(payload)
    clear_patterns_cache()
    os.environ.pop("VIRAL_MLBB_HOOK_MIN", None)
    applied = apply_feedback_gates(force=True)
    assert applied["VIRAL_MLBB_HOOK_MIN"] >= 0.08


def test_feedback_reject_and_rank(tmp_path: Path, monkeypatch) -> None:
    patterns = {
        "gates": {
            "MLBB_FEEDBACK_REJECT_HOOK_BELOW": 0.08,
            "MLBB_FEEDBACK_REJECT_FIGHT_DUR_BELOW": 28,
        },
        "rank_profile": {
            "hook_target": 0.27,
            "fight_dur_target": 50,
            "clip_target": 0.25,
            "hook_weight": 0.45,
            "fight_dur_weight": 0.25,
            "clip_weight": 0.30,
        },
    }
    path = tmp_path / "feedback_patterns.json"
    path.write_text(json.dumps(patterns), encoding="utf-8")
    monkeypatch.setenv("MLBB_FEEDBACK_PATTERNS_PATH", str(path))
    monkeypatch.setenv("MLBB_FEEDBACK_GATE", "1")
    clear_patterns_cache()

    bad = {"hook_score": 0.04, "fight_dur": 26, "clip_score": 0.2}
    good = {"hook_score": 0.27, "fight_dur": 55, "clip_score": 0.26}
    reject, why = feedback_reject_row(bad)
    assert reject and "hook" in why
    assert feedback_rank_boost(good) > feedback_rank_boost(bad)
