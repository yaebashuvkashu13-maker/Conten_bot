from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np


@dataclass(slots=True)
class GameplayScore:
    path: str
    is_gameplay: bool
    gameplay_score: float
    sampled_frames: int
    hud_frame_ratio: float
    minimap_score: float
    skill_button_score: float
    top_hud_score: float
    edge_density: float
    source_label: str = ""
    video_id: str = ""
    webpage_url: str = ""
    description: str = ""
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None


def _hud_control_score(gray: np.ndarray) -> float:
    edges = cv2.Canny(gray, 60, 130)
    edge_density = float((edges > 0).mean())

    _, bright = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(bright, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    roundish = 0
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < 45:
            continue
        perimeter = cv2.arcLength(contour, True)
        if perimeter <= 0:
            continue
        circularity = 4.0 * np.pi * area / (perimeter * perimeter)
        if circularity >= 0.35:
            roundish += 1

    contour_score = min(roundish / 6.0, 1.0)
    density_score = min(edge_density / 0.12, 1.0)
    return max(contour_score, density_score * 0.75)


def _frame_gameplay_signals(frame: np.ndarray) -> tuple[float, float, float, float]:
    # Work on a stable portrait-sized frame so fixed HUD regions are comparable.
    resized = cv2.resize(frame, (360, 640), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 70, 140)
    edge_density = float((edges > 0).mean())

    # MLBB gameplay clips usually preserve the game HUD after portrait cropping:
    # minimap/score/timer near the top, joystick lower-left, skills lower-right.
    top_left = gray[20:170, 0:150]
    top_center = gray[0:95, 105:255]
    lower_left = gray[420:620, 0:170]
    lower_right = gray[390:635, 185:360]

    minimap_edges = float((cv2.Canny(top_left, 60, 130) > 0).mean())
    top_edges = float((cv2.Canny(top_center, 60, 130) > 0).mean())
    joystick_score = _hud_control_score(lower_left)
    skill_score = _hud_control_score(lower_right)

    minimap_score = min(minimap_edges / 0.11, 1.0)
    top_hud_score = min(top_edges / 0.10, 1.0)
    skill_button_score = max(skill_score, joystick_score * 0.7)

    return minimap_score, skill_button_score, top_hud_score, edge_density


def score_video(video_path: str | Path, sample_count: int = 10) -> GameplayScore:
    path = Path(video_path)
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {path}")

    frame_total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if frame_total <= 0:
        frame_indices = list(range(sample_count))
    else:
        start = int(frame_total * 0.08)
        stop = max(start + 1, int(frame_total * 0.92))
        frame_indices = np.linspace(start, stop, sample_count, dtype=int).tolist()

    frame_scores: list[float] = []
    minimap_scores: list[float] = []
    skill_scores: list[float] = []
    top_scores: list[float] = []
    edge_scores: list[float] = []

    for frame_idx in frame_indices:
        if frame_total > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_idx))
        ok, frame = cap.read()
        if not ok:
            continue
        minimap, skill, top_hud, edge_density = _frame_gameplay_signals(frame)
        frame_score = (
            0.33 * minimap
            + 0.30 * skill
            + 0.22 * top_hud
            + 0.15 * min(edge_density / 0.14, 1.0)
        )
        frame_scores.append(float(frame_score))
        minimap_scores.append(float(minimap))
        skill_scores.append(float(skill))
        top_scores.append(float(top_hud))
        edge_scores.append(float(edge_density))

    cap.release()

    if not frame_scores:
        return GameplayScore(
            path=str(path),
            is_gameplay=False,
            gameplay_score=0.0,
            sampled_frames=0,
            hud_frame_ratio=0.0,
            minimap_score=0.0,
            skill_button_score=0.0,
            top_hud_score=0.0,
            edge_density=0.0,
        )

    scores = np.array(frame_scores, dtype=np.float32)
    hud_frame_ratio = float((scores >= 0.44).mean())
    gameplay_score = float(scores.mean() * 0.7 + hud_frame_ratio * 0.3)
    is_gameplay = gameplay_score >= 0.48 and hud_frame_ratio >= 0.30

    return GameplayScore(
        path=str(path),
        is_gameplay=is_gameplay,
        gameplay_score=gameplay_score,
        sampled_frames=len(frame_scores),
        hud_frame_ratio=hud_frame_ratio,
        minimap_score=float(np.mean(minimap_scores)),
        skill_button_score=float(np.mean(skill_scores)),
        top_hud_score=float(np.mean(top_scores)),
        edge_density=float(np.mean(edge_scores)),
    )


def _load_manifest_index(input_dir: Path) -> dict[str, dict]:
    records: dict[str, dict] = {}
    for manifest_path in input_dir.glob("*_manifest.jsonl"):
        with manifest_path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                video_id = str(record.get("video_id") or "")
                if video_id:
                    records[video_id] = record
    return records


def scan_directory(
    input_dir: str | Path,
    output_csv: str | Path,
    *,
    sample_count: int = 10,
    limit: int | None = None,
) -> list[GameplayScore]:
    source_dir = Path(input_dir)
    manifest_index = _load_manifest_index(source_dir)
    video_paths = sorted(source_dir.rglob("*.mp4"))
    if limit is not None:
        video_paths = video_paths[:limit]

    results: list[GameplayScore] = []
    for idx, video_path in enumerate(video_paths, 1):
        try:
            result = score_video(video_path, sample_count=sample_count)
        except Exception as exc:
            print(f"Skipping {video_path}: {exc}")
            continue
        record = manifest_index.get(video_path.stem, {})
        result.source_label = str(record.get("source_label") or video_path.parent.name)
        result.video_id = str(record.get("video_id") or video_path.stem)
        result.webpage_url = str(record.get("webpage_url") or "")
        result.description = str(record.get("description") or "")
        result.view_count = record.get("view_count")
        result.like_count = record.get("like_count")
        result.comment_count = record.get("comment_count")
        results.append(result)
        if idx % 25 == 0:
            print(f"Scanned {idx}/{len(video_paths)} videos")

    out = Path(output_csv)
    out.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(asdict(results[0]).keys()) if results else list(GameplayScore.__dataclass_fields__.keys())
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for result in sorted(results, key=lambda row: row.gameplay_score, reverse=True):
            writer.writerow(asdict(result))

    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Filter TikTok videos down to real gameplay scenes.")
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--output-csv", required=True)
    parser.add_argument("--sample-count", type=int, default=10)
    parser.add_argument("--limit", type=int)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    results = scan_directory(
        args.input_dir,
        args.output_csv,
        sample_count=args.sample_count,
        limit=args.limit,
    )
    gameplay_count = sum(1 for result in results if result.is_gameplay)
    print(f"Gameplay videos: {gameplay_count}/{len(results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
