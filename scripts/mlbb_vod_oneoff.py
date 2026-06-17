#!/usr/bin/env python3
"""One-off: download owner URL and send VOD fight cuts to Telegram."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_vod_segment_feed import (
    INBOX,
    VodPipelineDownloader,
    _ensure_registry,
    _load_state,
    _process_vod_segments,
    _registry_entry,
    _save_state,
    log,
    send_message,
)
from mlbb_vod_segment_store import labeled_ids, vod_youtube_id
from nightly_youtube_montage import fetch_video_meta
from youtube_download import load_env, normalize_youtube_url, subprocess_env_no_proxy, ytdlp_cmd, ytdlp_extra_args

ENV_PATH = Path("/root/.video_bot.env")


def download_vod_exact(url: str, dest: Path, env: dict[str, str]) -> Path:
    """Download one VOD to exact path (no shorts duration filter, no wrong-file fallback)."""
    url = normalize_youtube_url(url)
    vid = _video_id(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    template = str(dest.parent / f"yt_{vid}.%(ext)s")
    fmt = env.get(
        "YOUTUBE_FORMAT",
        "bv*[height<=1080][vcodec^=avc1]+ba/bv*[height<=1080]+ba/b[height<=1080]/b",
    )
    cmd = ytdlp_cmd(env, use_proxy=False) + [
        "--no-playlist",
        "--restrict-filenames",
        "--merge-output-format",
        "mp4",
        "-f",
        fmt,
        "--match-filter",
        "duration >= 300",
        *ytdlp_extra_args(env),
        "-o",
        template,
        url,
    ]
    subprocess.run(
        cmd,
        check=True,
        timeout=int(env.get("YOUTUBE_DOWNLOAD_TIMEOUT", "14400")),
        env=subprocess_env_no_proxy(env),
    )
    if not dest.exists() or dest.stat().st_size < 500_000:
        raise RuntimeError(f"download failed or too small: {dest}")
    if vod_youtube_id(dest) != vid:
        raise RuntimeError(f"wrong file after download: {dest.name} expected {vid}")
    return dest


def _video_id(url: str) -> str:
    from urllib.parse import parse_qs, urlparse

    url = normalize_youtube_url(url)
    parsed = urlparse(url)
    if parsed.netloc.endswith("youtu.be"):
        return parsed.path.strip("/").split("/")[0][:11]
    return (parse_qs(parsed.query).get("v") or [""])[0][:11]


def pause_worker() -> None:
    for pat in (
        "mlbb_continuous_worker.py",
        "mlbb_vod_segment_feed.py",
        "mlbb_vod_montage_feed.py",
    ):
        subprocess.run(["pkill", "-f", pat], check=False)
    time.sleep(2)


def resume_worker() -> None:
    repo = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml"))
    script = repo / "scripts/install_mlbb_continuous_worker.sh"
    if script.exists():
        subprocess.run(["bash", str(script)], check=False)


def register_vod(path: Path, *, title: str) -> dict:
    state = _load_state()
    vid = vod_youtube_id(path)
    entry = _registry_entry(path, title=title, exhausted=False)
    registry = [r for r in state.get("vods", []) if r.get("id") != vid]
    registry.insert(0, entry)
    state["vods"] = registry
    scanned = [x for x in state.get("scanned_vods", []) if x != path.name]
    state["scanned_vods"] = scanned
    state["active_vod"] = path.name
    _save_state(state)
    return entry


def main() -> int:
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--batch-max", type=int, default=5)
    parser.add_argument("--no-resume-worker", action="store_true")
    args = parser.parse_args()

    vid = _video_id(args.url)
    if len(vid) != 11:
        print("bad youtube url", file=sys.stderr)
        return 1

    os.environ.setdefault("MLBB_ONLY_MODE", "1")
    os.environ.setdefault("MLBB_SEND_ENABLED", "1")
    os.environ.setdefault("MLBB_LEARNING_FIRST", "0")
    os.environ.setdefault("HIGHLIGHT_HEATMAP", "0")
    os.environ.setdefault("MLBB_VOD_BATCH_MAX", str(args.batch_max))
    os.environ.setdefault("MLBB_VOD_PROBE_LIMIT", "16")
    os.environ.setdefault("MLBB_VOD_VARIABLE_LENGTH", "1")

    env = {**os.environ, **load_env(ENV_PATH)}
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("TG missing", file=sys.stderr)
        return 1

    pause_worker()

    INBOX.mkdir(parents=True, exist_ok=True)
    dest = INBOX / f"yt_{vid}.mp4"
    try:
        if not dest.exists() or dest.stat().st_size < 500_000:
            send_message(token, chat_id, f"📥 Качаю VOD {vid}…")
            dest = download_one(args.url, INBOX, env)
        meta = fetch_video_meta(vid, env) or {}
        title = str(meta.get("title") or vid)
        entry = register_vod(dest, title=title)
        send_message(
            token,
            chat_id,
            f"✅ Скачал: {title[:100]}\n"
            f"Сканирую нарезки (~{entry.get('duration_min', '?')} мин)…",
        )

        registry = _ensure_registry(env)
        downloader = VodPipelineDownloader(env)
        labeled = labeled_ids()
        sent = _process_vod_segments(
            token,
            chat_id,
            dest,
            entry,
            labeled=labeled,
            probe_limit=int(env.get("MLBB_VOD_PROBE_LIMIT", "16")),
            downloader=downloader,
            registry=registry,
        )
        send_message(token, chat_id, f"Готово: отправлено {sent} кусков из {vid}")
        print(f"oneoff_done sent={sent} vod={vid}")
        return 0 if sent > 0 else 1
    except Exception as exc:
        send_message(token, chat_id, f"❌ Ошибка one-off {vid}: {exc}")
        log.exception("oneoff failed")
        return 1
    finally:
        if not args.no_resume_worker:
            resume_worker()


if __name__ == "__main__":
    raise SystemExit(main())
