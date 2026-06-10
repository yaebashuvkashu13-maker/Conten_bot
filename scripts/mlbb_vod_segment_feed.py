#!/usr/bin/env python3
"""
MLBB VOD calibration: send every suitable segment as its own clip (no montage merge).

Owner rates with 👍 Ок / 👎 Не ок buttons — all passing segments, no 3-clip cap.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MLBB_TITLE_RE = re.compile(r"mobile legends|mlbb|bang bang|мобайл легенд", re.I)
INBOX = Path("/root/data/mlbb/youtube_nightly/inbox")

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
SEGMENT_SEC = float(os.environ.get("MLBB_VOD_SEGMENT_SEC", os.environ.get("HIGHLIGHT_WINDOW_SEC", "10")))
STATE_PATH = Path("/root/data/mlbb/vod_segment_state.json")

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


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {"active_vod": "", "scanned_vods": [], "vods": [], "used_youtube_ids": []}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"active_vod": "", "scanned_vods": [], "vods": [], "used_youtube_ids": []}
    data.setdefault("vods", [])
    data.setdefault("used_youtube_ids", [])
    data.setdefault("scanned_vods", [])
    return data


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _registry_entry(path: Path, *, title: str = "", exhausted: bool = False) -> dict:
    vid = vod_youtube_id(path)
    return {
        "id": vid,
        "path": str(path),
        "title": title or path.name,
        "exhausted": exhausted,
        "duration_min": int(_ffprobe_duration(path) // 60),
    }


def _ensure_registry(env: dict[str, str]) -> list[dict]:
    state = _load_state()
    registry: list[dict] = list(state.get("vods", []))
    known = {r.get("id") for r in registry}
    used = set(state.get("used_youtube_ids", []))

    # Bootstrap owner MLBB VOD + any we downloaded before.
    if INBOX.exists():
        for p in sorted(INBOX.glob("yt_*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True):
            vid = vod_youtube_id(p)
            if vid in known or _ffprobe_duration(p) < 1800:
                continue
            if vid in used or vid == "E4Dsp53yvv4":
                from nightly_youtube_montage import fetch_video_meta

                meta = fetch_video_meta(vid, env) or {"title": p.stem, "id": vid}
                title = str(meta.get("title") or p.stem)
                if vid == "E4Dsp53yvv4" or MLBB_TITLE_RE.search(title):
                    registry.append(_registry_entry(p, title=title))
                    known.add(vid)

    state["vods"] = registry
    state["used_youtube_ids"] = sorted(set(used) | known)
    _save_state(state)
    return registry


def _pick_available_vod(registry: list[dict]) -> dict | None:
    for row in registry:
        if row.get("exhausted"):
            continue
        path = Path(str(row.get("path", "")))
        if path.exists() and _ffprobe_duration(path) > 600:
            return row
    return None


def _mark_vod_exhausted(vod_id: str) -> None:
    state = _load_state()
    for row in state.get("vods", []):
        if row.get("id") == vod_id:
            row["exhausted"] = True
    _save_state(state)


def _download_new_mlbb_vod(env: dict[str, str], registry: list[dict]) -> Path | None:
    from nightly_youtube_montage import discover_candidates, download_video, pick_candidate

    state = _load_state()
    used = set(state.get("used_youtube_ids", []))
    used.update(r.get("id", "") for r in registry if r.get("id"))

    min_sec = float(os.environ.get("MLBB_VOD_MIN_SEC", "2700"))  # 45 min
    max_sec = float(os.environ.get("MLBB_VOD_MAX_SEC", "10800"))  # 3 h
    queries = [
        q.strip()
        for q in os.environ.get(
            "MLBB_VOD_SEARCH_QUERIES",
            "Mobile Legends Bang Bang live stream full,MLBB ranked gameplay full match",
        ).split(",")
        if q.strip()
    ]
    candidates = discover_candidates(env, queries=queries, min_sec=min_sec, max_sec=max_sec, search_limit=12)
    pick = pick_candidate(candidates, used)
    if not pick:
        return None

    path = download_video(pick, env)
    entry = _registry_entry(path, title=str(pick.get("title", ""))[:120])
    registry.append(entry)
    state["vods"] = registry
    state["used_youtube_ids"] = sorted(used | {pick["id"]})
    state["active_vod"] = path.name
    _save_state(state)
    return path


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


def _normalize_clip(clip: dict, vod: Path) -> dict:
    from smart_video_editor import profile_action_clip_bounds

    _, clip_hi = profile_action_clip_bounds(PROFILE)
    dur = float(os.environ.get("MLBB_VOD_SEGMENT_SEC", str(max(SEGMENT_SEC, clip_hi))))
    return {
        **clip,
        "source_path": str(vod),
        "source_index": 0,
        "input_duration": dur,
        "output_duration": dur,
        "speed": 1.0,
    }


def render_single_segment(vod: Path, clip: dict, out_path: Path) -> bool:
    """
    Cut montage-length window without logo.
    Double-seek avoids frozen first ~2s (keyframe seek before -i).
    """
    from smart_video_editor import (
        TARGET_HEIGHT,
        TARGET_WIDTH,
        OUTPUT_FPS,
        detect_game_viewport_crop,
        ffprobe_has_audio,
        game_audio_filter_chain,
        output_encode_args,
        run_command,
    )

    clip = _normalize_clip(clip, vod)
    start = float(clip["start"])
    dur = float(clip["input_duration"])
    pre_roll = min(float(os.environ.get("MLBB_SEEK_PREROLL", "3")), max(0.0, start))
    rough_seek = max(0.0, start - pre_roll)
    fine_seek = pre_roll

    crop = detect_game_viewport_crop(vod, start, dur)
    crop_prefix = ""
    if crop and len(crop) == 4:
        x, y, w, h = crop
        if w > 0 and h > 0:
            crop_prefix = f"crop={w}:{h}:{x}:{y},"
    vf = (
        f"{crop_prefix}"
        f"scale={TARGET_WIDTH}:{TARGET_HEIGHT}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={TARGET_WIDTH}:{TARGET_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,"
        f"fps={OUTPUT_FPS},setpts=PTS-STARTPTS,format=yuv420p"
    )

    os.environ.setdefault("SMART_OUTPUT_PRESET", "fast")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    has_audio = ffprobe_has_audio(vod)
    cmd = [
        "ffmpeg",
        "-y",
        "-hwaccel",
        "none",
        "-ss",
        f"{rough_seek:.3f}",
        "-i",
        str(vod),
        "-ss",
        f"{fine_seek:.3f}",
        "-t",
        f"{dur:.3f}",
        "-vf",
        vf,
    ]
    if has_audio:
        cmd.extend(["-af", game_audio_filter_chain(1.0), "-map", "0:v:0", "-map", "0:a:0?"])
    else:
        cmd.extend(["-an"])
    cmd.extend(output_encode_args())
    cmd.append(str(out_path))
    run_command(cmd)
    return out_path.exists() and out_path.stat().st_size > 100_000


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


def _send_segment_batch(
    token: str,
    chat_id: str,
    vod: Path,
    to_send: list[dict],
    sig: str,
) -> int:
    seg_sec = int(float(os.environ.get("MLBB_VOD_SEGMENT_SEC", "10")))
    send_message(
        token,
        chat_id,
        f"MLBB VOD — {len(to_send)} кусков (~{seg_sec}с)\n"
        f"Стрим: {vod_youtube_id(vod)} ({vod.name})\n"
        f"👍 Ок / 👎 Не ок под каждым\n"
        f"Статистика: 👍{stats()['feedback_yes']} 👎{stats()['feedback_no']}",
    )
    SEGMENTS_ROOT.mkdir(parents=True, exist_ok=True)
    sent_ids: list[str] = []
    for row in to_send:
        sid = row["segment_id"]
        out = SEGMENTS_ROOT / f"seg_{sid}.mp4"
        force = os.environ.get("MLBB_FORCE_RERENDER", "1") == "1"
        if force or not out.exists() or out.stat().st_size < 500_000:
            if not render_single_segment(vod, row["clip"], out):
                continue
        seg_dur = _ffprobe_duration(out)
        caption = (
            f"MLBB кусок #{sid}\n"
            f"{vod_youtube_id(vod)} @ {int(row['start'])}s | {seg_dur:.0f}с\n"
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
    return len(sent_ids)


def main() -> int:
    if os.environ.get("MLBB_ONLY_MODE", "1") != "1":
        print("SKIP: MLBB_ONLY_MODE not set")
        return 0

    os.environ.setdefault("HIGHLIGHT_HEATMAP", "0")
    os.environ.setdefault("HIGHLIGHT_USE_OWNER_ANCHORS", "0")
    os.environ.setdefault("STRICT_PROBE_LIMIT", os.environ.get("MLBB_VOD_PROBE_LIMIT", "50"))
    os.environ.setdefault("OWNER_PREVIEW_REQUIRED", "0")
    os.environ["LOGO_FILE"] = "/nonexistent/mlbb_calibration_no_logo.png"
    os.environ.setdefault("MLBB_VOD_SEGMENT_SEC", "10")

    for key, val in strict_peak_env(PROFILE).items():
        os.environ[key] = val

    env = {**os.environ, **load_env(ENV_PATH)}
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("TG_BOT_TOKEN or TG_CHAT_ID missing", file=sys.stderr)
        return 1

    labeled = labeled_ids()
    sent = load_feed_sent()
    probe_limit = int(os.environ.get("MLBB_VOD_PROBE_LIMIT", "12"))
    auto_download = os.environ.get("MLBB_VOD_AUTO_DOWNLOAD", "1") == "1"
    registry = _ensure_registry(env)

    for attempt in range(3):
        entry = _pick_available_vod(registry)
        vod: Path | None = Path(entry["path"]) if entry else None

        if vod is None or not vod.exists():
            if not auto_download:
                print("no vod and auto_download=0")
                return 0
            send_message(token, chat_id, "📥 Текущий стрим закончился — качаю новый MLBB VOD с YouTube…")
            vod = _download_new_mlbb_vod(env, registry)
            if not vod:
                send_message(token, chat_id, "⚠️ Не нашёл новый MLBB стрим на YouTube. Повторю позже.")
                return 1
            title = next((r.get("title", "") for r in registry if r.get("id") == vod_youtube_id(vod)), vod.name)
            send_message(
                token,
                chat_id,
                f"✅ Скачал: {title[:80]}\n"
                f"Сканирую и нарежу куски (~{int(_ffprobe_duration(vod) // 60)} мин стрима)…",
            )
            entry = next((r for r in registry if r.get("id") == vod_youtube_id(vod)), None)

        sig = file_sha256(vod)
        to_send = _collect_scan_segments(vod, sig, labeled, sent, probe_limit)
        if to_send:
            n = _send_segment_batch(token, chat_id, vod, to_send, sig)
            state = _load_state()
            state["active_vod"] = vod.name
            scanned = set(state.get("scanned_vods", []))
            scanned.add(vod.name)
            state["scanned_vods"] = sorted(scanned)
            _save_state(state)
            print(f"sent={n} vod={vod.name} attempt={attempt}")
            return 0

        vid = vod_youtube_id(vod)
        _mark_vod_exhausted(vid)
        if entry:
            entry["exhausted"] = True
        print(f"exhausted vod={vod.name} attempt={attempt} — try next/download")
        # loop: pick next local or download on next iteration

    send_message(token, chat_id, "⚠️ Не удалось найти новые куски — попробую снова на следующем cron.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
