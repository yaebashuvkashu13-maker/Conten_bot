#!/usr/bin/env python3
"""
One-shot MLBB silver bootstrap: local datasets + YouTube Shorts + train + report.

Fills highlight_exemplars and youtube_shorts_index from:
  - /root/hero_datasets (owner TikTok gameplay)
  - /root/telegram_uploads/pending (131+ owner mp4)
  - /root/datasets/tiktok/mlbb (ranked CSV, gameplay-filtered)
  - owner 👍/👎 from vod_segment_labels + calibration_labels
  - YouTube Shorts search (aggressive)
  - viral_reference_ingest mobile_legends
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gameplay_gate import is_gameplay_video
from highlight_scorer import WINDOW_SEC, clear_exemplar_cache, score_candidate_window
from mlbb_calibration_store import (
    SHORTS_ROOT,
    rebuild_index_from_disk,
    repair_index,
    stats as cal_stats,
    upsert_candidate,
)
from viral_scorer import hook_score
from youtube_download import load_env, subprocess_env_no_proxy

REPO = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml"))
DATA_MLBB = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))
EXEMPLAR_ROOT = Path(
    os.environ.get("HIGHLIGHT_EXEMPLAR_ROOT", str(REPO / "data" / "highlight_exemplars"))
)
TELEGRAM_ROOT = Path("/root/telegram_uploads")
TIKTOK_ROOT = Path("/root/datasets/tiktok/mlbb")
HERO_ROOT = Path("/root/hero_datasets")
LOG_PATH = Path(os.environ.get("MLBB_SILVER_BOOTSTRAP_LOG", str(DATA_MLBB / "mlbb_silver_bootstrap.log")))

TEAMFIGHT_RE = re.compile(
    r"(team\s*fight|teamfight|savage|maniac|legendary|triple\s*kill|"
    r"quadra|penta|clutch|outplay|wipe|m7\b|esports|grand\s*final|"
    r"alter\s*ego|on\s*ic|rrq|blacklist|savage|kill\s*streak|"
    r"файт|тим\s*файт|savage|клатч)",
    re.I,
)
PROMO_RE = re.compile(
    r"(#ad\b|sponsored|giveaway|promo\b|free\s+diamond|skin\s+gratis|"
    r"log\s*in\s+mlbb|mailbox|official\s+event|tutorial|guide|tips|"
    r"funny|meme|intro|reaction|dance|cctv|wendy|allstar\s+party|"
    r"border|emote\s+gratis|login|trailer|collab\s+round|skin\s+showcase)",
    re.I,
)

SEARCH_QUERIES_EXTRA = (
    "mlbb savage teamfight shorts",
    "mlbb triple kill shorts",
    "mlbb mythic rank fight shorts",
    "mobile legends esports highlights shorts",
    "mlbb onic alter ego shorts",
    "mlbb chou savage shorts",
    "mlbb ling outplay shorts",
    "mlbb m7 highlights shorts",
)


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _ffmpeg_copy(src: Path, dest: Path, *, max_sec: float = 12.0) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 50_000:
        return True
    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        str(src),
        "-t",
        str(max_sec),
        "-c",
        "copy",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, check=False, timeout=120)
    if proc.returncode == 0 and dest.exists():
        return True
    try:
        shutil.copy2(src, dest)
        return dest.exists()
    except OSError:
        return False


def _score_local(path: Path) -> dict:
    os.environ.setdefault("HIGHLIGHT_HEATMAP", "0")
    os.environ.setdefault("HIGHLIGHT_USE_OWNER_ANCHORS", "0")
    dur = max(4.0, min(60.0, _ffprobe_duration(path)))
    window = min(WINDOW_SEC, dur * 0.85)
    m = score_candidate_window(path, 0.15, window, "mobile_legends")
    hook, hook_meta = hook_score(path, 0.15, "mobile_legends", duration_sec=window)
    combat = (
        max(0.0, float(m.clip_score)) * 0.35
        + float(m.minimap_delta) * 2.5
        + float(m.skill_delta) * 2.5
        + hook * 0.30
    )
    gate = bool(m.rule_pass and m.visual_pass)
    combined = combat + (0.15 if gate else 0.0)
    return {
        "score": round(combined, 4),
        "combat_score": round(combat, 4),
        "clip_score": round(float(m.clip_score), 4),
        "hook_score": round(hook, 4),
        "minimap_delta": round(float(m.minimap_delta), 4),
        "skill_delta": round(float(m.skill_delta), 4),
        "rule_pass": int(gate),
        "pass_reason": m.pass_reason or "",
        "hook_menu": hook_meta.get("menu_overlay", 0),
    }


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


def copy_exemplar(src: Path, label: str, prefix: str) -> bool:
    dest = EXEMPLAR_ROOT / "mobile_legends" / label / f"{prefix}{src.stem}.mp4"
    return _ffmpeg_copy(src, dest)


def ingest_local_mp4(
    path: Path,
    *,
    source: str,
    title: str = "",
    min_score: float = 0.08,
    lenient: bool = True,
) -> bool:
    if not path.exists() or path.stat().st_size < 200_000:
        return False
    dur = _ffprobe_duration(path)
    if dur < 4 or dur > 120:
        return False
    text = f"{title} {path.name}"
    if PROMO_RE.search(text):
        return False

    ok, gscore, reason = is_gameplay_video(path, csv_lookup={}, description=text)
    hard_reject = reason in ("promo_text", "csv_lookup")
    if not ok and hard_reject:
        return False

    feats = _score_local(path)
    if feats["score"] < min_score and not feats["rule_pass"] and not lenient:
        return False
    if not ok and lenient and feats["score"] < 0.04:
        return False

    vid = path.stem
    if vid.startswith("yt_"):
        vid = vid[3:]
    elif re.fullmatch(r"\d{10,22}", vid):
        pass
    else:
        vid = re.sub(r"[^a-zA-Z0-9_-]", "_", vid)[:48]

    dest = SHORTS_ROOT / f"local_{vid}.mp4"
    if not dest.exists():
        try:
            shutil.copy2(path, dest)
        except OSError:
            dest = path

    upsert_candidate(
        {
            "video_id": vid,
            "id": vid,
            "title": (title or path.stem)[:240],
            "path": str(dest),
            "url": "",
            "source": source,
            "view_count": 0,
            "duration": dur,
            **feats,
            "gameplay_pass": int(ok),
            "gameplay_score": round(float(gscore), 4),
            "gameplay_reason": reason,
            "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    return True


def bootstrap_hero_datasets(*, limit: int) -> int:
    n = 0
    for mp4 in sorted(HERO_ROOT.rglob("*.mp4"))[: limit * 2]:
        if copy_exemplar(mp4, "good", f"hero_{mp4.parent.name}_"):
            n += 1
        if n >= limit:
            break
    log(f"hero_datasets exemplars good={n}")
    return n


def bootstrap_telegram_pending(*, limit: int) -> int:
    n = 0
    roots = [TELEGRAM_ROOT / "pending", TELEGRAM_ROOT]
    seen: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        for mp4 in sorted(root.rglob("*.mp4")):
            key = str(mp4.resolve())
            if key in seen:
                continue
            seen.add(key)
            if mp4.stat().st_size < 500_000:
                continue
            if ingest_local_mp4(mp4, source="telegram_pending", title=mp4.name):
                n += 1
            if copy_exemplar(mp4, "good", "tg_"):
                pass
            if n >= limit:
                break
        if n >= limit:
            break
    log(f"telegram pending indexed={n}")
    return n


def bootstrap_tiktok_ranked(*, limit: int) -> int:
    ranked = REPO / "data" / "mlbb" / "current_mlbb_ranked_videos.csv"
    if not ranked.exists():
        ranked = Path("data/mlbb/current_mlbb_ranked_videos.csv")
    if not ranked.exists() or not TIKTOK_ROOT.exists():
        log("tiktok ranked skip: csv or root missing")
        return 0

    gameplay_ids: set[str] = set()
    gp_csv = DATA_MLBB / "gameplay_filter_latest.csv"
    if gp_csv.exists():
        with gp_csv.open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                raw = str(row.get("is_gameplay", row.get("gameplay_score", ""))).lower()
                if raw in ("1", "true", "yes") or (
                    raw.replace(".", "", 1).isdigit() and float(raw) >= 0.5
                ):
                    gameplay_ids.add(str(row.get("video_id", "")))

    rows: list[dict] = []
    with ranked.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            desc = str(row.get("description") or "")
            vid = str(row.get("video_id") or "")
            if PROMO_RE.search(desc):
                continue
            if gameplay_ids and vid not in gameplay_ids:
                if not TEAMFIGHT_RE.search(desc):
                    continue
            dur = float(row.get("duration") or 0)
            if dur < 8 or dur > 75:
                continue
            if TEAMFIGHT_RE.search(desc) or "willdominateyougg" in str(row.get("uploader", "")).lower():
                rows.append(row)

    rows.sort(key=lambda r: int(r.get("view_count") or 0), reverse=True)
    n = 0
    for row in rows[: limit * 3]:
        vid = str(row.get("video_id", ""))
        mp4 = TIKTOK_ROOT / f"{vid}.mp4"
        if not mp4.exists():
            for alt in TIKTOK_ROOT.glob(f"*{vid}*.mp4"):
                mp4 = alt
                break
        if not mp4.exists():
            continue
        if ingest_local_mp4(
            mp4,
            source="tiktok_ranked",
            title=str(row.get("description", ""))[:200],
            min_score=0.06,
        ):
            n += 1
        if copy_exemplar(mp4, "good", f"tt_{vid}_"):
            pass
        if n >= limit:
            break
    log(f"tiktok ranked indexed={n}")
    return n


def bootstrap_owner_labels_exemplars() -> tuple[int, int]:
    good = bad = 0
    for label_name, label in (("good", "good"), ("bad", "bad")):
        for path_key in (DATA_MLBB / "vod_segment_labels.json", DATA_MLBB / "calibration_labels.json"):
            if not path_key.exists():
                continue
            try:
                data = json.loads(path_key.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            for row in data.get(label_name, []):
                path = Path(str(row.get("path", "")))
                if not path.exists():
                    continue
                prefix = f"owner_{label_name}_"
                if copy_exemplar(path, label, prefix):
                    if label == "good":
                        good += 1
                    else:
                        bad += 1
    log(f"owner label exemplars good={good} bad={bad}")
    return good, bad


def bootstrap_quarantine_bad(*, limit: int = 8) -> int:
    n = 0
    for folder in (
        DATA_MLBB / "quarantine" / "promo",
        DATA_MLBB / "quarantine" / "ad",
    ):
        if not folder.exists():
            continue
        for mp4 in sorted(folder.glob("*.mp4"))[:limit]:
            if copy_exemplar(mp4, "bad", "promo_"):
                n += 1
    log(f"quarantine bad exemplars={n}")
    return n


def run_youtube_ingest(*, max_downloads: int) -> int:
    script = Path(__file__).resolve().parent / "mlbb_youtube_shorts_ingest.py"
    alt = Path("/usr/local/bin/mlbb_youtube_shorts_ingest.py")
    if alt.exists():
        script = alt
    env = {**os.environ, **load_env()}
    env["MLBB_INGEST_SKIP_IF_PENDING"] = "0"
    env["MLBB_CALIBRATION_LENIENT"] = "1"
    for key in list(env):
        if "proxy" in key.lower():
            env.pop(key, None)
    cmd = [
        sys.executable,
        str(script),
        "--max-downloads",
        str(max_downloads),
        "--max-per-query",
        "35",
        "--download-delay",
        "6",
        "--search-delay",
        "2",
        "--min-score",
        "0.08",
    ]
    log(f"youtube ingest start max_downloads={max_downloads}")
    proc = subprocess.run(cmd, env=subprocess_env_no_proxy(env), capture_output=True, text=True, timeout=7200)
    tail = (proc.stdout or "")[-2000:]
    log(f"youtube ingest rc={proc.returncode}\n{tail}")
    return proc.returncode


def run_viral_reference(*, max_download: int) -> int:
    script = Path("/usr/local/bin/viral_reference_ingest.py")
    if not script.exists():
        script = REPO / "scripts" / "viral_reference_ingest.py"
    if not script.exists():
        log("viral_reference_ingest missing")
        return 1
    env = dict(os.environ)
    env["CONTENT_BOT_REPO"] = str(REPO)
    env["VIRAL_REFERENCE_ROOT"] = "/root/datasets/viral_reference"
    cmd = [
        sys.executable,
        str(script),
        "--profile",
        "mobile_legends",
        "--max-download",
        str(max_download),
    ]
    log(f"viral_reference start max_download={max_download}")
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=7200)
    tail = (proc.stdout or "")[-1500:]
    log(f"viral_reference rc={proc.returncode}\n{tail}")
    return proc.returncode


def run_train() -> int:
    script = Path("/usr/local/bin/highlight_train.py")
    if not script.exists():
        script = REPO / "scripts" / "highlight_train.py"
    env = dict(os.environ)
    env.update(
        {
            "HIGHLIGHT_HEATMAP": "0",
            "HIGHLIGHT_USE_OWNER_ANCHORS": "0",
            "MLBB_TRAIN_MAX_EXEMPLARS": "300",
            "MLBB_USE_CLASSIFIER": "1",
            "CONTENT_BOT_REPO": str(REPO),
        }
    )
    proc = subprocess.run(
        [sys.executable, str(script), "--profile", "mobile_legends"],
        env=env,
        capture_output=True,
        text=True,
        timeout=3600,
    )
    log(f"highlight_train rc={proc.returncode} {(proc.stdout or '').strip()}")
    clear_exemplar_cache()
    return proc.returncode


def send_telegram_report(summary: dict) -> None:
    token = os.environ.get("TG_BOT_TOKEN", "")
    chat = os.environ.get("TG_CHAT_ID", "")
    if not token or not chat:
        return
    import urllib.parse
    import urllib.request

    text = (
        "🧠 MLBB silver bootstrap\n"
        f"hero={summary.get('hero', 0)} tg={summary.get('telegram', 0)} "
        f"tiktok={summary.get('tiktok', 0)}\n"
        f"exemplars 👍{summary.get('ex_good', 0)} 👎{summary.get('ex_bad', 0)}\n"
        f"shorts pending={summary.get('pending', 0)} index={summary.get('index', 0)}\n"
        f"train rc={summary.get('train_rc', '?')}\n"
        "Отправки включены. Ставь 👍/👎 на каждый клип — это главный сигнал."
    )
    url = (
        f"https://api.telegram.org/bot{token}/sendMessage?"
        + urllib.parse.urlencode({"chat_id": chat, "text": text})
    )
    try:
        urllib.request.urlopen(url, timeout=20)
    except Exception as exc:
        log(f"telegram report failed: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hero-limit", type=int, default=24)
    parser.add_argument("--telegram-limit", type=int, default=80)
    parser.add_argument("--tiktok-limit", type=int, default=40)
    parser.add_argument("--youtube-downloads", type=int, default=30)
    parser.add_argument("--viral-downloads", type=int, default=25)
    parser.add_argument("--skip-youtube", action="store_true")
    parser.add_argument("--skip-viral", action="store_true")
    parser.add_argument("--skip-train", action="store_true")
    parser.add_argument("--telegram", action="store_true")
    args = parser.parse_args()

    env_file = Path("/root/.video_bot.env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

    os.environ.setdefault("CONTENT_BOT_REPO", str(REPO))
    os.environ.setdefault("MLBB_DATA_ROOT", str(DATA_MLBB))
    os.environ.setdefault("HIGHLIGHT_HEATMAP", "0")
    SHORTS_ROOT.mkdir(parents=True, exist_ok=True)
    EXEMPLAR_ROOT.mkdir(parents=True, exist_ok=True)

    log("=== mlbb_silver_bootstrap start ===")
    repair_index()
    rebuild_index_from_disk()

    summary = {
        "hero": bootstrap_hero_datasets(limit=args.hero_limit),
        "telegram": bootstrap_telegram_pending(limit=args.telegram_limit),
        "tiktok": bootstrap_tiktok_ranked(limit=args.tiktok_limit),
    }
    ex_good, ex_bad = bootstrap_owner_labels_exemplars()
    summary["ex_good"] = ex_good + summary["hero"] + summary["telegram"]
    summary["ex_bad"] = ex_bad + bootstrap_quarantine_bad()

    if not args.skip_youtube and args.youtube_downloads > 0:
        run_youtube_ingest(max_downloads=args.youtube_downloads)

    if not args.skip_viral and args.viral_downloads > 0:
        run_viral_reference(max_download=args.viral_downloads)

    rebuild_index_from_disk()
    cs = cal_stats()
    summary["pending"] = cs.get("pending", 0)
    summary["index"] = cs.get("candidates", 0)

    if not args.skip_train:
        summary["train_rc"] = run_train()
    else:
        summary["train_rc"] = "skipped"

    log(f"=== done {json.dumps(summary, ensure_ascii=False)} ===")

    if args.telegram:
        send_telegram_report(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
