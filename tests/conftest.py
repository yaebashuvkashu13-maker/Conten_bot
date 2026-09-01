"""CI path isolation — tests must not require /root on ubuntu-latest."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

WORKSPACE = Path(__file__).resolve().parents[1]
SCRIPTS = WORKSPACE / "scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _layout(root: Path) -> dict[str, Path]:
    mlbb = root / "data" / "mlbb"
    paths = {
        "root": root,
        "mlbb": mlbb,
        "pubg": root / "data" / "pubg",
        "standoff": root / "data" / "standoff",
        "genshin": root / "data" / "genshin",
        "wot": root / "data" / "wot",
        "inbox": mlbb / "youtube_nightly" / "inbox",
        "segments": Path(os.environ.get("CONTENT_BOT_REPO", str(WORKSPACE))) / "data" / "mlbb",
        "vod_cache": root / "data" / "vod_analysis_cache",
        "exemplars": root / "data" / "highlight_exemplars",
        "analysis_cache": mlbb / "analysis_cache",
        "env": root / ".video_bot.env",
        "state": mlbb / "vod_segment_state.json",
        "reject_examples": mlbb / "reject_examples",
        "calibration": mlbb / "calibration_labels.json",
    }
    for key in (
        "mlbb",
        "pubg",
        "standoff",
        "genshin",
        "wot",
        "inbox",
        "vod_cache",
        "exemplars",
        "analysis_cache",
        "reject_examples",
    ):
        paths[key].mkdir(parents=True, exist_ok=True)
    if not paths["env"].exists():
        paths["env"].write_text(
            "TG_BOT_TOKEN=ci-test-token\nTG_CHAT_ID=111\nVK_CALLBACK_SECRET=ci-secret\n",
            encoding="utf-8",
        )
    return paths


def _apply_env(paths: dict[str, Path]) -> None:
    os.environ["CONTENT_BOT_REPO"] = str(WORKSPACE)
    os.environ["MLBB_DATA_ROOT"] = str(paths["mlbb"])
    os.environ["SHOOTER_PUBG_DATA_ROOT"] = str(paths["pubg"])
    os.environ["SHOOTER_STANDOFF_DATA_ROOT"] = str(paths["standoff"])
    os.environ["VOD_GENSHIN_DATA_ROOT"] = str(paths["genshin"])
    os.environ["VOD_WOT_DATA_ROOT"] = str(paths["wot"])
    os.environ["OWNER_BATCH_LOCK"] = str(paths["mlbb"] / "OWNER_BATCH_RUNNING")
    os.environ["VIDEO_BOT_ENV"] = str(paths["env"])
    os.environ["ENV_FILE"] = str(paths["env"])
    os.environ["VOD_ANALYSIS_CACHE_DIR"] = str(paths["vod_cache"])
    os.environ["HIGHLIGHT_EXEMPLAR_ROOT"] = str(paths["exemplars"])
    os.environ["INTELLICLIP_CACHE_DIR"] = str(paths["analysis_cache"])


# Seed env before test collection imports application modules.
_ci_paths = _layout(WORKSPACE / ".ci_test_data")
_apply_env(_ci_paths)


def _patch_module_paths(monkeypatch: pytest.MonkeyPatch, paths: dict[str, Path]) -> None:
    patches: list[tuple[str, str, Any]] = [
        ("mlbb_vod_segment_feed", "INBOX", paths["inbox"]),
        ("mlbb_vod_segment_feed", "STATE_PATH", paths["state"]),
        ("mlbb_vod_segment_feed", "ENV_PATH", paths["env"]),
        ("shooter_vod_segment_feed", "ENV_PATH", paths["env"]),
        ("gameplay_gate", "REJECT_EXAMPLES_DIR", paths["reject_examples"]),
        ("gameplay_gate", "CALIBRATION_LABELS_PATH", paths["calibration"]),
        ("vod_analysis_cache", "DEFAULT_ROOT", str(paths["vod_cache"])),
    ]
    for mod_name, attr, value in patches:
        mod = sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, attr):
            monkeypatch.setattr(mod, attr, value)


@pytest.fixture(autouse=True)
def _ci_path_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect VPS paths to workspace/tmp — no writes under /root."""
    paths = _layout(tmp_path / "case")
    _apply_env(paths)
    for key, val in (
        ("CONTENT_BOT_REPO", str(WORKSPACE)),
        ("MLBB_DATA_ROOT", str(paths["mlbb"])),
        ("SHOOTER_PUBG_DATA_ROOT", str(paths["pubg"])),
        ("SHOOTER_STANDOFF_DATA_ROOT", str(paths["standoff"])),
        ("VOD_GENSHIN_DATA_ROOT", str(paths["genshin"])),
        ("VOD_WOT_DATA_ROOT", str(paths["wot"])),
        ("OWNER_BATCH_LOCK", str(paths["mlbb"] / "OWNER_BATCH_RUNNING")),
        ("VIDEO_BOT_ENV", str(paths["env"])),
        ("ENV_FILE", str(paths["env"])),
        ("VOD_ANALYSIS_CACHE_DIR", str(paths["vod_cache"])),
        ("HIGHLIGHT_EXEMPLAR_ROOT", str(paths["exemplars"])),
        ("INTELLICLIP_CACHE_DIR", str(paths["analysis_cache"])),
    ):
        monkeypatch.setenv(key, val)
    _patch_module_paths(monkeypatch, paths)
