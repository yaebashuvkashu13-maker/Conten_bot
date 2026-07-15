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
    assert payload["gates"]["MLBB_VOD_MIN_CLIP_SCORE"] <= 0.16
    assert "VIRAL_MLBB_HOOK_MIN" not in payload["gates"]

    save_patterns(payload)
    clear_patterns_cache()
    os.environ.pop("VIRAL_MLBB_HOOK_MIN", None)
    applied = apply_feedback_gates(force=True)
    assert applied["MLBB_VOD_MIN_CLIP_SCORE"] <= 0.16


def test_feedback_reject_banner_has_soft_floors_not_full_bypass(tmp_path: Path, monkeypatch) -> None:
    patterns = {
        "gates": {
            "MLBB_FEEDBACK_REJECT_HOOK_BELOW": 0.15,
            "MLBB_FEEDBACK_REJECT_FIGHT_DUR_BELOW": 30,
        },
        "rank_profile": {},
    }
    path = tmp_path / "feedback_patterns.json"
    path.write_text(json.dumps(patterns), encoding="utf-8")
    monkeypatch.setenv("MLBB_FEEDBACK_PATTERNS_PATH", str(path))
    monkeypatch.setenv("MLBB_FEEDBACK_GATE", "1")
    monkeypatch.setenv("MLBB_BANNER_MIN_HOOK", "0.05")
    monkeypatch.setenv("MLBB_BANNER_MIN_FIGHT_SEC", "10")
    clear_patterns_cache()

    weak_banner = {
        "hook_score": 0.04,
        "fight_dur": 9,
        "clip_score": 0.2,
        "kill_banner": "savage",
        "kill_banner_tier": 5,
    }
    reject, why = feedback_reject_row(weak_banner)
    assert reject and ("hook" in why or "fight" in why)

    ok_banner = {
        "hook_score": 0.12,
        "fight_dur": 18,
        "clip_score": 0.2,
        "kill_banner": "double",
        "kill_banner_tier": 2,
    }
    reject_ok, _ = feedback_reject_row(ok_banner)
    assert not reject_ok


def test_verified_high_tier_banner_gets_narrow_hook_relief(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "feedback_patterns.json"
    path.write_text(
        json.dumps(
            {
                "gates": {
                    "MLBB_FEEDBACK_REJECT_HOOK_BELOW": 0.08,
                    "MLBB_FEEDBACK_REJECT_FIGHT_DUR_BELOW": 28,
                }
            }
        )
    )
    monkeypatch.setenv("MLBB_FEEDBACK_PATTERNS_PATH", str(path))
    monkeypatch.setenv("MLBB_FEEDBACK_GATE", "1")
    monkeypatch.setenv("MLBB_BANNER_MIN_HOOK", "0.05")
    monkeypatch.setenv("MLBB_BANNER_HIGH_TIER_HOOK_MULT", "0.45")
    monkeypatch.setenv("MLBB_BANNER_OCR_HOOK_FLOOR", "0.015")
    clear_patterns_cache()
    savage = {
        "hook_score": 0.024,
        "fight_dur": 32,
        "kill_banner": "savage",
        "kill_banner_tier": 5,
        "kill_banner_source": "ocr",
    }
    assert feedback_reject_row(savage)[0] is False
    dead = {**savage, "hook_score": 0.010}
    rejected, reason = feedback_reject_row(dead)
    assert rejected is True
    assert "banner_low_hook" in reason
    no_tier = {"hook_score": 0.030, "fight_dur": 32, "kill_banner": "double", "kill_banner_tier": 0}
    rejected2, reason2 = feedback_reject_row(no_tier)
    assert rejected2 is True
    assert "banner_low_hook" in reason2
    # Non-OCR double still uses softer tier mult, not OCR floor.
    double = {
        "hook_score": 0.020,
        "fight_dur": 32,
        "kill_banner": "double",
        "kill_banner_tier": 2,
        "kill_banner_source": "ref",
    }
    monkeypatch.setenv("MLBB_BANNER_DOUBLE_HOOK_MULT", "0.50")
    clear_patterns_cache()
    # 0.05 * 0.50 = 0.025 → 0.020 rejects
    rejected3, reason3 = feedback_reject_row(double)
    assert rejected3 is True
    assert "banner_low_hook" in reason3
    double_ok = {**double, "hook_score": 0.026}
    assert feedback_reject_row(double_ok)[0] is False


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
