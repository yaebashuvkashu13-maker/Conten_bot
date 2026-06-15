#!/usr/bin/env python3
"""Structured HUD signals for MLBB/PUBG clips — minimap, controls, replay vs live."""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np

REPLAY_TITLE = re.compile(r"\b(replay|повтор|match\s*replay|my\s*replay)\b", re.I)

# Normalized ROIs (x0, y0, x1, y1) on 320x180 analysis frame — MLBB portrait-ish crop.
MLBB_ROIS = {
    "minimap": (0.0, 0.72, 0.28, 1.0),
    "joystick": (0.55, 0.72, 1.0, 1.0),
    "top_score": (0.30, 0.0, 0.70, 0.14),
    "kill_banner": (0.18, 0.02, 0.82, 0.30),
}

PUBG_ROIS = {
    "minimap": (0.78, 0.02, 0.98, 0.22),
    "joystick": (0.62, 0.68, 0.98, 0.98),
    "kill_feed": (0.02, 0.12, 0.28, 0.45),
    "top_score": (0.35, 0.0, 0.65, 0.08),
}


@dataclass
class HudSignals:
    minimap_activity: float = 0.0
    joystick_activity: float = 0.0
    top_hud_activity: float = 0.0
    center_motion: float = 0.0
    combat_intensity: float = 0.0
    replay_likelihood: float = 0.0
    live_match_likelihood: float = 0.0
    event_density: float = 0.0

    def to_dict(self) -> dict:
        return {k: round(float(v), 4) for k, v in asdict(self).items()}


def _roi_gray(frame: np.ndarray, roi: tuple[float, float, float, float]) -> np.ndarray:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    x0, y0, x1, y1 = roi
    return gray[int(h * y0) : int(h * y1), int(w * x0) : int(w * x1)]


def _read_frame_at(video_path: Path, t_sec: float):
    try:
        from gameplay_gate import _read_frame_at as _read
    except ImportError:
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from gameplay_gate import _read_frame_at as _read

    return _read(video_path, t_sec)


def _region_temporal_activity(
    video_path: Path,
    *,
    start_sec: float,
    duration_sec: float,
    roi: tuple[float, float, float, float],
    sample_frames: int = 6,
) -> float:
    end = start_sec + max(duration_sec, 0.4)
    times = np.linspace(start_sec, max(start_sec + 0.05, end - 0.05), num=max(sample_frames, 2))
    patches: list[np.ndarray] = []
    for t in times:
        frame = _read_frame_at(video_path, float(t))
        if frame is None:
            continue
        try:
            from video_orientation import resize_for_analysis
        except ImportError:
            resize_for_analysis = lambda f: cv2.resize(f, (320, 180))  # type: ignore

        small = resize_for_analysis(frame)
        patch = _roi_gray(small, roi)
        if patch.size:
            patches.append(patch.astype(np.float32))
    if len(patches) < 2:
        return 0.0
    deltas = [float(np.mean(np.abs(patches[i] - patches[i - 1]))) / 255.0 for i in range(1, len(patches))]
    return float(np.mean(deltas))


def _estimate_replay_likelihood(
    *,
    title: str,
    center_motion: float,
    joystick_activity: float,
    minimap_activity: float,
    top_hud_activity: float,
) -> float:
    score = 0.0
    if REPLAY_TITLE.search(title or ""):
        score += 0.45
    # Screen-recorded replay: lots of action but almost frozen virtual controls.
    if center_motion >= 0.02 and joystick_activity < 0.006 and minimap_activity >= 0.004:
        score += 0.35
    if center_motion >= 0.015 and joystick_activity < 0.004:
        score += 0.15
    # Live phone capture usually shows active thumb zone variance.
    if joystick_activity >= 0.012:
        score -= 0.25
    if top_hud_activity >= 0.008:
        score -= 0.1
    return float(max(0.0, min(1.0, score)))


