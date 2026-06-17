from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from mlbb_kill_ui import (  # noqa: E402
    KillUiResult,
    _match_kill_keywords,
    _normalize_ocr_text,
    _looks_like_tower_banner,
    result_is_multikill,
)


def test_normalize_ocr_text() -> None:
    assert "double" in _normalize_ocr_text("d0uble")


def test_match_double_kill() -> None:
    count, label = _match_kill_keywords("DOUBLE KILL")
    assert count >= 1
    assert label == "double_kill"


def test_match_triple_kill_russian() -> None:
    count, label = _match_kill_keywords("Тройное убийство")
    assert count >= 1
    assert label == "triple_kill"


def test_match_savage() -> None:
    count, label = _match_kill_keywords("SAVAGE!")
    assert count >= 1
    assert label == "savage"


def test_no_match() -> None:
    count, label = _match_kill_keywords("loading screen")
    assert count == 0
    assert label == ""


def test_result_is_multikill_double() -> None:
    row = KillUiResult(0.5, True, 1, 0.1, 0.1, "DOUBLE KILL", "kill_keyword=double_kill", "double_kill")
    assert result_is_multikill(row)


def test_result_is_multikill_rejects_single() -> None:
    row = KillUiResult(0.5, True, 1, 0.1, 0.1, "slain", "kill_keyword=slain", "slain")
    assert not result_is_multikill(row)


def test_tower_banner_rejected() -> None:
    assert _looks_like_tower_banner("Turret Destroyed")
    assert _looks_like_tower_banner("Башня уничтожена")


def test_two_pass_scan_runs_second_half(monkeypatch) -> None:
    from mlbb_kill_ui import scan_vod_kill_peaks

    monkeypatch.setenv("MLBB_KILL_SCAN_TWO_PASS", "1")
    monkeypatch.setenv("MLBB_KILL_SCAN_WINDOW_SEC", "15")
    monkeypatch.setenv("MLBB_KILL_SCAN_STEP_SEC", "30")
    monkeypatch.setenv("MLBB_REQUIRE_MULTIKILL", "1")
    monkeypatch.setenv("MLBB_VOD_MIN_PEAK_SEC", "120")

    calls: list[tuple[float, float]] = []

    def _fake_score(_path, start, duration, *, sample_frames=4):
        calls.append((float(start), float(duration)))
        if start >= 135.0:
            return KillUiResult(
                0.5,
                True,
                1,
                0.1,
                0.1,
                "DOUBLE KILL",
                "kill_keyword=double_kill",
                "double_kill",
            )
        return KillUiResult(0.2, True, 1, 0.1, 0.1, "slain", "kill_keyword=slain", "slain")

    import cv2

    class _Cap:
        def isOpened(self):
            return True

        def get(self, prop):
            if prop == cv2.CAP_PROP_FPS:
                return 30.0
            if prop == cv2.CAP_PROP_FRAME_COUNT:
                return 30 * 1600
            return 0.0

        def release(self):
            return None

    monkeypatch.setattr(cv2, "VideoCapture", lambda _p: _Cap())
    monkeypatch.setattr("mlbb_kill_ui.score_mlbb_kill_ui", _fake_score)

    rows = scan_vod_kill_peaks(Path("/tmp/fake.mp4"), limit=5)
    assert any(r["start_sec"] == 135.0 for r in rows)
    assert (120.0, 15.0) in calls
    assert (135.0, 15.0) in calls


def test_two_pass_skips_second_half_when_multikill_in_first(monkeypatch) -> None:
    from mlbb_kill_ui import scan_vod_kill_peaks

    monkeypatch.setenv("MLBB_KILL_SCAN_TWO_PASS", "1")
    monkeypatch.setenv("MLBB_KILL_SCAN_WINDOW_SEC", "15")
    monkeypatch.setenv("MLBB_KILL_SCAN_STEP_SEC", "30")
    monkeypatch.setenv("MLBB_REQUIRE_MULTIKILL", "1")
    monkeypatch.setenv("MLBB_VOD_MIN_PEAK_SEC", "120")

    calls: list[float] = []

    def _fake_score(_path, start, duration, *, sample_frames=4):
        calls.append(float(start))
        return KillUiResult(
            0.5,
            True,
            1,
            0.1,
            0.1,
            "DOUBLE KILL",
            "kill_keyword=double_kill",
            "double_kill",
        )

    import cv2

    class _Cap:
        def isOpened(self):
            return True

        def get(self, prop):
            if prop == cv2.CAP_PROP_FPS:
                return 30.0
            if prop == cv2.CAP_PROP_FRAME_COUNT:
                return 30 * 1600
            return 0.0

        def release(self):
            return None

    monkeypatch.setattr(cv2, "VideoCapture", lambda _p: _Cap())
    monkeypatch.setattr("mlbb_kill_ui.score_mlbb_kill_ui", _fake_score)

    scan_vod_kill_peaks(Path("/tmp/fake.mp4"), limit=3)
    assert 120.0 in calls
    assert 135.0 not in calls
