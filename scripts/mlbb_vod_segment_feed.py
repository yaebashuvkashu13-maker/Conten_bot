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

def _ffprobe_duration(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    try:
        return float((proc.stdout or "0").strip())
    except ValueError:
        return 0.0


def pick_vod() -> Path | None:
    """Prefer full YouTube VOD in inbox (GB), not short owner preview clips."""
    candidates: list[Path] = []
    for root in (
        Path("/root/data/mlbb/youtube_nightly/inbox"),
        Path("/root/videos"),
        Path("/root/datasets/mlbb"),
    ):
        if not root.exists():
            continue
        for path in root.rglob("*E4Dsp53yvv4*.mp4"):
            if path.is_file():
                candidates.append(path)
    if not candidates:
        return None
    return max(candidates, key=lambda p: (p.stat().st_size, _ffprobe_duration(p)))


def bootstrap_exemplar_segments() -> list[dict]:
    """Send existing owner-marked exemplar clips when auto-scan finds nothing yet."""
    root = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml")) / "data/highlight_exemplars/mobile_legends"
    rows: list[dict] = []
    for label_dir, is_good_hint in (("good", True), ("bad", False)):
        for path in sorted((root / label_dir).glob("E4Dsp53yvv4_*.mp4")):
            stem = path.stem
            parts = stem.split("_")
            if len(parts) < 3:
                continue
            try:
                start = int(parts[1])
            except ValueError:
                continue
            sid = f"E4Dsp53yvv4_{start}"
            rows.append(
                {
                    "segment_id": sid,
                    "path": path,
                    "start": start,
                    "score": 1.0 if is_good_hint else 0.0,
                    "hook_score": 0.0,
                    "bootstrap": True,
                    "prior_label": label_dir,
                }
            )
    return rows


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


def _collect_scan_segments(vod: Path, sig: str, labeled: dict, sent: set, probe_limit: int) -> list[dict]:
    pool = discover_strict_candidates(vod, PROFILE, sig, set())[:probe_limit]
    out: list[dict] = []
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
        out.append(
            {
                "segment_id": sid,
                "clip": clip,
                "start": start,
                "score": float(clip.get("score") or metrics.get("viral_score") or 0),
                "hook_score": float(metrics.get("hook_score") or (clip.get("highlight_metrics") or {}).get("hook_score") or 0),
                "visual_pass": vis.get("visual_pass", True),
            }
        )
    return out


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

    dur = _ffprobe_duration(vod)
    labeled = labeled_ids()
    sent = load_feed_sent()
    probe_limit = int(os.environ.get("MLBB_VOD_PROBE_LIMIT", "50"))
    full_scan = os.environ.get("MLBB_VOD_FULL_SCAN", "0") == "1"

    to_send: list[dict] = []
    for row in bootstrap_exemplar_segments():
        sid = row["segment_id"]
        if sid in labeled or sid in sent:
            continue
        to_send.append(row)

    sig = file_sha256(vod)
    if not to_send and full_scan:
        to_send = _collect_scan_segments(vod, sig, labeled, sent, probe_limit)

    if not to_send:
        s = stats()
        send_message(
            token,
            chat_id,
            f"MLBB VOD: все текущие куски уже отправлены или оценены.\n"
            f"Полный VOD {vod.name} ({int(dur // 60)} мин) — автоскан ночью.\n"
            f"Напиши /mlbb_vod позже или жди cron.",
        )
        print(f"nothing to send pending={s['pending']} vod={vod.name} full_scan={full_scan}")
        return 0

    mode = "exemplar" if to_send[0].get("bootstrap") else "scan"
    send_message(
        token,
        chat_id,
        f"MLBB VOD — {len(to_send)} кусков ({mode})\n"
        f"Каждый отдельно — жми 👍 Ок / 👎 Не ок под видео.\n"
        f"Статистика: 👍{stats()['feedback_yes']} 👎{stats()['feedback_no']}",
    )

    SEGMENTS_ROOT.mkdir(parents=True, exist_ok=True)
    sent_ids: list[str] = []
    for row in to_send:
        sid = row["segment_id"]
        if row.get("bootstrap"):
            out = Path(row["path"])
        else:
            out = SEGMENTS_ROOT / f"seg_{sid}.mp4"
            if not out.exists():
                if not render_single_segment(vod, row["clip"], out):
                    continue

        prior = row.get("prior_label", "")
        prior_hint = f" (было: {prior})" if prior else ""
        caption = (
            f"MLBB кусок #{sid}\n"
            f"VOD {vod_youtube_id(vod)} @ {int(row['start'])}s{prior_hint}\n"
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
    print(f"sent={len(sent_ids)} mode={mode} vod={vod.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
