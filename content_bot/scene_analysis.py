from __future__ import annotations

import glob
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


@dataclass(slots=True)
class ManifestRecord:
    video_id: str
    description: str
    view_count: int | None
    like_count: int | None
    duration: float | None
    webpage_url: str | None


@dataclass(slots=True)
class VideoCandidate:
    path: Path
    record: ManifestRecord | None
    score: float
    reason: str


@dataclass(slots=True)
class SceneSegment:
    path: Path
    start_sec: float
    duration_sec: float
    score: float
    source_score: float


def probe_duration(path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return 0.0
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def load_manifest_index(manifest_glob: str) -> dict[str, ManifestRecord]:
    index: dict[str, ManifestRecord] = {}
    for manifest_path in sorted(Path(p) for p in glob.glob(manifest_glob)):
        if not manifest_path.is_file():
            continue
        for line in manifest_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            video_id = str(raw.get("video_id") or "")
            if not video_id:
                continue
            index[video_id] = ManifestRecord(
                video_id=video_id,
                description=str(raw.get("description") or "").lower(),
                view_count=raw.get("view_count"),
                like_count=raw.get("like_count"),
                duration=raw.get("duration"),
                webpage_url=raw.get("webpage_url"),
            )
    return index


def video_id_from_path(path: Path) -> str | None:
    stem = path.stem
    if stem.isdigit():
        return stem
    match = re.search(r"(\d{8,})", stem)
    return match.group(1) if match else None


def text_matches_hero(text: str, keywords: list[str]) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in keywords)


def is_excluded(text: str, exclude_keywords: list[str]) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in exclude_keywords)


def score_source_video(
    path: Path,
    record: ManifestRecord | None,
    *,
    duration: float,
    min_source_duration: float,
) -> tuple[float, str] | None:
    if duration < min_source_duration:
        return None

    views = (record.view_count if record else None) or 0
    likes = (record.like_count if record else None) or 0
    view_score = float(np.log1p(max(views, 0))) * 2.0
    like_score = float(np.log1p(max(likes, 0)))
    duration_score = min(duration / 60.0, 2.0)

    # Penalize very long raw uploads; sweet spot ~20-90 sec sources.
    length_penalty = 0.0
    if duration > 120:
        length_penalty = min((duration - 120) / 120.0, 1.5)

    score = view_score + like_score + duration_score - length_penalty
    reason = f"views={views}, likes={likes}, dur={duration:.1f}s"
    return score, reason


def motion_timeline(path: Path, sample_fps: float = 2.0) -> tuple[list[float], float]:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return [], 0.0

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    sample_every = max(int(round(fps / sample_fps)), 1)
    motions: list[float] = []
    prev_gray = None
    frame_idx = 0

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if frame_idx % sample_every != 0:
            frame_idx += 1
            continue

        small = cv2.resize(frame, (160, 90), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape
        y0, y1 = int(h * 0.22), int(h * 0.78)
        x0, x1 = int(w * 0.18), int(w * 0.82)
        center = gray[y0:y1, x0:x1]

        if prev_gray is not None:
            diff = cv2.absdiff(center, prev_gray[y0:y1, x0:x1])
            motions.append(float(diff.mean() / 255.0))
        prev_gray = gray
        frame_idx += 1

    cap.release()
    sample_interval = sample_every / fps if fps else 0.5
    return motions, sample_interval


def find_best_segment(
    path: Path,
    *,
    window_sec: float,
    min_motion: float = 0.008,
) -> SceneSegment | None:
    motions, sample_interval = motion_timeline(path)
    duration = probe_duration(path)
    if duration <= 0:
        return None

    if not motions:
        if duration >= window_sec:
            return SceneSegment(path, 0.0, window_sec, 0.0, 0.0)
        return SceneSegment(path, 0.0, duration, 0.0, 0.0)

    window_samples = max(int(round(window_sec / sample_interval)), 1)
    if len(motions) <= window_samples:
        avg_motion = float(np.mean(motions))
        if avg_motion < min_motion:
            return None
        return SceneSegment(path, 0.0, min(duration, window_sec), avg_motion, 0.0)

    best_score = -1.0
    best_start_idx = 0
    for start in range(0, len(motions) - window_samples + 1):
        window = motions[start : start + window_samples]
        avg = float(np.mean(window))
        peak = float(np.max(window))
        score = avg * 0.7 + peak * 0.3
        if score > best_score:
            best_score = score
            best_start_idx = start

    if best_score < min_motion:
        return None

    start_sec = best_start_idx * sample_interval
    clip_duration = min(window_sec, max(duration - start_sec, 1.0))
    return SceneSegment(path, start_sec, clip_duration, best_score, 0.0)


def filter_candidates(
    video_root: Path,
    manifest_index: dict[str, ManifestRecord],
    *,
    hero_keywords: list[str],
    exclude_keywords: list[str],
    sample_limit: int,
    min_source_duration: float,
) -> list[VideoCandidate]:
    candidates: list[VideoCandidate] = []

    for video_path in sorted(video_root.rglob("*.mp4")):
        video_id = video_id_from_path(video_path)
        record = manifest_index.get(video_id) if video_id else None

        text_blob = " ".join(
            part
            for part in [
                video_path.as_posix(),
                record.description if record else "",
            ]
            if part
        )
        if exclude_keywords and is_excluded(text_blob, exclude_keywords):
            continue
        if not text_matches_hero(text_blob, hero_keywords):
            continue

        duration = probe_duration(video_path)
        scored = score_source_video(
            video_path,
            record,
            duration=duration,
            min_source_duration=min_source_duration,
        )
        if scored is None:
            continue
        score, reason = scored
        candidates.append(VideoCandidate(video_path, record, score, reason))

    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates[:sample_limit]
