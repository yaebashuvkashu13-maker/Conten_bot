"""P0 drought fixes: exhaust, gun-snap, skip honor, dense deadline, gun-only timeline."""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
import sys

sys.path.insert(0, str(SCRIPTS))


def test_drought_defaults_exhaust_and_gun_snap(monkeypatch: pytest.MonkeyPatch) -> None:
    from vod_force_send import apply_drought_pubg_env

    monkeypatch.delenv("VOD_FORCE_SEND_ZERO_EXHAUST", raising=False)
    monkeypatch.delenv("PUBG_OWNER_NEIGHBORHOOD_GUN_SNAP", raising=False)
    monkeypatch.delenv("SHOOTER_VOD_SKIP_DISCOVERY", raising=False)
    monkeypatch.delenv("VOD_FORCE_SKIP_DISCOVERY", raising=False)
    monkeypatch.delenv("VOD_FORCE_OPS_SKIP_DISCOVERY", raising=False)
    env = apply_drought_pubg_env({}, escalation=0)
    assert env["PUBG_SINGLES_ZERO_SEND_EXHAUST"] == "20"
    assert env["PUBG_OWNER_NEIGHBORHOOD_GUN_SNAP"] == "1"
    assert env["PUBG_DISLIKE_LOOT_FLOOR_LOCK"] == "0"
    assert env["SHOOTER_VOD_SKIP_DISCOVERY"] == "0"


def test_drought_honors_ops_skip_before_absolute_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    from vod_force_send import apply_drought_pubg_env

    monkeypatch.setenv("SHOOTER_VOD_SKIP_DISCOVERY", "1")
    monkeypatch.setenv("VOD_ABSOLUTE_SILENCE_SEC", "5400")
    with patch("vod_hang_detector.last_send_age_sec", return_value=3600.0):
        env = apply_drought_pubg_env({}, escalation=2)
    assert env["SHOOTER_VOD_SKIP_DISCOVERY"] == "1"
    assert env["VOD_FORCE_SKIP_DISCOVERY"] == "1"


def test_drought_clears_ops_skip_after_absolute_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    from vod_force_send import apply_drought_pubg_env

    monkeypatch.setenv("SHOOTER_VOD_SKIP_DISCOVERY", "1")
    monkeypatch.setenv("VOD_ABSOLUTE_SILENCE_SEC", "5400")
    with patch("vod_hang_detector.last_send_age_sec", return_value=12000.0):
        env = apply_drought_pubg_env({}, escalation=2)
    assert env["SHOOTER_VOD_SKIP_DISCOVERY"] == "0"
    assert env["VOD_FORCE_SKIP_DISCOVERY"] == "0"


def test_gunfire_flags_ignore_loud_score_without_gun() -> None:
    from pubg_fight_segment import _gunfire_active_flags

    timeline = [
        {"start": 0.0, "gun": 0.0, "score": 0.95},
        {"start": 2.0, "gun": 0.04, "score": 0.10},
        {"start": 4.0, "gun": 0.01, "score": 0.80},
    ]
    flags = _gunfire_active_flags(timeline, active_min=0.34)
    assert flags == [False, True, False]


