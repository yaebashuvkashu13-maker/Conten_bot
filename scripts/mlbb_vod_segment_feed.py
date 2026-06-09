#!/usr/bin/env python3
"""
MLBB VOD calibration: send every suitable segment as its own clip (no montage merge).

Owner rates with 👍 Ок / 👎 Не ок buttons — all passing segments, no 3-clip cap.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_vod_segment_store import (
    SEGMENTS_ROOT,
    inline_keyboard_markup,
    labeled_ids,
    load_feed_sent,
    mark_feed_sent,
    segment_id,
    stats,
    upsert_segment,
    vod_youtube_id,
)
from montage_env import strict_peak_env
from preview_gate import validate_clips_before_preview
from strict_montage_direct import discover_strict_candidates, file_sha256
from youtube_download import load_env

ENV_PATH = Path("/root/.video_bot.env")
PROFILE = "mobile_legends"
TELEGRAM_MAX_BYTES = 20 * 1024 * 1024

VOD_PATHS = (
    Path("/root/videos/owner_mlbb_E4Dsp53yvv4_v2_20260608_144710.mp4"),
    Path("/root/videos/yt_E4Dsp53yvv4.mp4"),
)


def pick_vod() -> Path | None:
    for path in VOD_PATHS:
        if path.exists():
            return path
    inbox = Path("/root/data/mlbb/youtube_nightly/inbox")
    for pattern in ("*E4Dsp53yvv4*", "yt_*.mp4", "owner_mlbb_*.mp4"):
        for path in sorted(inbox.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True):
            if path.is_file() and path.suffix == ".mp4":
                return path
    videos = sorted(Path("/root/videos").glob("owner_mlbb_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    return videos[0] if videos else None


def send_video(token: str, chat_id: str, path: Path, caption: str, *, seg_id: str) -> bool:
    if path.stat().st_size > TELEGRAM_MAX_BYTES:
        return False
    url = f"https://api.telegram.org/bot{token}/sendVideo"
    cmd = [
        "curl",
        "-sS",
        "--noproxy",
        "*",
        "-m",
        "600",
        "-F",
        f"chat_id={chat_id}",
        "-F",
        "supports_streaming=true",
        "-F",
        f"caption={caption[:900]}",
        "-F",
        f"reply_markup={json.dumps(inline_keyboard_markup(seg_id), ensure_ascii=False)}",
        "-F",
        f"video=@{path}",
        url,
    ]
    clean_env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    result = subprocess.run(cmd, capture_output=True, text=True, env=clean_env, timeout=620)
    try:
        return bool(json.loads(result.stdout).get("ok"))
    except json.JSONDecodeError:
        return False


def send_message(token: str, chat_id: str, text: str) -> None:
    subprocess.run(
        [
            "curl",
            "-sS",
            "--noproxy",
            "*",
            "-F",
            f"chat_id={chat_id}",
            "-F",
            f"text={text[:3900]}",
            f"https://api.telegram.org/bot{token}/sendMessage",
        ],
        env={k: v for k, v in os.environ.items() if "proxy" not in k.lower()},
        check=False,
        timeout=30,
    )


def render_single_segment(vod: Path, clip: dict, out_path: Path) -> bool:
    from smart_video_editor import render_segment, run_command

    logo = Path(os.environ.get("LOGO_FILE", "/root/logo.png"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="mlbb-seg-"))
    try:
        seg_path = temp_dir / "seg.mp4"
        clip = {**clip, "source_path": str(vod), "source_index": 0}
        render_segment(clip, seg_path, logo)
        if not seg_path.exists():
            return False
        run_command(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-i",
                str(seg_path),
                "-c",
                "copy",
                str(out_path),
            ]
        )
        return out_path.exists()
    finally:
        import shutil

        shutil.rmtree(temp_dir, ignore_errors=True)


def main() -> int:
    if os.environ.get("MLBB_ONLY_MODE", "1") != "1":
        print("SKIP: MLBB_ONLY_MODE not set")
        return 0

    os.environ.setdefault("HIGHLIGHT_HEATMAP", "0")
    os.environ.setdefault("HIGHLIGHT_USE_OWNER_ANCHORS", "0")
    os.environ.setdefault("STRICT_PROBE_LIMIT", os.environ.get("MLBB_VOD_PROBE_LIMIT", "50"))
    os.environ.setdefault("OWNER_PREVIEW_REQUIRED", "0")

    for key, val in strict_peak_env(PROFILE).items():
        os.environ[key] = val

    env = {**os.environ, **load_env(ENV_PATH)}
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("TG_BOT_TOKEN or TG_CHAT_ID missing", file=sys.stderr)
        return 1

    vod = pick_vod()
    if not vod or not vod.exists():
        send_message(token, chat_id, "MLBB VOD: нет файла E4Dsp53yvv4 на диске — положи VOD в /root/videos/")
        return 1

    sig = file_sha256(vod)
    labeled = labeled_ids()
    sent = load_feed_sent()
    probe_limit = int(os.environ.get("MLBB_VOD_PROBE_LIMIT", "50"))

    pool = discover_strict_candidates(vod, PROFILE, sig, set())
    pool = pool[:probe_limit]

    to_send: list[dict] = []
    for clip in pool:
        start = float(clip.get("start", 0))
        sid = segment_id(vod, start)
        if sid in labeled or sid in sent:
            continue
        ok, reason, _, metrics_rows, visual_rows = validate_clips_before_preview(vod, PROFILE, [clip])
        if not ok:
            continue
        metrics = (metrics_rows[0] if metrics_rows else {}) or clip.get("highlight_metrics") or {}
        vis = visual_rows[0] if visual_rows else {}
        to_send.append(
            {
                "segment_id": sid,
                "clip": clip,
                "start": start,
                "score": float(clip.get("score") or metrics.get("viral_score") or 0),
                "hook_score": float(metrics.get("hook_score") or (clip.get("highlight_metrics") or {}).get("hook_score") or 0),
                "visual_pass": vis.get("visual_pass", True),
            }
        )

    if not to_send:
        s = stats()
        print(f"nothing to send pending={s['pending']} pool={len(pool)}")
        return 0

    send_message(
        token,
        chat_id,
        f"MLBB VOD — {len(to_send)} кусков с {vod.name}\n"
        f"Каждый отдельно — жми 👍 Ок / 👎 Не ок под видео.\n"
        f"Статистика: 👍{stats()['feedback_yes']} 👎{stats()['feedback_no']}",
    )

    SEGMENTS_ROOT.mkdir(parents=True, exist_ok=True)
    sent_ids: list[str] = []
    for row in to_send:
        sid = row["segment_id"]
        out = SEGMENTS_ROOT / f"seg_{sid}.mp4"
        if not out.exists():
            if not render_single_segment(vod, row["clip"], out):
                continue

        caption = (
            f"MLBB кусок #{sid}\n"
            f"VOD {vod_youtube_id(vod)} @ {int(row['start'])}s\n"
            f"score={row['score']:.3f} hook={row['hook_score']:.2f}\n"
            f"👍 Ок / 👎 Не ок"
        )
        if not send_video(token, chat_id, out, caption, seg_id=sid):
            send_message(token, chat_id, f"{caption}\n(файл >20MB — не отправился)")
        upsert_segment(
            {
                "segment_id": sid,
                "path": str(out),
                "vod": str(vod),
                "vod_id": vod_youtube_id(vod),
                "start": row["start"],
                "score": row["score"],
                "hook_score": row["hook_score"],
                "sig": sig,
                "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        sent_ids.append(sid)
        time.sleep(1.5)

    mark_feed_sent(sent_ids)
    print(f"sent={len(sent_ids)} from pool={len(pool)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
