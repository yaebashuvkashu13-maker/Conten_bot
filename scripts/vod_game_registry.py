#!/usr/bin/env python3
"""Central registry for VOD pipeline games — paths, profiles, ops helpers."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# Daily-cycle order (quotas reset at Moscow midnight).
DAILY_GAMES = ("mlbb", "pubg", "standoff", "genshin", "wot")
# Bump when VOD gate/feed logic changes — grep logs for this string to verify deploy.
VOD_PIPELINE_REV = "vod-fast-scan-ru-2026-06-26"
# All games with VOD inbox / segment feed support.
VOD_GAMES = DAILY_GAMES


@dataclass(frozen=True)
class VodGameSpec:
    id: str
    profile: str
    data_root_env: str
    default_data_root: str
    state_name: str = "vod_segment_state.json"
    feed_kind: str = "mlbb"  # mlbb | shooter | extended

    @property
    def data_root(self) -> Path:
        return Path(os.environ.get(self.data_root_env, self.default_data_root))

    def inbox(self) -> Path:
        return self.data_root / "youtube_nightly" / "inbox"

    def state_path(self) -> Path:
        return self.data_root / self.state_name

    def index_path(self) -> Path:
        return self.data_root / "vod_segment_index.json"

    def labels_path(self) -> Path:
        return self.data_root / "vod_segment_labels.json"

    def feed_sent_path(self) -> Path:
        return self.data_root / "vod_segment_feed_sent.json"

    def segments_root(self) -> Path:
        g = self.id.upper()
        default = Path("/root/datasets") / self.id / "vod_segments"
        for key in (f"VOD_{g}_SEGMENTS_ROOT", f"SHOOTER_{g}_SEGMENTS_ROOT", "MLBB_VOD_SEGMENTS_ROOT"):
            raw = os.environ.get(key)
            if raw:
                return Path(raw)
        return default


SPECS: dict[str, VodGameSpec] = {
    "mlbb": VodGameSpec(
        id="mlbb",
        profile="mobile_legends",
        data_root_env="MLBB_DATA_ROOT",
        default_data_root="/root/data/mlbb",
        feed_kind="mlbb",
    ),
    "pubg": VodGameSpec(
        id="pubg",
        profile="pubg",
        data_root_env="SHOOTER_PUBG_DATA_ROOT",
        default_data_root="/root/data/pubg",
        feed_kind="shooter",
    ),
    "standoff": VodGameSpec(
        id="standoff",
        profile="standoff",
        data_root_env="SHOOTER_STANDOFF_DATA_ROOT",
        default_data_root="/root/data/standoff",
        feed_kind="shooter",
    ),
    "genshin": VodGameSpec(
        id="genshin",
        profile="genshin",
        data_root_env="VOD_GENSHIN_DATA_ROOT",
        default_data_root="/root/data/genshin",
        feed_kind="extended",
    ),
    "wot": VodGameSpec(
        id="wot",
        profile="wot",
        data_root_env="VOD_WOT_DATA_ROOT",
        default_data_root="/root/data/wot",
        feed_kind="extended",
    ),
}


def spec(game: str) -> VodGameSpec:
    g = game.strip().lower()
    if g not in SPECS:
        raise KeyError(f"unknown game {game!r}; expected one of {VOD_GAMES}")
    return SPECS[g]


def is_extended_game(game: str) -> bool:
    return spec(game).feed_kind == "extended"


def is_shooter_game(game: str) -> bool:
    return spec(game).feed_kind == "shooter"


def load_state(game: str) -> dict:
    path = spec(game).state_path()
    if not path.exists():
        return {"vods": [], "vod_outcomes": [], "zero_cut_streak": 0}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"vods": [], "vod_outcomes": [], "zero_cut_streak": 0}


def save_state(game: str, state: dict) -> None:
    path = spec(game).state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def inbox_video_ids(game: str) -> set[str]:
    inbox = spec(game).inbox()
    if not inbox.is_dir():
        return set()
    return {p.stem.replace("yt_", "") for p in inbox.glob("yt_*.mp4")}


def streak_from_state(state: dict) -> int:
    hist = state.get("vod_outcomes")
    if isinstance(hist, list) and hist:
        n = 0
        for row in reversed(hist):
            if int(row.get("sent", 0)) > 0:
                break
            n += 1
        legacy = int(state.get("zero_cut_streak") or 0)
        return max(n, legacy)
    return int(state.get("zero_cut_streak") or 0)


def exhausted_summary(game: str, state: dict | None = None) -> dict:
    state = state if state is not None else load_state(game)
    inbox_ids = inbox_video_ids(game)
    vods = state.get("vods") or []
    exhausted = [v for v in vods if v.get("exhausted")]
    inbox_exhausted = [v for v in exhausted if str(v.get("id") or "") in inbox_ids]
    reasons: dict[str, int] = {}
    for row in inbox_exhausted:
        key = str(row.get("reject_reason") or "none")[:48]
        reasons[key] = reasons.get(key, 0) + 1
    return {
        "game": game,
        "inbox": len(inbox_ids),
        "registry": len(vods),
        "exhausted_total": len(exhausted),
        "exhausted_inbox": len(inbox_exhausted),
        "streak": streak_from_state(state),
        "top_reject_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])[:6]),
    }


def _adaptive_module(game: str):
    kind = spec(game).feed_kind
    if kind == "mlbb":
        import mlbb_vod_adaptive_gate as mod

        return mod
    if kind == "extended":
        import extended_vod_adaptive_gate as mod

        return mod
    import shooter_vod_adaptive_gate as mod

    return mod


def adaptive_streak_fn(game: str) -> Callable[[dict], int]:
    return _adaptive_module(game).streak_from_state


def soften_level_fn(game: str) -> Callable[[int], int]:
    mod = _adaptive_module(game)
    if is_extended_game(game):

        def _soften(streak: int) -> int:
            return mod.soften_level(streak)

        return _soften
    return mod.soften_level
