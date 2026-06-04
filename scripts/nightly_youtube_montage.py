#!/usr/bin/env python3
"""Nightly: find 1.5–3.5h MLBB YouTube streams/VOD, download, Smart Edit, send to owner."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ENV_FILE = Path("/root/.video_bot.env")
WORK_ROOT = Path("/root/data/mlbb/youtube_nightly")
INBOX = WORK_ROOT / "inbox"
STATE_FILE = WORK_ROOT / "state.json"
REPORT_FILE = WORK_ROOT / "last_report.json"
LOG_FILE = WORK_ROOT / "nightly.log"
PROCESSOR = Path("/usr/local/bin/smart_video_editor.py")
OUTPUT_DIR = Path("/root/videos")

SKIP_TITLE = re.compile(
    r"(giveaway|#short\b|shorts\b|tiktok\b|reaction only|official cinematic|"
    r"trailer\b|skin review|diamond giveaway|how to get)",
    re.I,
)

DEFAULT_QUERIES = [
    "Mobile Legends Bang Bang live stream full",
    "MLBB ranked gameplay full match",
    "Mobile Legends stream replay gameplay",
]


def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def setup_logging() -> None:
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"used_ids": [], "runs": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def save_report(payload: dict) -> None:
    payload["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    REPORT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def clean_proxy_env(env: dict[str, str]) -> dict[str, str]:
    out = dict(env)
    for key in list(out):
        if "proxy" in key.lower():
            out.pop(key, None)
    return out


def ytdlp_base(env: dict[str, str]) -> list[str]:
    return [
        "yt-dlp",
        "--impersonate",
        env.get("YTDLP_IMPERSONATE", "chrome-131"),
        "--no-warnings",
        "--no-progress",
    ]


def parse_youtube_id(url: str) -> str:
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(url.strip())
    if parsed.netloc.endswith("youtu.be"):
        return parsed.path.strip("/").split("/")[0][:11]
    if "/live/" in parsed.path:
        part = parsed.path.split("/live/", 1)[-1].split("/")[0].split("?")[0]
        if part:
            return part[:11]
    if "/shorts/" in parsed.path:
        return parsed.path.split("/shorts/", 1)[-1].split("/")[0][:11]
    return (parse_qs(parsed.query).get("v") or [""])[0][:11]


def fetch_video_meta(video_id: str, env: dict[str, str]) -> dict | None:
    url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = ytdlp_base(env) + ["-j", "--no-playlist", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return None
    duration = float(data.get("duration") or 0)
    title = str(data.get("title") or "")
    return {
        "id": video_id,
        "url": url,
        "duration": duration,
        "title": title,
        "uploader": str(data.get("uploader") or data.get("channel") or ""),
    }


def discover_candidates(
    env: dict[str, str],
    *,
    queries: list[str] | None = None,
    min_sec: float | None = None,
    max_sec: float | None = None,
    search_limit: int | None = None,
) -> list[dict]:
    if queries is None:
        queries = [
            q.strip()
            for q in env.get("YOUTUBE_NIGHTLY_QUERIES", ",".join(DEFAULT_QUERIES)).split(",")
            if q.strip()
        ]
    search_limit = int(search_limit or env.get("YOUTUBE_NIGHTLY_SEARCH_LIMIT", "15"))
    min_sec = float(min_sec if min_sec is not None else env.get("YOUTUBE_NIGHT_MIN_SEC", "5400"))
    max_sec = float(max_sec if max_sec is not None else env.get("YOUTUBE_NIGHT_MAX_SEC", "12600"))
    seen: set[str] = set()
    out: list[dict] = []

    for query in queries:
        cmd = ytdlp_base(env) + [
            f"ytsearch{search_limit}:{query}",
            "--flat-playlist",
            "--print",
            "%(id)s",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            logging.warning("search timeout: %s", query)
            continue
        if result.returncode != 0:
            logging.warning("search failed %s: %s", query, (result.stderr or "")[:200])
            continue
        for line in result.stdout.splitlines():
            vid = line.strip()
            if not vid or vid in seen:
                continue
            seen.add(vid)
            meta = fetch_video_meta(vid, env)
            if not meta:
                continue
            if SKIP_TITLE.search(meta["title"]):
                logging.info("skip title=%s", meta["title"][:80])
                continue
            dur = meta["duration"]
            if dur < min_sec or dur > max_sec:
                logging.info(
                    "skip duration %.0fs id=%s title=%s",
                    dur,
                    vid,
                    meta["title"][:60],
                )
                continue
            out.append(meta)
            logging.info(
                "candidate %.0f min id=%s %s",
                dur / 60,
                vid,
                meta["title"][:70],
            )
    out.sort(key=lambda item: abs(item["duration"] - 7200))  # prefer ~2h
    return out


def candidates_from_urls(
    urls: list[str],
    env: dict[str, str],
    *,
    min_sec: float,
    max_sec: float,
    used_ids: set[str] | None = None,
) -> list[dict]:
    """Pinned YouTube URLs when search finds nothing (or owner-provided VOD)."""
    used_ids = used_ids or set()
    out: list[dict] = []
    for raw in urls:
        url = str(raw).strip()
        if not url:
            continue
        vid = parse_youtube_id(url)
        if not vid:
            logging.warning("bad youtube url: %s", url[:80])
            continue
        if vid in used_ids:
            logging.info("skip fallback used id=%s", vid)
            continue
        meta = fetch_video_meta(vid, env)
        if not meta:
            logging.warning("fallback meta failed id=%s", vid)
            continue
        if SKIP_TITLE.search(meta["title"]):
            logging.info("skip fallback title=%s", meta["title"][:80])
            continue
        dur = meta["duration"]
        if dur < min_sec or dur > max_sec:
            logging.info(
                "skip fallback duration %.0fs id=%s title=%s",
                dur,
                vid,
                meta["title"][:60],
            )
            continue
        out.append(meta)
        logging.info("fallback candidate %.0f min id=%s %s", dur / 60, vid, meta["title"][:70])
    out.sort(key=lambda item: item["duration"], reverse=True)
    return out


def pick_candidate(candidates: list[dict], used_ids: set[str]) -> dict | None:
    for item in candidates:
        if item["id"] not in used_ids:
            return item
    return None


def download_video(meta: dict, env: dict[str, str]) -> Path:
    sys.path.insert(0, "/usr/local/bin")
    from youtube_download import download_one  # noqa: WPS433

    INBOX.mkdir(parents=True, exist_ok=True)
    return download_one(meta["url"], INBOX, env)


def run_montage(source: Path, env: dict[str, str], chat_id: str) -> tuple[int, str]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".queue.txt") as tmp:
        tmp.write(f"{source.resolve()}|MLBB|{chat_id}\n")
        queue_path = tmp.name

    run_env = os.environ.copy()
    for key, value in env.items():
        run_env.setdefault(key, value)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from montage_env import profile_montage_env

    run_env = clean_proxy_env(run_env)
    run_env.update(profile_montage_env("mobile_legends"))
    run_env.update(
        {
            "QUEUE_FILE": queue_path,
            "MAX_SOURCES": "1",
            "SINGLE_SOURCE_MODE": "1",
            "SEND_TELEGRAM": "1",
            "TG_CHAT_ID": chat_id,
            "TG_BOT_TOKEN": env.get("TG_BOT_TOKEN", ""),
            "OUTPUT_DIR": str(OUTPUT_DIR),
            "DEFAULT_GAME_PROFILE": "mobile_legends",
            "QUEUE_GAME_PROFILE": "mobile_legends",
            "STRICT_GAMEPLAY": env.get("YOUTUBE_NIGHTLY_STRICT", "0"),
            "SMART_MAKE_TIMEOUT_SEC": env.get("SMART_MAKE_TIMEOUT_SEC", "10800"),
            "SMART_MAKE_TIMEOUT_MAX_SEC": env.get("SMART_MAKE_TIMEOUT_MAX_SEC", "14400"),
        }
    )
    timeout = int(float(run_env.get("SMART_MAKE_TIMEOUT_MAX_SEC", "14400")))
    try:
        completed = subprocess.run(
            [sys.executable, str(PROCESSOR)],
            env=run_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        tail = (completed.stderr or completed.stdout or "")[-800:]
        return completed.returncode, tail
    finally:
        Path(queue_path).unlink(missing_ok=True)


def send_text(env: dict[str, str], chat_id: str, text: str) -> None:
    token = env.get("TG_BOT_TOKEN", "")
    if not token or not chat_id:
        return
    clean = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    subprocess.run(
        [
            "curl",
            "-sS",
            "-m",
            "60",
            "-F",
            f"chat_id={chat_id}",
            "-F",
            f"text={text[:3900]}",
            f"https://api.telegram.org/bot{token}/sendMessage",
        ],
        check=False,
        capture_output=True,
        env=clean,
    )


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Nightly YouTube MLBB montage")
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--url", help="Skip search; use this YouTube URL")
    args = parser.parse_args()

    setup_logging()
    env = load_env()
    chat_id = env.get("TG_CHAT_ID", "")
    if not env.get("TG_BOT_TOKEN") or not chat_id:
        logging.error("TG_BOT_TOKEN / TG_CHAT_ID missing")
        return 1

    state = load_state()
    used = set(state.get("used_ids") or [])

    if args.url:
        vid = parse_youtube_id(args.url)
        meta = fetch_video_meta(vid, env)
        if not meta:
            logging.error("could not fetch meta for %s", args.url)
            return 1
        candidates = [meta]
    else:
        logging.info("discovering YouTube streams…")
        candidates = discover_candidates(env)
        save_report(
            {
                "phase": "discover",
                "candidates": [
                    {
                        "id": c["id"],
                        "duration_min": round(c["duration"] / 60, 1),
                        "title": c["title"][:120],
                    }
                    for c in candidates[:8]
                ],
            }
        )
        if args.discover_only:
            print(json.dumps(candidates[:5], ensure_ascii=False, indent=2))
            return 0

    pick = pick_candidate(candidates, used) if not args.url else candidates[0]
    if not pick:
        msg = "Ночной YouTube: не нашёл новых стримов 1.5–3.5 ч (все уже обработаны или пустой поиск)."
        logging.warning(msg)
        save_report({"ok": False, "error": "no_candidate", "message": msg})
        send_text(env, chat_id, f"🌙 {msg}")
        return 0

    logging.info("picked %s (%.0f min) %s", pick["id"], pick["duration"] / 60, pick["title"][:80])
    send_text(
        env,
        chat_id,
        f"🌙 Ночной пайплайн: качаю YouTube (~{int(pick['duration'] // 60)} мин)\n"
        f"{pick['title'][:200]}\n{pick['url']}",
    )

    try:
        path = download_video(pick, env)
    except Exception as exc:
        logging.exception("download failed")
        save_report({"ok": False, "error": "download", "detail": str(exc), "pick": pick})
        send_text(env, chat_id, f"🌙 YouTube: не скачалось — {exc}")
        return 1

    logging.info("downloaded %s", path)
    send_text(
        env,
        chat_id,
        f"🌙 Скачано. Запускаю нарезку (может занять до ~{int(pick['duration'] // 120)} мин)…",
    )

    code, tail = run_montage(path, env, chat_id)
    used.add(pick["id"])
    state["used_ids"] = list(used)[-200:]
    state.setdefault("runs", []).append(
        {
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "id": pick["id"],
            "title": pick["title"][:120],
            "rc": code,
        }
    )
    state["runs"] = state["runs"][-30:]
    save_state(state)

    report = {
        "ok": code == 0,
        "video_id": pick["id"],
        "title": pick["title"],
        "url": pick["url"],
        "source": str(path),
        "duration_min": round(pick["duration"] / 60, 1),
        "montage_rc": code,
        "log_tail": tail[-500:],
    }
    save_report(report)

    if code == 0:
        try:
            sys.path.insert(0, "/usr/local/bin")
            from publish_ready_montage import latest_in_output_dir, register  # noqa: WPS433

            montage = latest_in_output_dir()
            if montage:
                manifest = register(
                    montage,
                    source_url=pick["url"],
                    title=pick["title"],
                )
                report["montage_path"] = manifest.get("path")
        except Exception as exc:
            logging.warning("publish manifest: %s", exc)
        send_text(
            env,
            chat_id,
            f"🌅 Ночная нарезка готова (источник ~{report['duration_min']} мин).\n"
            f"Ролик выше в чате. Источник: {pick['url']}\n"
            f"Для n8n/TikTok/IG: /root/data/mlbb/publish/latest_montage.json",
        )
        logging.info("montage ok")
        return 0

    send_text(
        env,
        chat_id,
        f"🌙 Нарезка не собралась (код {code}). Источник сохранён.\n"
        f"Возможно мало чистого геймплея в стриме.\n{pick['url']}",
    )
    logging.error("montage failed rc=%s tail=%s", code, tail[-300:])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