def test_elasticity_does_not_harden_under_soften(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import pubg_drought_elasticity as el

    monkeypatch.setenv("PUBG_DROUGHT_ELASTICITY", "1")
    monkeypatch.setenv("PUBG_DROUGHT_ELASTICITY_PATH", str(tmp_path / "el.json"))
    monkeypatch.setenv("VOD_FORCE_SOFTEN", "1")
    monkeypatch.setenv("PUBG_QUALITY_SCORE_MIN_SINGLES", "0.20")
    # Fresh send → would be 1.10 without soften guard
    el.note_successful_send(ts=time.time())
    assert el.elasticity_scale(hours_idle=0.2) == pytest.approx(1.0)
    info = el.apply_elasticity_to_environ(hours_idle=0.2)
    assert float(info["applied"]["PUBG_QUALITY_SCORE_MIN_SINGLES"]) <= 0.20 + 1e-6


def test_dense_deadline_env_is_consulted(monkeypatch: pytest.MonkeyPatch) -> None:
    """SHOOTER_VOD_DENSE_PROBE_DEADLINE_SEC must be read by discover path."""
    src = (SCRIPTS / "shooter_vod_fast_scan.py").read_text(encoding="utf-8")
    assert "SHOOTER_VOD_DENSE_PROBE_DEADLINE_SEC" in src
    assert "_deadline_hit" in src
    batch = (SCRIPTS / "vod_audio_batch.py").read_text(encoding="utf-8")
    assert "SHOOTER_VOD_DENSE_PROBE_DEADLINE_SEC" in batch


def test_adaptive_floors_respect_elasticity(monkeypatch: pytest.MonkeyPatch) -> None:
    from game_adaptive_thresholds import apply_to_environ

    monkeypatch.setenv("PUBG_DROUGHT_ELASTICITY_ACTIVE", "1")
    monkeypatch.setenv("PUBG_SINGLE_MIN_GUN_DENSITY", "0.030")
    monkeypatch.setenv("PUBG_CLIP_MIN_GUN_DENSITY", "0.030")
    monkeypatch.setenv("SMART_PUBG_MIN_GUNFIRE_DENSITY", "0.030")
    monkeypatch.delenv("VOD_FORCE_SOFTEN", raising=False)
    monkeypatch.delenv("VOD_FORCE_ESCALATION", raising=False)
    out = apply_to_environ("pubg")
    assert float(out["gun_density_min"]) <= 0.030 + 1e-6
    assert float(__import__("os").environ["PUBG_SINGLE_MIN_GUN_DENSITY"]) <= 0.030 + 1e-6


def test_tighten_short_kill_single_not_padded() -> None:
    from pubg_montage_bounds import tighten_pubg_clip_bounds

    report = {
        "shooting_start": 100.0,
        "kill_sec": 108.0,
        "fight_end": 130.0,
        "timeline": [
            {"start": t, "gun": 0.08 if 100 <= t <= 108 else 0.0, "score": 0.2}
            for t in range(90, 140, 2)
        ],
    }
    start, dur = tighten_pubg_clip_bounds(95.0, 40.0, report, peak=105.0, single=True)
    assert dur < 18.0, dur
    assert start + dur <= 108.0 + 5.0


def test_gunfire_end_ignores_score_only_bins() -> None:
    from pubg_montage_bounds import _gunfire_end_from_report

    report = {
        "timeline": [
            {"start": 10.0, "gun": 0.0, "score": 0.9},
            {"start": 12.0, "gun": 0.05, "score": 0.1},
            {"start": 20.0, "gun": 0.0, "score": 0.95},
        ]
    }
    end = _gunfire_end_from_report(report, fallback=99.0)
    assert 12.0 <= end <= 16.0


def test_force_send_exhaust_fallback_is_positive() -> None:
    from pathlib import Path

    src = Path(__file__).resolve().parents[1].joinpath("scripts", "vod_force_send.py").read_text(encoding="utf-8")
    assert 'env.get("PUBG_SINGLES_ZERO_SEND_EXHAUST", "20")' in src

def test_drought_overlay_roundtrip(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vod_drought_overlay import clear_drought_overlay, drought_overlay_active, write_drought_overlay

    path = tmp_path / "drought.env"
    monkeypatch.setenv("VOD_DROUGHT_ENV_FILE", str(path))
    write_drought_overlay(
        {
            "VOD_FORCE_SOFTEN": "1",
            "PUBG_QUALITY_SCORE_MIN_SINGLES": "0.20",
            "PUBG_DISLIKE_LOOT_FLOOR_LOCK": "0",
        }
    )
    assert path.is_file()
    assert drought_overlay_active()
    text = path.read_text(encoding="utf-8")
    assert "VOD_FORCE_SOFTEN=1" in text
    assert "PUBG_QUALITY_SCORE_MIN_SINGLES" in text
    clear_drought_overlay()
    assert not path.is_file()


def test_owner_policy_unlocks_loot_floor_under_soften(monkeypatch: pytest.MonkeyPatch) -> None:
    from pubg_owner_calibration import apply_owner_send_policy

    monkeypatch.setenv("VOD_FORCE_SOFTEN", "1")
    monkeypatch.setenv("PUBG_DISLIKE_LOOT_FLOOR_LOCK", "1")
    apply_owner_send_policy()
    assert __import__("os").environ["PUBG_DISLIKE_LOOT_FLOOR_LOCK"] == "0"


def test_force_send_pythonpath_repo_first() -> None:
    from pathlib import Path

    src = Path(__file__).resolve().parents[1].joinpath("scripts", "vod_force_send.py").read_text()
    assert '[scripts_path, local_bin, *parts]' in src or 'scripts_path, local_bin' in src


def test_esc2_hard_unlocks_kill_gates_despite_deploy_pins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deploy pins REQUIRE_*=1; esc2 must hard-assign unlock (not get(pin))."""
    from vod_force_send import apply_drought_pubg_env
    from vod_hang_detector import apply_agent_recover_env

    monkeypatch.setattr("vod_hang_detector.last_send_age_sec", lambda: 9000.0)
    for key, val in (
        ("PUBG_REQUIRE_AUTHOR_KILL_SINGLES", "1"),
        ("PUBG_REQUIRE_AUTHOR_KILL", "1"),
        ("PUBG_DISLIKE_REQUIRE_KILL_EVIDENCE", "1"),
        ("PUBG_COMBAT_ACT_ALLOW_NO_KILL", "0"),
        ("PUBG_OWNER_GOOD_TRUST_NO_KILL", "0"),
        ("PUBG_DISLIKE_LOOT_FLOOR_LOCK", "1"),
    ):
        monkeypatch.setenv(key, val)
    force = apply_drought_pubg_env({}, escalation=2)
    hang = apply_agent_recover_env({}, escalation=2)
    for env in (force, hang):
        assert env["PUBG_REQUIRE_AUTHOR_KILL_SINGLES"] == "0"
        assert env["PUBG_REQUIRE_AUTHOR_KILL"] == "0"
        assert env["PUBG_DISLIKE_REQUIRE_KILL_EVIDENCE"] == "0"
        assert env["PUBG_COMBAT_ACT_ALLOW_NO_KILL"] == "1"
        assert env["PUBG_OWNER_GOOD_TRUST_NO_KILL"] == "1"
        assert env["PUBG_DISLIKE_LOOT_FLOOR_LOCK"] == "0"


def test_daily_cycle_runner_preserves_drought_overlay(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vod_drought_overlay import write_drought_overlay
    import daily_cycle_runner as dcr

    steady = tmp_path / "steady.env"
    drought = tmp_path / "drought.env"
    steady.write_text("VOD_FORCE_SOFTEN=0\nPUBG_QUALITY_SCORE_MIN_SINGLES=0.40\n", encoding="utf-8")
    monkeypatch.setenv("VOD_DROUGHT_ENV_FILE", str(drought))
    monkeypatch.setattr(dcr, "ENV_PATH", steady)
    write_drought_overlay(
        {
            "VOD_FORCE_SOFTEN": "1",
            "PUBG_QUALITY_SCORE_MIN_SINGLES": "0.20",
            "PUBG_REQUIRE_AUTHOR_KILL_SINGLES": "0",
        }
    )
    monkeypatch.setenv("VOD_FORCE_SOFTEN", "1")
    env = dcr._load_runtime_env()
    assert env["VOD_FORCE_SOFTEN"] == "1"
    assert float(env["PUBG_QUALITY_SCORE_MIN_SINGLES"]) == 0.20


def test_isolate_drought_inbox_noop_when_prefer_missing(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vod_hang_detector import isolate_drought_inbox

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "yt_AAAAAAAAAAA.mp4").write_bytes(b"x")
    monkeypatch.setenv("VOD_DROUGHT_PREFER_IDS", "zRQC8jkxbXQ")

    class _Spec:
        def inbox(self):
            return inbox

    monkeypatch.setattr("vod_hang_detector.spec", lambda game: _Spec())
    moved = isolate_drought_inbox("pubg")
    assert moved == []
    assert (inbox / "yt_AAAAAAAAAAA.mp4").is_file()
    assert not (inbox / "parked").exists() or not any((inbox / "parked").iterdir())


def test_force_send_hold_defaults_off() -> None:
    src = Path(__file__).resolve().parents[1].joinpath("scripts", "vod_force_send.py").read_text()
    assert 'VOD_RECOVER_HOLD_SYSTEMD", "0"' in src or "VOD_RECOVER_HOLD_SYSTEMD', '0'" in src


def test_absolute_silence_default_is_5400() -> None:
    force = Path(__file__).resolve().parents[1].joinpath("scripts", "vod_force_send.py").read_text()
    hang = Path(__file__).resolve().parents[1].joinpath("scripts", "vod_hang_detector.py").read_text()
    assert 'VOD_ABSOLUTE_SILENCE_SEC", "5400"' in force
    assert 'VOD_ABSOLUTE_SILENCE_SEC", "5400"' in hang
    assert 'VOD_ABSOLUTE_SILENCE_SEC", "10800"' not in force
    assert 'VOD_ABSOLUTE_SILENCE_SEC", "10800"' not in hang


def test_overlay_includes_kill_unlock_keys() -> None:
    from vod_drought_overlay import OVERLAY_KEYS

    for key in (
        "PUBG_REQUIRE_AUTHOR_KILL_SINGLES",
        "PUBG_DISLIKE_REQUIRE_KILL_EVIDENCE",
        "PUBG_COMBAT_ACT_ALLOW_NO_KILL",
        "PUBG_RELAX_OWNER_HEURISTICS",
        "VOD_PUBG_QUALITY_STRICT",
    ):
        assert key in OVERLAY_KEYS
