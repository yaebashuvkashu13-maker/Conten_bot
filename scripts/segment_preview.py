#!/usr/bin/env python3
"""Segment proof package: 3 screenshots per segment + owner preview gate."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import cv2

from gameplay_gate import _read_frame_at, detect_game_viewport_crop
from visual_action_check import _norm_profile, segment_frame_times

PROOF_ROOT = Path(os.environ.get("PREVIEW_QUEUE_ROOT", "/root/data/mlbb/preview_queue"))
OWNER_APPROVAL_FILE = "OWNER_APPROVED.json"


def _owner_chat_id(env: dict[str, str]) -> str:
    return env.get("TG_CHAT_ID", "").strip()


def preview_id_for(game: str, basename: str) -> str:
    slug = f"{_norm_profile(game)}_{basename}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    return slug.replace(" ", "_")


def save_frame_jpeg(frame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 88])


def build_proof_package(
    *,
    video_path: Path,
    profile: str,
    game_label: str,
    segments: list[dict],
    visual_rows: list[dict],
    audio_metrics: list[dict],
    montage_path: Path | None,
    preview_id: str,
) -> dict[str, Any]:
    profile = _norm_profile(profile)
    proof_dir = PROOF_ROOT / preview_id
    proof_dir.mkdir(parents=True, exist_ok=True)

    crop_cache: dict[float, tuple | None] = {}
    segment_proofs: list[dict] = []

    for idx, (seg, vis) in enumerate(zip(segments, visual_rows)):
        start = float(seg.get("start", vis["start"]))
        dur = float(seg.get("input_duration") or seg.get("output_duration") or vis["duration"])
        if start not in crop_cache:
            crop_cache[start] = detect_game_viewport_crop(video_path, start, dur)
        crop = crop_cache[start]

        shots: list[dict] = []
        for label, t in segment_frame_times(start, dur):
            frame = _read_frame_at(video_path, t)
            rel = f"seg{idx + 1:02d}_{label}.jpg"
            shot_path = proof_dir / rel
            if frame is not None:
                if crop is not None:
                    x, y, w, h = crop
                    frame = frame[y : y + h, x : x + w]
                save_frame_jpeg(frame, shot_path)
            frame_row = next((f for f in vis.get("frames", []) if f["label"] == label), {})
            shots.append(
                {
                    "label": label,
                    "timestamp": round(t, 3),
                    "path": str(shot_path),
                    "visual_pass": frame_row.get("pass", False),
                    "visual_reason": frame_row.get("reason", ""),
                    "visual_metrics": frame_row.get("metrics", {}),
                }
            )

        audio = audio_metrics[idx] if idx < len(audio_metrics) else {}
        segment_proofs.append(
            {
                "index": idx + 1,
                "start": round(start, 3),
                "duration": round(dur, 3),
                "game": game_label,
                "profile": profile,
                "visual_pass": vis.get("visual_pass", False),
                "frames_passed": vis.get("frames_passed", 0),
                "audio_metrics": audio,
                "screenshots": shots,
            }
        )

    pkg = {
        "preview_id": preview_id,
        "status": "PENDING_OWNER",
        "game": game_label,
        "profile": profile,
        "video_source": str(video_path),
        "montage_path": str(montage_path) if montage_path else None,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "segments": segment_proofs,
        "visual_passed_segments": sum(1 for s in segment_proofs if s["visual_pass"]),
        "total_segments": len(segment_proofs),
        "owner_approved": False,
    }
    proof_json = proof_dir / "proof.json"
    proof_json.write_text(json.dumps(pkg, ensure_ascii=False, indent=2), encoding="utf-8")
    return pkg


def send_proof_to_owner(pkg: dict[str, Any], env: dict[str, str]) -> None:
    """Send screenshots + caption to owner — never sendVideo here."""
    from smart_video_editor import run_command, telegram_curl_env

    token = env.get("TG_BOT_TOKEN", "")
    chat_id = _owner_chat_id(env)
    if not token or not chat_id:
        return

    proof_dir = PROOF_ROOT / pkg["preview_id"]
    lines = [
        f"PREVIEW {pkg['game']} id={pkg['preview_id']}",
        f"visual_passed={pkg['visual_passed_segments']}/{pkg['total_segments']}",
        "Подтверди: /approve_preview " + pkg["preview_id"],
        "Отклонить: /reject_preview " + pkg["preview_id"],
    ]
    for seg in pkg["segments"]:
        lines.append(
            f"seg{seg['index']} start={seg['start']}s visual={seg['visual_pass']} "
            f"gun={seg['audio_metrics'].get('gunfire_density', '—')} "
            f"motion={seg['audio_metrics'].get('center_motion', '—')}"
        )
    caption = "\n".join(lines)[:900]

    curl_env = telegram_curl_env()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    run_command(
        ["curl", "-sS", "-m", "60", "-X", "POST", url, "-d", f"chat_id={chat_id}", "-d", f"text={caption}"],
        env=curl_env,
    )

    for seg in pkg["segments"]:
        for shot in seg["screenshots"]:
            path = Path(shot["path"])
            if not path.exists():
                continue
            cap = (
                f"{pkg['game']} seg{seg['index']} {shot['label']} t={shot['timestamp']} "
                f"visual={shot['visual_pass']} ({shot['visual_reason']})"
            )
            photo_url = f"https://api.telegram.org/bot{token}/sendPhoto"
            run_command(
                [
                    "curl",
                    "-sS",
                    "-m",
                    "120",
                    "-F",
                    f"chat_id={chat_id}",
                    "-F",
                    f"caption={cap[:900]}",
                    "-F",
                    f"photo=@{path}",
                ],
                env=curl_env,
            )


def is_owner_approved(preview_id: str) -> bool:
    path = PROOF_ROOT / preview_id / OWNER_APPROVAL_FILE
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return bool(data.get("approved"))


def approve_preview(preview_id: str, *, by_chat: str = "") -> dict[str, Any] | None:
    proof_path = PROOF_ROOT / preview_id / "proof.json"
    if not proof_path.exists():
        return None
    pkg = json.loads(proof_path.read_text(encoding="utf-8"))
    pkg["owner_approved"] = True
    pkg["status"] = "APPROVED"
    pkg["approved_at"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    pkg["approved_by"] = by_chat
    proof_path.write_text(json.dumps(pkg, ensure_ascii=False, indent=2), encoding="utf-8")
    (PROOF_ROOT / preview_id / OWNER_APPROVAL_FILE).write_text(
        json.dumps({"approved": True, "by": by_chat, "at": pkg["approved_at"]}, indent=2),
        encoding="utf-8",
    )
    return pkg


def reject_preview(preview_id: str, *, by_chat: str = "", reason: str = "") -> None:
    proof_path = PROOF_ROOT / preview_id / "proof.json"
    if not proof_path.exists():
        return
    pkg = json.loads(proof_path.read_text(encoding="utf-8"))
    pkg["status"] = "REJECTED"
    pkg["rejected_at"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    pkg["rejected_by"] = by_chat
    pkg["reject_reason"] = reason
    proof_path.write_text(json.dumps(pkg, ensure_ascii=False, indent=2), encoding="utf-8")


def send_approved_montage(pkg: dict[str, Any], env: dict[str, str], caption: str) -> None:
    montage = pkg.get("montage_path")
    if not montage:
        raise RuntimeError("no montage_path in proof package")
    path = Path(montage)
    if not path.exists():
        raise RuntimeError(f"montage missing: {path}")

    os.environ["OWNER_PREVIEW_APPROVED"] = "1"
    os.environ["STRICT_PEAK_MONTAGE"] = "1"
    os.environ["QUEUE_GAME_PROFILE"] = pkg.get("profile", "")
    os.environ["DEFAULT_GAME_PROFILE"] = pkg.get("profile", "")

    from smart_video_editor import send_telegram_video

    chat_id = _owner_chat_id(env)
    token = env.get("TG_BOT_TOKEN", "")
    if not token or not chat_id:
        raise RuntimeError("TG_BOT_TOKEN/TG_CHAT_ID missing")
    send_telegram_video(token, chat_id, path, caption)
