#!/usr/bin/env python3
"""Download PUBG Mobile HUD reference crops for future template/YOLO gates."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# Roboflow PUBG Mobile sample frames (public dataset previews).
PUBG_REFERENCE_IMAGES = (
    "https://source.roboflow.com/SqKtpTrBYCTd26oAILsc/0vmpDv8PySpDlefWH6u0/original.jpg",
    "https://source.roboflow.com/SqKtpTrBYCTd26oAILsc/0bwoji9EcTBGTEBOwd0T/original.jpg",
    "https://source.roboflow.com/SqKtpTrBYCTd26oAILsc/01xK3JtMTE1SS7e3z70F/original.jpg",
)

PUBG_SAMPLE_VIDEOS = ()

ROOT = Path(os.environ.get("PUBG_UI_REFS_ROOT", "/root/datasets/pubg/ui_refs"))

# Normalized ROI layout for PUBG Mobile (portrait 9:16 typical phone capture).
LAYOUT = {
    "profile": "pubg_mobile",
    "resolution_note": "ROIs are fractions of frame width/height",
    "regions": {
        "minimap": {"x0": 0.78, "y0": 0.02, "x1": 0.98, "y1": 0.22},
        "joystick": {"x0": 0.62, "y0": 0.68, "x1": 0.98, "y1": 0.98},
        "fire_button": {"x0": 0.68, "y0": 0.52, "x1": 0.96, "y1": 0.72},
        "kill_feed": {"x0": 0.02, "y0": 0.12, "x1": 0.28, "y1": 0.45},
        "player_count": {"x0": 0.38, "y0": 0.0, "x1": 0.62, "y1": 0.07},
        "health_bar": {"x0": 0.04, "y0": 0.88, "x1": 0.38, "y1": 0.96},
    },
    "detection_hints": {
        "chicken_dinner": "center overlay + winner text",
        "squad_wipe": "kill feed spike + player count drop",
        "sniper": "scope overlay center + gunfire audio",
    },
    "datasets": [
        "https://universe.roboflow.com/pubg-mobile/peace-game",
        "https://universe.roboflow.com/big-pubg/big-pubgs",
        "https://ieee-dataport.org/documents/guigraphical-user-interface-elements-video-games-shooter-games",
    ],
}


def _download_url(url: str, dest: Path) -> bool:
    if dest.exists() and dest.stat().st_size > 2048:
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pubg-ui-refs/1.0"})
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = resp.read()
        if len(data) < 1024:
            return False
        dest.write_bytes(data)
        return True
    except Exception:
        return False


def _crop_rois(image_path: Path, out_dir: Path) -> list[str]:
    try:
        import cv2
    except ImportError:
        return []

    img = cv2.imread(str(image_path))
    if img is None:
        return []
    h, w = img.shape[:2]
    saved: list[str] = []
    crops_dir = out_dir / "crops" / image_path.stem
    crops_dir.mkdir(parents=True, exist_ok=True)
    for name, roi in LAYOUT["regions"].items():
        x0 = int(w * float(roi["x0"]))
        y0 = int(h * float(roi["y0"]))
        x1 = int(w * float(roi["x1"]))
        y1 = int(h * float(roi["y1"]))
        if x1 <= x0 or y1 <= y0:
            continue
        crop = img[y0:y1, x0:x1]
        if crop.size == 0:
            continue
        out = crops_dir / f"{name}.png"
        cv2.imwrite(str(out), crop)
        saved.append(str(out))
    return saved


def _extract_frame_from_video(url: str, out_jpg: Path, *, at_sec: float = 8.0) -> bool:
    """Best-effort single frame via yt-dlp + ffmpeg."""
    out_jpg.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_jpg.with_suffix(".mp4.part")
    try:
        subprocess.run(
            [
                "yt-dlp",
                "-f",
                "bv*[height<=720]+ba/b[height<=720]",
                "--no-playlist",
                "-o",
                str(tmp),
                url,
            ],
            capture_output=True,
            check=False,
            timeout=120,
        )
        if not tmp.exists() or tmp.stat().st_size < 4096:
            return False
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                str(at_sec),
                "-i",
                str(tmp),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(out_jpg),
            ],
            capture_output=True,
            check=False,
            timeout=60,
        )
        return out_jpg.exists() and out_jpg.stat().st_size > 1024
    except (subprocess.TimeoutExpired, OSError):
        return False
    finally:
        tmp.unlink(missing_ok=True)


def download_pubg_ui_refs(*, extract_video: bool = False) -> dict:
    ROOT.mkdir(parents=True, exist_ok=True)
    frames_dir = ROOT / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    saved_images: list[str] = []
    saved_crops: list[str] = []

    for idx, url in enumerate(PUBG_REFERENCE_IMAGES):
        dest = frames_dir / f"ref_{idx:02d}.jpg"
        if _download_url(url, dest):
            saved_images.append(str(dest))
            saved_crops.extend(_crop_rois(dest, ROOT))

    if extract_video:
        for idx, url in enumerate(PUBG_SAMPLE_VIDEOS):
            dest = frames_dir / f"video_{idx:02d}.jpg"
            if _extract_frame_from_video(url, dest):
                saved_images.append(str(dest))
                saved_crops.extend(_crop_rois(dest, ROOT))

    layout_path = ROOT / "layout.json"
    layout_path.write_text(json.dumps(LAYOUT, indent=2, ensure_ascii=False), encoding="utf-8")

    index = {
        "root": str(ROOT),
        "frames": saved_images,
        "crops": saved_crops,
        "layout": str(layout_path),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    index_path = ROOT / "index.json"
    index_path.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "frames": len(saved_images),
        "crops": len(saved_crops),
        "root": str(ROOT),
        "layout": str(layout_path),
    }


def main() -> int:
    extract = "--extract-video" in sys.argv
    stats = download_pubg_ui_refs(extract_video=extract)
    print(json.dumps(stats, ensure_ascii=False), flush=True)
    return 0 if stats.get("frames", 0) > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
