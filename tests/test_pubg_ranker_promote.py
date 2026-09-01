from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pubg_ranker_promote import _atomic_promote, should_promote  # noqa: E402


def test_rejects_candidate_that_increases_bad_accept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBG_RANKER_MIN_OOF_BALANCED_ACCURACY", "0.52")
    candidate = {"oof_balanced_accuracy": 0.70}
    champion = {"oof_balanced_accuracy": 0.65}
    ok, reason = should_promote(
        candidate,
        champion,
        {"accepted_recall": 0.8, "bad_accept_rate": 0.2},
        {"accepted_recall": 0.8, "bad_accept_rate": 0.0},
    )
    assert ok is False
    assert "bad_accept" in reason


def test_accepts_non_regressing_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBG_RANKER_MIN_OOF_BALANCED_ACCURACY", "0.52")
    ok, reason = should_promote(
        {"oof_balanced_accuracy": 0.70},
        {"oof_balanced_accuracy": 0.68},
        {"accepted_recall": 0.81, "bad_accept_rate": 0.0},
        {"accepted_recall": 0.80, "bad_accept_rate": 0.0},
    )
    assert ok is True
    assert reason == "promotion_gates_pass"


def test_atomic_promotion_keeps_previous_model(tmp_path: Path) -> None:
    champion = tmp_path / "ranker.joblib"
    candidate = tmp_path / "ranker.candidate.joblib"
    champion.write_bytes(b"old")
    candidate.write_bytes(b"new")
    previous = _atomic_promote(candidate, champion)
    assert champion.read_bytes() == b"new"
    assert previous is not None
    assert previous.read_bytes() == b"old"