def analyze_hud_signals(
    video_path: Path,
    *,
    title: str = "",
    start_sec: float = 0.0,
    duration_sec: float | None = None,
    profile: str = "mobile_legends",
) -> HudSignals:
    """Multi-signal HUD read — not a single joystick heuristic."""
    path = Path(video_path)
    if not path.exists():
        return HudSignals()

    try:
        from gameplay_gate import _ffprobe_duration
    except ImportError:
        import subprocess

        def _ffprobe_duration(p: Path) -> float:
            proc = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(p),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
            try:
                return float((proc.stdout or "0").strip())
            except ValueError:
                return 0.0

    dur = duration_sec if duration_sec is not None else _ffprobe_duration(path)
    window = min(15.0, max(4.0, dur * 0.9))
    rois = PUBG_ROIS if profile == "pubg" else MLBB_ROIS

    try:
        from gameplay_gate import score_segment_combat
    except ImportError:
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from gameplay_gate import score_segment_combat

    center_motion, minimap_delta, skill_delta, _ct = score_segment_combat(
        path, start_sec, window, sample_frames=8
    )
    minimap_activity = max(
        minimap_delta,
        _region_temporal_activity(
            path, start_sec=start_sec, duration_sec=window, roi=rois["minimap"]
        ),
    )
    joystick_activity = max(
        skill_delta,
        _region_temporal_activity(
            path, start_sec=start_sec, duration_sec=window, roi=rois["joystick"]
        ),
    )
    top_hud_activity = _region_temporal_activity(
        path, start_sec=start_sec, duration_sec=window, roi=rois["top_score"]
    )

    # Event density: any HUD zone moving — replays can still have minimap fights.
    event_density = float(max(minimap_activity, joystick_activity, top_hud_activity * 0.8))
    replay_likelihood = _estimate_replay_likelihood(
        title=title,
        center_motion=center_motion,
        joystick_activity=joystick_activity,
        minimap_activity=minimap_activity,
        top_hud_activity=top_hud_activity,
    )
    live_match_likelihood = float(
        max(
            0.0,
            min(
                1.0,
                (0.35 if joystick_activity >= 0.01 else 0.0)
                + (0.25 if minimap_activity >= 0.008 else 0.0)
                + (0.20 if center_motion >= 0.018 else 0.0)
                + (0.10 if top_hud_activity >= 0.006 else 0.0)
                - replay_likelihood * 0.5,
            ),
        )
    )
    combat_intensity = float(
        min(
            1.0,
            center_motion * 2.2
            + minimap_activity * 3.5
            + joystick_activity * 2.0
            + top_hud_activity * 1.5,
        )
    )
    return HudSignals(
        minimap_activity=minimap_activity,
        joystick_activity=joystick_activity,
        top_hud_activity=top_hud_activity,
        center_motion=center_motion,
        combat_intensity=combat_intensity,
        replay_likelihood=replay_likelihood,
        live_match_likelihood=live_match_likelihood,
        event_density=event_density,
    )


def hud_learning_boost(signals: HudSignals) -> float:
    """Soft rank boost for active-learning queue — prefer live ranked fights."""
    boost = signals.combat_intensity * 0.15 + signals.live_match_likelihood * 0.12
    boost -= signals.replay_likelihood * 0.08
    return float(boost)


def passes_soft_replay_penalty(signals: HudSignals, env: dict[str, str] | None = None) -> tuple[bool, str]:
    """Optional hard reject for obvious replays when MLBB_REJECT_REPLAY=1."""
    env = env or dict(os.environ)
    if env.get("MLBB_REJECT_REPLAY", "0") != "1":
        return True, "replay_allowed"
    threshold = float(env.get("MLBB_REPLAY_REJECT_THRESHOLD", "0.82"))
    if signals.replay_likelihood >= threshold and signals.live_match_likelihood < 0.25:
        return False, f"likely_replay={signals.replay_likelihood:.2f}"
    return True, "ok"
