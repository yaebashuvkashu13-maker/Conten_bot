#!/usr/bin/env python3
"""One-off: download & send N HQ YouTube Shorts, then exit for VOD restore."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_calibration_store import SHORTS_ROOT
from mlbb_youtube_shorts_ingest import NEGATIVE_TITLE, PROFILE, SEARCH_QUERIES, search_shorts
from youtube_download import load_env, subprocess_env_no_proxy, ytdlp_cmd, ytdlp_extra_args

STATE_PATH = Path(os.environ.get("MLBB_HQ_MISSION_STATE", "/root/data/mlbb/hq_shorts_mission.json"))
MISSION_ROOT = Path(os.environ.get("MLBB_HQ_SHORTS_DIR", "/root/datasets/mlbb/youtube_shorts_hq_mission"))
ENV_PATH = Path("/root/.video_bot.env")
TELEGRAM_MAX_BYTES = 20 * 1024 * 1024

HQ_FORMAT = os.environ.get(
    "YOUTUBE_SHORTS_FORMAT_HQ",
    "bv*[vcodec^=avc1][height<=1080][height>=720]+ba/"
    "bv*[height<=1080][height>=720]+ba/"
    "bv*[height<=1080]+ba/b[height<=1080]/best",
)


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"sent_ids": [], "downloaded": {}, "target": 10}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"sent_ids": [], "downloaded": {}, "target": 10}


def save_state(state: dict) -> None:
    state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def probe_quality(path: Path) -> dict:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,bit_rate",
            "-show_entries",
            "format=bit_rate,duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    out = {"height": 0, "width": 0, "codec": "", "bitrate": 0, "duration": 0.0}
    if proc.returncode != 0:
        return out
    try:
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams") or []
        if streams:
            s = streams[0]
            out["height"] = int(s.get("height") or 0)
            out["width"] = int(s.get("width") or 0)
            out["codec"] = str(s.get("codec_name") or "")
            out["bitrate"] = int(s.get("bit_rate") or 0)
        fmt = data.get("format") or {}
        if not out["bitrate"]:
            out["bitrate"] = int(fmt.get("bit_rate") or 0)
        out["duration"] = float(fmt.get("duration") or 0.0)
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return out


def quality_ok(q: dict, *, min_height: int, min_bitrate_kbps: int) -> tuple[bool, str]:
    h = int(q.get("height") or 0)
    br = int(q.get("bitrate") or 0) // 1000
    if h < min_height:
        return False, f"low_height={h}"
    if br > 0 and br < min_bitrate_kbps:
        return False, f"low_bitrate={br}k"
    if h > 0 and h < 480:
        return False, "soap"
    return True, f"{h}p {br}k {q.get('codec', '')}"


def download_hq(url: str, out_dir: Path, env: dict[str, str], video_id: str) -> Path | None:
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"yt_{video_id}.mp4"
    if dest.exists() and dest.stat().st_size > 200_000:
        return dest
    date_after = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y%m%d")
    cmd = ytdlp_cmd(env, use_proxy=False) + [
        "-f",
        HQ_FORMAT,
        "--merge-output-format",
        "mp4",
        "--dateafter",
        date_after,
        "--sleep-requests",
        "1.5",
        "-o",
        str(out_dir / "yt_%(id)s.%(ext)s"),
        "--no-playlist",
        *ytdlp_extra_args(env),
        url,
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, check=False, timeout=420, env=subprocess_env_no_proxy(env)
    )
    if proc.returncode != 0:
        print(f"download_fail {video_id}: {(proc.stderr or proc.stdout or '')[-200:]}")
        return None
    return dest if dest.exists() else None


def send_video(token: str, chat_id: str, path: Path, caption: str) -> bool:
    if path.stat().st_size > TELEGRAM_MAX_BYTES:
        print(f"skip_telegram_too_big {path.name} bytes={path.stat().st_size}")
        return False
    cmd = [
        "curl",
        "-sS",
        "-m",
        "600",
        "-F",
        f"chat_id={chat_id}",
        "-F",
        "supports_streaming=true",
        "-F",
        f"caption={caption[:900]}",
        "-F",
        f"video=@{path}",
        f"https://api.telegram.org/bot{token}/sendVideo",
    ]
    clean = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    result = subprocess.run(cmd, capture_output=True, text=True, env=clean, timeout=620)
    try:
        return bool(json.loads(result.stdout or "{}").get("ok"))
    except json.JSONDecodeError:
        return False


def send_message(token: str, chat_id: str, text: str) -> None:
    subprocess.run(
        [
            "curl",
            "-sS",
            "-F",
            f"chat_id={chat_id}",
            "-F",
            f"text={text[:3900]}",
            f"https://api.telegram.org/bot{token}/sendMessage",
        ],
        check=False,
        timeout=30,
    )


MISSION_QUERIES = SEARCH_QUERIES[:4]


def collect_pool(env: dict[str, str], *, per_query: int) -> list[dict]:
    seen: set[str] = set()
    pool: list[dict] = []
    for query in MISSION_QUERIES:
        print(f"search {query}", flush=True)
        for row in search_shorts(query, limit=per_query, env=env, days=120):
            vid = row["video_id"]
            if vid in seen:
                continue
            seen.add(vid)
            pool.append(row)
        time.sleep(2)
    pool.sort(key=lambda r: int(r.get("view_count") or 0), reverse=True)
    return pool


def existing_hq_files(sent_ids: set[str], *, min_height: int) -> list[tuple[Path, dict]]:
    """Reuse already-downloaded shorts if they pass HQ gate."""
    found: list[tuple[Path, dict]] = []
    for root in (SHORTS_ROOT, MISSION_ROOT):
        if not root.exists():
            continue
        for path in sorted(root.glob("yt_*.mp4")):
            vid = path.stem.replace("yt_", "", 1)
            if not vid or vid in sent_ids:
                continue
            q = probe_quality(path)
            ok, reason = quality_ok(q, min_height=min_height, min_bitrate_kbps=0)
            if ok:
                found.append((path, {"video_id": vid, "title": path.name, "url": f"https://youtube.com/shorts/{vid}", "view_count": 0, "_quality": reason}))
    return found


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=int, default=10)
    parser.add_argument("--min-height", type=int, default=720)
    parser.add_argument("--min-bitrate-kbps", type=int, default=800)
    parser.add_argument("--per-query", type=int, default=25)
    args = parser.parse_args()

    env = {**os.environ, **load_env(ENV_PATH)}
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("TG_BOT_TOKEN or TG_CHAT_ID missing", file=sys.stderr)
        return 1

    state = load_state()
    state["target"] = args.target
    sent_ids = set(state.get("sent_ids") or [])
    need = max(0, args.target - len(sent_ids))

    if need <= 0:
        print(f"mission_done already_sent={len(sent_ids)}")
        return 0

    send_message(
        token,
        chat_id,
        f"🎬 HQ Shorts миссия: нужно {need} роликов (≥{args.min_height}p).\n"
        f"Уже отправлено: {len(sent_ids)}/{args.target}\n"
        "После 10 — автоматически вернёмся к VOD-нарезкам.",
    )

    # Local files first — no YouTube wait
    print("scan local shorts…", flush=True)
    for path, row in existing_hq_files(sent_ids, min_height=args.min_height):
        if len(sent_ids) >= args.target:
            break
        vid = row["video_id"]
        q = probe_quality(path)
        _, reason = quality_ok(q, min_height=args.min_height, min_bitrate_kbps=args.min_bitrate_kbps)
        n = len(sent_ids) + 1
        caption = (
            f"HQ Short {n}/{args.target}\n"
            f"{reason} | {path.stat().st_size // 1024}KB (local)\n"
            f"{row.get('title', '')[:100]}\n"
            f"{row.get('url', '')}"
        )
        if not send_video(token, chat_id, path, caption):
            print(f"send_fail local {vid}", flush=True)
            continue
        sent_ids.add(vid)
        state.setdefault("downloaded", {})[vid] = {"path": str(path), "quality": q}
        state["sent_ids"] = sorted(sent_ids)
        save_state(state)
        print(f"SENT local {n}/{args.target} {vid} {reason}", flush=True)
        time.sleep(2)

    need = max(0, args.target - len(sent_ids))
    if need <= 0:
        send_message(token, chat_id, f"✅ HQ Shorts миссия завершена: {args.target}/{args.target}.")
        print("mission_complete local_only", flush=True)
        return 0

    pool = collect_pool(env, per_query=args.per_query)
    print(f"pool={len(pool)} need={need}", flush=True)

    for row in pool:
        if len(sent_ids) >= args.target:
            break
        vid = row["video_id"]
        if vid in sent_ids:
            continue
        if NEGATIVE_TITLE.search(row.get("title", "")):
            continue

        path = download_hq(row["url"], MISSION_ROOT, env, vid)
        if not path or not path.exists():
            continue

        q = probe_quality(path)
        ok, reason = quality_ok(q, min_height=args.min_height, min_bitrate_kbps=args.min_bitrate_kbps)
        if not ok:
            print(f"REJECT {vid} {reason} title={row.get('title','')[:40]}")
            try:
                path.unlink()
            except OSError:
                pass
            continue

        n = len(sent_ids) + 1
        caption = (
            f"HQ Short {n}/{args.target}\n"
            f"{reason} | {path.stat().st_size // 1024}KB\n"
            f"views={int(row.get('view_count') or 0)}\n"
            f"{row.get('title', '')[:100]}\n"
            f"{row.get('url', '')}"
        )
        if not send_video(token, chat_id, path, caption):
            print(f"send_fail {vid}")
            continue

        sent_ids.add(vid)
        state.setdefault("downloaded", {})[vid] = {"path": str(path), "quality": q, "title": row.get("title")}
        state["sent_ids"] = sorted(sent_ids)
        save_state(state)
        print(f"SENT {n}/{args.target} {vid} {reason}")
        time.sleep(2)

    need = max(0, args.target - len(sent_ids))
    if need > 0:
        send_message(
            token,
            chat_id,
            f"⚠️ HQ миссия: отправлено {len(sent_ids)}/{args.target}, не хватает {need}.\n"
            "Повторите запуск или ослабьте min-height.",
        )
        print(f"incomplete sent={len(sent_ids)} need={need}")
        return 1

    send_message(
        token,
        chat_id,
        f"✅ HQ Shorts миссия завершена: {args.target}/{args.target}.\n"
        "Возвращаюсь к VOD-нарезкам…",
    )
    print(f"mission_complete sent={len(sent_ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
