from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_production_env_keeps_strict_quality_and_wrapper_parity() -> None:
    install = (ROOT / "scripts" / "install_mlbb_vod_only.sh").read_text(encoding="utf-8")
    for expected in (
        "MLBB_KILL_BANNER_REQUIRED=1",
        "MLBB_VOD_BANNER_PRESEND=1",
        "MLBB_VOD_MOTION_ANCHOR_OK=0",
        "MLBB_VOD_ZERO_STREAK_SOFTEN=4",
        "MLBB_VOD_SKIP_REVALIDATE=1",
        "MLBB_FIGHT_MAX_SEC=28",
        "MLBB_FIGHT_HARD_MAX_SEC=32",
        "MLBB_FIGHT_TRIM_LONG=1",
    ):
        assert expected in install
    assert "export MLBB_FIGHT_MAX_SEC=55" not in install
    assert "export MLBB_FIGHT_HARD_MAX_SEC=65" not in install


def test_production_accepts_long_vods_without_skipping_early_fights() -> None:
    install = (ROOT / "scripts" / "install_mlbb_vod_only.sh").read_text(encoding="utf-8")
    assert "MLBB_VOD_MAX_SEC=10800" in install
    assert "MLBB_VOD_SKIP_LONG_SEC=10800" in install
    assert "HIGHLIGHT_MLBB_SKIP_INTRO_SEC=90" in install
