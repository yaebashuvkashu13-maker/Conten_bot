#!/usr/bin/env python3
"""Central registry for daily-cycle VOD games — paths, profiles, ops helpers."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

DAILY_GAMES = ("mlbb", "pubg", "standoff")


@dataclass(frozen=True)
class VodGameSpec:
    id: str
    profile: str
    data_root_env: str
    default_data_root: str
    state_name: str = "vod_segment_state.json"
    feed_kind: str = "mlbb"  # mlbb | shooter

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
        return Path(os.environ.get(f"SHOOTER_{g}_SEGMENTS_ROOT", os.environ.get("MLBB_VOD_SEGMENTS_ROOT", str(default))))


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
}


def spec(game: str) -> VodGameSpec:
    g = game.strip().lower()
    if g not in SPECS:
        raise KeyError(f"unknown game {game!r}; expected one of {DAILY_GAMES}")
    return SPECS[g]


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


def adaptive_streak_fn(game: str) -> Callable[[dict], int]:
    s = spec(game)
    if s.feed_kind == "mlbb":
        from mlbb_vod_adaptive_gate import streak_from_state as fn

        return fn
    from shooter_vod_adaptive_gate import streak_from_state as fn

    return fn


def soften_level_fn(game: str) -> Callable[[int], int]:
    s = spec(game)
    if s.feed_kind == "mlbb":
        from mlbb_vod_adaptive_gate import soften_level as fn

        return fn
    from shooter_vod_adaptive_gate import soften_level as fn

    return fn
