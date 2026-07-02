#!/usr/bin/env python3
"""Multi-game YouTube Shorts ingest + owner calibration feed (3 per game)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from calibration_dislike_reasons import (
    feedback_ack_message,
    labeled_keyboard_markup,
    normalize_game,
    shorts_keyboard_markup,
)
from highlight_scorer import normalize_profile
from mlbb_telegram_video import send_calibration_video
from youtube_download import load_env, subprocess_env_no_proxy, ytdlp_cmd, ytdlp_extra_args
from youtube_game_prefs import has_metro_royale, russian_score

REPO = Path(__file__).resolve().parent.parent
if os.environ.get("CONTENT_BOT_REPO"):
    _cand = Path(os.environ["CONTENT_BOT_REPO"])
    if (_cand / "config" / "shorts_calibration_games.yaml").exists():
        REPO = _cand
CONFIG_PATH = Path(os.environ.get("SHORTS_GAMES_CONFIG", str(REPO / "config" / "shorts_calibration_games.yaml")))
EXEMPLAR_ROOT = Path(os.environ.get("HIGHLIGHT_EXEMPLAR_ROOT", str(REPO / "data" / "highlight_exemplars")))
HQ_FORMAT = "bv*[height<=1080][height>=480]+ba/b[height<=1080]/best"

TITLE_BLOCK = re.compile(
    r"(giveaway|#ad\b|sponsored|tutorial|guide|tips|reaction|meme|funny|montage|compilation)",
    re.I,
)


@dataclass
class GameSpec:
    id: str
    profile: str
    label: str
    queries: tuple[str, ...]
    prefer_russian: bool = False
    legacy_module: str = ""
    batch: int = 3


def load_games() -> list[GameSpec]:
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    batch_default = int(raw.get("batch_default", 3))
    out: list[GameSpec] = []
    for row in raw.get("games", []):
        out.append(
            GameSpec(
                id=str(row["id"]),
                profile=str(row.get("profile", row["id"])),
                label=str(row.get("label", row["id"])),
                queries=tuple(row.get("queries") or ()),
                prefer_russian=bool(row.get("prefer_russian")),
                legacy_module=str(row.get("legacy_module", "")),
                batch=int(row.get("batch", batch_default)),
            )
        )
    return out


def _game_spec(game_id: str) -> GameSpec | None:
    gid = normalize_game(game_id)
    for g in load_games():
        if g.id == gid:
            return g
    return None


def _paths(game_id: str) -> dict[str, Path]:
    gid = normalize_game(game_id)
    if gid == "mlbb":
        from mlbb_calibration_store import (
            FEED_SENT_PATH,
            INDEX_PATH,
            LABELS_PATH,
            SHORTS_ROOT,
        )

        return {
            "shorts": SHORTS_ROOT,
            "index": INDEX_PATH,
            "labels": LABELS_PATH,
            "sent": FEED_SENT_PATH,
        }
    root = Path(os.environ.get(f"{gid.upper()}_DATA_ROOT", f"/root/data/{gid}"))
    ds = Path(os.environ.get(f"{gid.upper()}_SHORTS_ROOT", f"/root/datasets/{gid}/youtube_shorts"))
    return {
        "shorts": ds,
        "index": root / "shorts_index.json",
        "labels": root / "shorts_calibration_labels.json",
        "sent": root / "shorts_feed_sent.json",
    }


def _read_json(path: Path, default: dict | list) -> dict | list:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _title_ok(spec: GameSpec, title: str) -> bool:
    t = title or ""
    if TITLE_BLOCK.search(t):
        return False
    low = t.lower()
    if spec.id == "pubg":
        return has_metro_royale({"title": t}) and re.search(r"pubg|пабг|metro|метро", low)
    if spec.id == "standoff":
        return bool(re.search(r"standoff|стендоф", t, re.I))
    if spec.id == "genshin":
        return bool(re.search(r"genshin|геншин", t, re.I))
    if spec.id == "wot":
        return bool(re.search(r"world of tanks|\bwot\b|танк", t, re.I))
    if spec.id == "mlbb":
        return bool(re.search(r"mlbb|mobile legends|mobile legend", t, re.I))
    return True


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


def _search_shorts(query: str, *, limit: int, env: dict[str, str]) -> list[dict]:
    cmd = ytdlp_cmd(env) + [
        f"ytsearch{limit}:{query}",
        "--flat-playlist",
        "--print",
        "%(id)s|%(title)s|%(duration)s|%(view_count)s|%(upload_date)s",
    ]
    cmd += ytdlp_extra_args(env)
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=subprocess_env_no_proxy(env),
    )
    rows: list[dict] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split("|", 4)
        if len(parts) < 2:
            continue
        vid = parts[0][:11]
        if len(vid) != 11:
            continue
        try:
            dur = float(parts[2]) if len(parts) > 2 else 0.0
        except ValueError:
            dur = 0.0
        rows.append(
            {
                "video_id": vid,
                "title": parts[1][:200],
                "duration": dur,
                "view_count": int(float(parts[3])) if len(parts) > 3 and parts[3] else 0,
                "upload_date": parts[4] if len(parts) > 4 else "",
                "url": f"https://www.youtube.com/shorts/{vid}",
                "search_query": query,
            }
        )
    return rows


def _download_short(vid: str, dest: Path, env: dict[str, str]) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://www.youtube.com/shorts/{vid}"
    cmd = ytdlp_cmd(env) + ["-f", HQ_FORMAT, "-o", str(dest), url]
    cmd += ytdlp_extra_args(env)
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        timeout=int(env.get("YOUTUBE_SHORTS_TIMEOUT", "600")),
        env=subprocess_env_no_proxy(env),
    )
    return proc.returncode == 0 and dest.exists() and dest.stat().st_size > 10_000


def ingest_game(game_id: str, *, max_downloads: int = 3, env: dict[str, str] | None = None) -> int:
    spec = _game_spec(game_id)
    if not spec:
        return 0
    if spec.legacy_module == "mlbb":
        from mlbb_youtube_shorts_ingest import main as mlbb_ingest_main

        os.environ.setdefault("MLBB_INGEST_MAX_DOWNLOADS", str(max_downloads))
        mlbb_ingest_main()
        return max_downloads

    env = env or load_env(Path("/root/.video_bot.env"))
    paths = _paths(spec.id)
    paths["shorts"].mkdir(parents=True, exist_ok=True)
    index = _read_json(paths["index"], {"candidates": []})
    if not isinstance(index, dict):
        index = {"candidates": []}
    candidates: list[dict] = list(index.get("candidates", []))
    labeled = {c.get("video_id") for c in candidates if c.get("owner_label") in ("yes", "no")}
    saved = 0
    for query in spec.queries:
        if saved >= max_downloads:
            break
        for row in _search_shorts(f"{query} #shorts", limit=15, env=env):
            if saved >= max_downloads:
                break
            vid = row["video_id"]
            if vid in labeled or any(c.get("video_id") == vid for c in candidates):
                continue
            if row.get("duration", 0) > 60 or row.get("duration", 0) < 3:
                continue
            if not _title_ok(spec, row.get("title", "")):
                continue
            if spec.prefer_russian and russian_score(row) < 0.08:
                continue
            dest = paths["shorts"] / f"yt_{vid}.mp4"
            if not dest.exists():
                if not _download_short(vid, dest, env):
                    continue
                time.sleep(float(env.get("YTDLP_SLEEP_INTERVAL", "3")))
            dur = _ffprobe_duration(dest)
            if dur > 60 or dur < 3:
                continue
            candidates.append(
                {
                    **row,
                    "path": str(dest),
                    "game": spec.id,
                    "profile": spec.profile,
                    "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            saved += 1
    index["candidates"] = candidates
    index["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_json(paths["index"], index)
    return saved


def _pending(spec: GameSpec) -> list[dict]:
    if spec.legacy_module == "mlbb":
        from mlbb_calibration_store import pending_candidates, repair_index

        repair_index()
        return pending_candidates(limit=spec.batch * 3, repair=True)[: spec.batch]

    paths = _paths(spec.id)
    index = _read_json(paths["index"], {"candidates": []})
    labels = _read_json(paths["labels"], {"feedback": []})
    sent = _read_json(paths["sent"], {"ids": []})
    sent_ids = set(sent.get("ids", [])) if isinstance(sent, dict) else set()
    feedback_ids = {
        str(f.get("video_id", ""))
        for f in labels.get("feedback", [])
        if isinstance(labels, dict)
    }
    out: list[dict] = []
    for row in index.get("candidates", []) if isinstance(index, dict) else []:
        vid = str(row.get("video_id", ""))
        if not vid or vid in sent_ids or vid in feedback_ids:
            continue
        path = Path(str(row.get("path", "")))
        if not path.exists():
            continue
        out.append(row)
        if len(out) >= spec.batch:
            break
    return out


def apply_shorts_label(
    game_id: str,
    video_id: str,
    *,
    is_good: bool,
    reason: str = "",
    by_chat: str = "",
) -> tuple[bool, str]:
    spec = _game_spec(game_id)
    if not spec:
        return False, f"unknown_game:{game_id}"
    if spec.legacy_module == "mlbb":
        from mlbb_calibration_store import apply_owner_label

        ok, _ = apply_owner_label(video_id, is_good=is_good, reason=reason, by_chat=by_chat)
        return ok, feedback_ack_message("mlbb", is_good=is_good, reason=reason)

    paths = _paths(spec.id)
    index = _read_json(paths["index"], {"candidates": []})
    row = next(
        (c for c in index.get("candidates", []) if c.get("video_id") == video_id),
        None,
    )
    if not row:
        return False, f"unknown_id:{video_id}"
    path = Path(str(row.get("path", "")))
    if not path.exists():
        return False, f"file_missing:{video_id}"

    labels = _read_json(paths["labels"], {"good": [], "bad": [], "feedback": []})
    entry = {
        "video_id": video_id,
        "path": str(path),
        "title": row.get("title", ""),
        "game": spec.id,
        "profile": spec.profile,
        "reason": reason,
        "weight": 1.0 if is_good else 1.2,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "by_chat": by_chat,
        "source": "youtube_shorts",
    }
    labels.setdefault("feedback", []).append({**entry, "owner_label": "yes" if is_good else "no"})
    bucket = "good" if is_good else "bad"
    labels.setdefault(bucket, []).append(entry)
    labels["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_json(paths["labels"], labels)

    ex_dir = EXEMPLAR_ROOT / normalize_profile(spec.profile) / bucket
    ex_dir.mkdir(parents=True, exist_ok=True)
    dest = ex_dir / f"shorts_{video_id}.mp4"
    try:
        subprocess.run(["cp", "-f", str(path), str(dest)], check=False, timeout=30)
    except OSError:
        pass

    owner_path = REPO / "data" / f"{spec.profile}_owner_labels.json"
    if owner_path.parent.exists():
        data = _read_json(owner_path, {"videos": {}})
        if isinstance(data, dict):
            videos = data.setdefault("videos", {})
            rows = list(videos.get(video_id, []))
            rows.append(
                {
                    "time_sec": 0.0,
                    "label": bucket,
                    "source": "youtube_shorts",
                    "note": reason or "shorts_calibration",
                    "weight": entry["weight"],
                }
            )
            videos[video_id] = rows
            _write_json(owner_path, data)

    for c in index.get("candidates", []):
        if c.get("video_id") == video_id:
            c["owner_label"] = "yes" if is_good else "no"
            c["reason"] = reason
    _write_json(paths["index"], index)
    return True, feedback_ack_message(spec.id, is_good=is_good, reason=reason)


def feed_game(
    game_id: str,
    *,
    token: str,
    chat_id: str,
    env: dict[str, str] | None = None,
) -> int:
    spec = _game_spec(game_id)
    if not spec:
        return 0
    env = env or load_env(Path("/root/.video_bot.env"))

    if spec.legacy_module == "mlbb":
        from mlbb_calibration_feed import main as mlbb_feed_main

        os.environ["MLBB_CALIBRATION_BATCH"] = str(spec.batch)
        mlbb_feed_main()
        return spec.batch

    ingest_game(spec.id, max_downloads=spec.batch, env=env)
    pending = _pending(spec)
    sent = 0
    paths = _paths(spec.id)
    sent_data = _read_json(paths["sent"], {"ids": []})
    sent_ids = set(sent_data.get("ids", [])) if isinstance(sent_data, dict) else set()

    for i, row in enumerate(pending, 1):
        vid = str(row.get("video_id", ""))
        path = Path(str(row.get("path", "")))
        caption = (
            f"{spec.label} калибровка {i}/{len(pending)}\n"
            f"{row.get('title', '')[:120]}\n"
            f"{row.get('url', '')}\n"
            f"#id {vid}\n"
            f"👍 ок / 👎 не ок — учим бота"
        )
        ok = send_calibration_video(
            token,
            chat_id,
            path,
            caption,
            reply_markup=shorts_keyboard_markup(vid, game=spec.id),
        )
        if ok:
            sent_ids.add(vid)
            sent += 1
        time.sleep(1.5)

    if isinstance(sent_data, dict):
        sent_data["ids"] = sorted(sent_ids)
        sent_data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _write_json(paths["sent"], sent_data)
    return sent


def feed_all_games(*, token: str, chat_id: str) -> dict[str, int]:
    results: dict[str, int] = {}
    for spec in load_games():
        results[spec.id] = feed_game(spec.id, token=token, chat_id=chat_id)
    return results


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--game", default="all")
    parser.add_argument("--ingest-only", action="store_true")
    args = parser.parse_args()
    env = load_env(Path("/root/.video_bot.env"))
    token = env.get("TG_BOT_TOKEN", "").strip()
    chat_id = env.get("TG_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("TG_BOT_TOKEN / TG_CHAT_ID missing")
        return 1
    if args.ingest_only:
        for spec in load_games():
            if args.game not in ("all", spec.id):
                continue
            n = ingest_game(spec.id, max_downloads=spec.batch, env=env)
            print(f"ingest {spec.id}: {n}")
        return 0
    if args.game == "all":
        res = feed_all_games(token=token, chat_id=chat_id)
        print(json.dumps(res, ensure_ascii=False))
        return 0
    n = feed_game(args.game, token=token, chat_id=chat_id)
    print(f"feed {args.game}: sent={n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
