#!/usr/bin/env python3
"""Send numbered raw TikTok sources to owner for gameplay yes/no labeling."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERO_ROOT = Path("/root/hero_datasets")
ENV_PATH = Path("/root/.video_bot.env")
BATCH_SIZE = int(os.environ.get("CALIBRATION_BATCH_SIZE", "15"))
BATCH_NUM = int(os.environ.get("CALIBRATION_BATCH_NUM", "2"))
STATE_PATH = Path("/root/data/mlbb/calibration_batch_sent.json")
# Heroes that passed owner review in batch 1 — prefer similar sources.
PREFERRED_HEROES = {
    h.strip().lower()
    for h in os.environ.get(
        "CALIBRATION_PREFERRED_HEROES",
        "moskov,franco,miya,chou,valentina",
    ).split(",")
    if h.strip()
}


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def ffprobe_duration(path: Path) -> float:
    result = subprocess.run(
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
        timeout=30,
    )
    try:
        return float((result.stdout or "0").strip())
    except ValueError:
        return 0.0


def send_video(token: str, chat_id: str, path: Path, caption: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendVideo"
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
        url,
    ]
    clean_env = {
        k: v
        for k, v in os.environ.items()
        if k.lower() not in {"http_proxy", "https_proxy", "all_proxy", "http_proxy", "https_proxy", "all_proxy"}
    }
    result = subprocess.run(cmd, capture_output=True, text=True, env=clean_env, timeout=620)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(result.stdout or result.stderr, file=sys.stderr)
        return False
    ok = bool(payload.get("ok"))
    if not ok:
        print(json.dumps(payload)[:400], file=sys.stderr)
    return ok


def main() -> int:
    os.environ.setdefault("SMART_REJECT_MUSIC_BED", "0")
    os.environ.setdefault("SMART_REQUIRE_UNIFORM_GAMEPLAY", "0")
    print("[calibration] scanning hero_datasets...", flush=True)
    sys.path.insert(0, "/usr/local/bin")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from gameplay_gate import (  # noqa: WPS433
        path_blocked_by_calibration,
        path_whitelisted_by_calibration,
        _band_overlay_text_score,
        _read_frame_at,
        heuristic_gameplay_score,
        profile_looks_like_mlbb_edit,
        segment_hud_frame_pass_rate,
        segment_looks_like_hero_showcase,
        segment_looks_like_promo_or_cinematic,
    )
    import cv2
    import numpy as np

    env = load_env(ENV_PATH)
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = os.environ.get("TG_CHAT_ID") or env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("TG_BOT_TOKEN or TG_CHAT_ID missing", file=sys.stderr)
        return 1

    sent_before: set[str] = set()
    if STATE_PATH.exists():
        try:
            sent_before = set(json.loads(STATE_PATH.read_text()).get("paths", []))
        except json.JSONDecodeError:
            sent_before = set()

    candidates: list[tuple[float, Path, str]] = []
    heroes_seen: set[str] = set()

    preferred_dirs = [HERO_ROOT / h for h in PREFERRED_HEROES if (HERO_ROOT / h).is_dir()]
    other_dirs = [d for d in sorted(HERO_ROOT.iterdir()) if d.is_dir() and d not in preferred_dirs]
    hero_dirs = preferred_dirs + other_dirs

    checked = 0
    for hero_dir in hero_dirs:
        hero = hero_dir.name
        per_hero = 0
        for path in sorted(hero_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
            if per_hero >= (25 if hero in PREFERRED_HEROES else 12):
                break
            per_hero += 1
            checked += 1
            if checked % 10 == 0:
                print(f"[calibration] checked {checked} files, pool={len(candidates)}", flush=True)
            if str(path) in sent_before or path_blocked_by_calibration(path):
                continue
            dur = ffprobe_duration(path)
            if dur < 18.0 or dur > 75.0:
                continue
            if profile_looks_like_mlbb_edit(path, sample_frames=5):
                continue
            score = heuristic_gameplay_score(path, sample_frames=4)
            if score < 0.58 and not path_whitelisted_by_calibration(path):
                continue
            mid = max(1.0, dur * 0.28)
            win = min(11.0, max(8.0, dur - mid - 0.5))
            if segment_looks_like_promo_or_cinematic(path, mid, win, sample_frames=4):
                continue
            if segment_looks_like_hero_showcase(path, mid, win, sample_frames=4):
                continue
            hud_rate = segment_hud_frame_pass_rate(path, mid, win, sample_frames=4)
            if hud_rate < 0.55:
                continue
            cap = cv2.VideoCapture(str(path))
            center_texts: list[float] = []
            if cap.isOpened():
                for t in np.linspace(mid, mid + win, num=4):
                    frame = _read_frame_at(cap, float(t))
                    if frame is not None:
                        center_texts.append(_band_overlay_text_score(frame, 0.28, 0.72))
            cap.release()
            comics = float(np.mean(center_texts)) if center_texts else 1.0
            if comics > 0.32:
                continue
            diversity = 0.12 if hero not in heroes_seen else 0.0
            hero_bonus = 0.18 if hero in PREFERRED_HEROES else 0.0
            whitelist_bonus = 0.25 if path_whitelisted_by_calibration(path) else 0.0
            rank = score + hud_rate * 0.35 - comics * 0.35 + diversity + hero_bonus + whitelist_bonus
            candidates.append((rank, path, hero))
            heroes_seen.add(hero)

    candidates.sort(key=lambda item: item[0], reverse=True)
    print(f"[calibration] candidates={len(candidates)}", flush=True)

    picked: list[tuple[Path, str]] = []
    used_heroes: set[str] = set()
    for _rank, path, hero in candidates:
        if len(picked) >= BATCH_SIZE:
            break
        if hero in used_heroes and len(used_heroes) < 8:
            continue
        picked.append((path, hero))
        used_heroes.add(hero)
    if len(picked) < BATCH_SIZE:
        for _rank, path, hero in candidates:
            if len(picked) >= BATCH_SIZE:
                break
            if any(p == path for p, _ in picked):
                continue
            picked.append((path, hero))

    if len(picked) < BATCH_SIZE and candidates:
        print(f"[calibration] only {len(picked)} diverse heroes, filling from pool", flush=True)
        for _rank, path, hero in candidates:
            if len(picked) >= BATCH_SIZE:
                break
            if path in {p for p, _ in picked}:
                continue
            picked.append((path, hero))

    if not picked:
        intro = (
            "Калибровка: не нашёл 15 сырых клипов с жёстким гейтом в hero_datasets. "
            "Нужен живой прокси или ваши загрузки gameplay."
        )
        subprocess.run(
            [
                "curl",
                "-sS",
                "-F",
                f"chat_id={chat_id}",
                "-F",
                f"text={intro}",
                f"https://api.telegram.org/bot{token}/sendMessage",
            ],
            env={k: v for k, v in os.environ.items() if "proxy" not in k.lower()},
            check=False,
        )
        return 1

    subprocess.run(
        [
            "curl",
            "-sS",
            "-F",
            f"chat_id={chat_id}",
            "-F",
            "text="
            + (
                f"Калибровка партия {BATCH_NUM}: {len(picked)} сырых TikTok (без нарезки). "
                "После вашей разметки №1 — фильтры ужесточены. "
                "Ответьте номерами, где НЕ геймплей: «2, 5 — удалить». "
                "Нумерация с 1 в этой партии."
            ),
            f"https://api.telegram.org/bot{token}/sendMessage",
        ],
        env={k: v for k, v in os.environ.items() if "proxy" not in k.lower()},
        check=False,
    )

    sent_paths: list[str] = []
    for idx, (path, hero) in enumerate(picked, start=1):
        caption = (
            f"партия{BATCH_NUM} · {idx}/{len(picked)} | калибровка\n"
            f"герой: {hero}\n"
            f"сырой TikTok, без нарезки\n"
            f"id: {path.stem}"
        )
        ok = send_video(token, chat_id, path, caption)
        print(f"[{idx}] {path.name} hero={hero} ok={ok}")
        if ok:
            sent_paths.append(str(path))
        time.sleep(1.2)

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(
            {"paths": list(sent_before) + sent_paths, "at": time.strftime("%Y-%m-%d %H:%M:%S")},
            indent=2,
        )
    )
    return 0 if len(sent_paths) == len(picked) else 1


if __name__ == "__main__":
    raise SystemExit(main())
