#!/usr/bin/env python3
"""
Parallel MLBB TikTok harvester — not only ranked CSV.

Sources: CSV tables, official/community channels, hashtags, yt-dlp search queries.
Saves gameplay under out_root/; other clips -> out_root/non_gameplay/ (still kept for training).
"""

from __future__ import annotations

import argparse
import csv
import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from gameplay_gate import extract_video_id, is_gameplay_video, load_csv_lookup

STATE_PATH = Path("/root/data/mlbb/download_state.json")
QUEUE_PATH = Path("/root/data/mlbb/mass_download_queue.jsonl")
RANKED_CSV = Path("/root/data/mlbb/current_mlbb_ranked_videos.csv")
GAMEPLAY_CSV = Path("/root/data/mlbb/gameplay_filter_latest.csv")
DEFAULT_OUT = Path("/root/datasets/tiktok/mlbb")
LOG_PATH = Path("/root/data/mlbb/mass_download.log")

MLBB_SEEDS = [
    "https://www.tiktok.com/@mlbbttofficial",
    "https://www.tiktok.com/@mobilelegends_id",
    "https://www.tiktok.com/@mlbbesports_official",
    "https://www.tiktok.com/@willdominateyougg",
    "https://www.tiktok.com/@mlbb_top_creation",
    "https://www.tiktok.com/@mlbbindo",
    "https://www.tiktok.com/@mlbbphilippines",
    "https://www.tiktok.com/tag/mobilelegends",
    "https://www.tiktok.com/tag/mlbb",
    "https://www.tiktok.com/tag/mlbbhighlights",
    "https://www.tiktok.com/tag/hayabusa",
    "https://www.tiktok.com/tag/mlbbmoments",
    "https://www.tiktok.com/tag/mlbbindonesia",
    "https://www.tiktok.com/tag/fannymlbb",
    "https://www.tiktok.com/tag/gusion",
    "https://www.tiktok.com/tag/lingmlbb",
    "https://www.tiktok.com/tag/choumllbb",
]

# yt-dlp TikTok search (works when proxy + cookies are healthy)
MLBB_SEARCHES = [
    "mobile legends gameplay",
    "mlbb highlights",
    "mlbb rank push",
    "mlbb savage",
    "mlbb hayabusa",
    "mlbb fanny",
    "mlbb mythic",
    "mobile legends bang bang",
]

_stats_lock = threading.Lock()
_stats = {
    "queued": 0,
    "attempted": 0,
    "saved_gameplay": 0,
    "saved_other": 0,
    "failed": 0,
    "rejected": 0,
    "skipped_exists": 0,
}


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


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


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"downloaded_ids": [], "rejected_ids": [], "seen_urls": []}
    return json.loads(STATE_PATH.read_text())


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["downloaded_ids"] = list(state.get("downloaded_ids", []))[-30000:]
    state["rejected_ids"] = list(state.get("rejected_ids", []))[-30000:]
    state["seen_urls"] = list(state.get("seen_urls", []))[-80000:]
    state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def count_mp4_on_disk(out_root: Path) -> int:
    if not out_root.exists():
        return 0
    return sum(1 for _ in out_root.rglob("*.mp4"))


def slug_from_url(url: str) -> str:
    path = urlparse(url).path.strip("/").split("/")
    if len(path) >= 2 and path[0].startswith("@"):
        return path[0].lstrip("@").replace(".", "_")
    if path and path[0] == "tag":
        return f"tag_{path[1]}"
    return "misc"


def dest_for(url: str, out_root: Path) -> Path:
    vid = extract_video_id(Path(url), url) or str(abs(hash(url)))
    return out_root / slug_from_url(url) / f"{vid}.mp4"


def urls_from_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    items: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            url = (row.get("webpage_url") or "").strip()
            if "tiktok.com" in url and "/video/" in url:
                items.append(
                    {
                        "url": url,
                        "video_id": str(row.get("video_id") or extract_video_id(Path(url), "")),
                        "description": row.get("description", ""),
                        "source": path.name,
                    }
                )
    return items


def _ytdlp_base(proxy: str) -> list[str]:
    return ["yt-dlp", "--proxy", proxy, "--no-warnings"]


def urls_from_seed(seed: str, proxy: str, max_entries: int = 500) -> list[dict]:
    cmd = _ytdlp_base(proxy) + [
        "--flat-playlist",
        "--dump-single-json",
        "--playlist-end",
        str(max_entries),
        seed,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        return []
    if result.returncode != 0:
        log(f"seed failed {seed}: {(result.stderr or '')[:200]}")
        return []
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    return _entries_from_payload(payload, f"seed:{seed}")


def urls_from_search(query: str, proxy: str, max_entries: int = 200) -> list[dict]:
    # TikTok search via yt-dlp prefix (falls back gracefully if unsupported)
    target = f"tiktoksearch{max_entries}:{query}"
    cmd = _ytdlp_base(proxy) + ["--flat-playlist", "--dump-single-json", target]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        return []
    if result.returncode != 0:
        return []
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    return _entries_from_payload(payload, f"search:{query}")


def _entries_from_payload(payload: dict, source: str) -> list[dict]:
    entries = payload.get("entries") or [payload]
    items: list[dict] = []
    for entry in entries:
        if not entry:
            continue
        url = entry.get("webpage_url") or entry.get("url") or ""
        if not url and entry.get("id"):
            uploader = entry.get("uploader") or entry.get("channel") or "user"
            url = f"https://www.tiktok.com/@{uploader}/video/{entry['id']}"
        if "tiktok.com" not in url:
            continue
        if "/video/" not in url and entry.get("id"):
            url = f"https://www.tiktok.com/video/{entry['id']}"
        if "/video/" not in url:
            continue
        items.append(
            {
                "url": url,
                "video_id": str(entry.get("id") or extract_video_id(Path(url), "")),
                "description": str(entry.get("description") or entry.get("title") or ""),
                "source": source,
            }
        )
    return items


def append_queue_line(item: dict) -> None:
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with QUEUE_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, ensure_ascii=False) + "\n")


def discover_items(
    proxy: str,
    state: dict,
    target: int,
    seen_urls: set[str],
) -> list[dict]:
    downloaded = set(state.get("downloaded_ids", []))
    rejected = set(state.get("rejected_ids", []))
    queue_items: list[dict] = []

    def add(item: dict) -> None:
        url = item["url"]
        vid = item.get("video_id") or ""
        if url in seen_urls:
            return
        if vid and (vid in downloaded or vid in rejected):
            return
        seen_urls.add(url)
        queue_items.append(item)
        append_queue_line(item)

    for item in urls_from_csv(RANKED_CSV):
        add(item)
        if len(queue_items) >= target:
            return queue_items[:target]

    for item in urls_from_csv(GAMEPLAY_CSV):
        add(item)
        if len(queue_items) >= target:
            return queue_items[:target]

    for query in MLBB_SEARCHES:
        if len(queue_items) >= target * 2:
            break
        log(f"search {query!r}")
        for item in urls_from_search(query, proxy, max_entries=250):
            add(item)
        time.sleep(1)

    for seed in MLBB_SEEDS:
        if len(queue_items) >= target * 2:
            break
        log(f"scanning seed {seed}")
        for item in urls_from_seed(seed, proxy, max_entries=450):
            add(item)
        time.sleep(2)

    state["seen_urls"] = list(set(state.get("seen_urls", [])) | seen_urls)[-80000:]
    return queue_items[:target]


def download_file(url: str, dest: Path, proxy: str) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 80_000:
        return True
    partial = dest.with_suffix(".mp4.part")
    cmd = _ytdlp_base(proxy) + [
        "--no-playlist",
        "--no-write-thumbnail",
        "--merge-output-format",
        "mp4",
        "-f",
        "best[height<=720]/best",
        "-o",
        str(partial),
        url,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=300)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        partial.unlink(missing_ok=True)
        return False
    if partial.exists():
        partial.replace(dest)
    return dest.exists() and dest.stat().st_size > 80_000


def process_item(
    item: dict,
    out_root: Path,
    proxy: str,
    lookup: dict,
    strict_csv: bool,
    state: dict,
    state_lock: threading.Lock,
) -> None:
    url = item["url"]
    dest = dest_for(url, out_root)
    vid = item.get("video_id") or extract_video_id(dest, "") or ""

    if dest.exists() and dest.stat().st_size > 80_000:
        with _stats_lock:
            _stats["skipped_exists"] += 1
        return

    with _stats_lock:
        _stats["attempted"] += 1

    if not download_file(url, dest, proxy):
        with _stats_lock:
            _stats["failed"] += 1
        return

    ok, _score, reason = is_gameplay_video(
        dest,
        csv_lookup=lookup,
        description=item.get("description", ""),
        min_score=0.70,
    )
    if not ok and strict_csv and reason == "csv_lookup":
        dest.unlink(missing_ok=True)
        with _stats_lock:
            _stats["rejected"] += 1
        with state_lock:
            if vid:
                state.setdefault("rejected_ids", []).append(vid)
        return

    if not ok:
        other = out_root / "non_gameplay" / dest.parent.name / dest.name
        other.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest.replace(other)
        with _stats_lock:
            _stats["saved_other"] += 1
        saved_path = other
    else:
        with _stats_lock:
            _stats["saved_gameplay"] += 1
        saved_path = dest
        log(f"kept gameplay {vid} ({reason})")

    with state_lock:
        if vid:
            ids = state.setdefault("downloaded_ids", [])
            if vid not in ids:
                ids.append(vid)
        urls = state.setdefault("seen_urls", [])
        if url not in urls:
            urls.append(url)
        state["last_saved"] = str(saved_path)


def producer_loop(
    work_q: queue.Queue[dict | None],
    proxy: str,
    state: dict,
    target: int,
    stop_event: threading.Event,
) -> None:
    seen = set(state.get("seen_urls", []))
    while not stop_event.is_set():
        on_disk = count_mp4_on_disk(DEFAULT_OUT)
        if on_disk >= target and work_q.empty():
            log(f"producer: on_disk={on_disk} >= target={target}, stopping discovery")
            break
        batch_target = max(target - on_disk, 200)
        items = discover_items(proxy, state, batch_target, seen)
        save_state(state)
        if not items:
            log("producer: no new URLs discovered, sleep 60s")
            time.sleep(60)
            continue
        with _stats_lock:
            _stats["queued"] += len(items)
        for item in items:
            if stop_event.is_set():
                break
            work_q.put(item)
        log(f"producer: queued {len(items)} urls (on_disk={on_disk})")
        if on_disk >= target:
            break
        time.sleep(5)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=int, default=5000, help="URLs to try / files on disk goal")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--env", type=Path, default=Path("/root/.video_bot.env"))
    parser.add_argument(
        "--strict-csv-reject",
        action="store_true",
        help="Delete clips marked non-gameplay in gameplay_filter CSV (default: keep in non_gameplay/)",
    )
    parser.add_argument(
        "--no-producer",
        action="store_true",
        help="Single discovery pass then exit (legacy mode)",
    )
    args = parser.parse_args()

    env = load_env(args.env)
    proxy = env.get("YTDLP_PROXY") or env.get("PROXY_URL")
    if not proxy:
        log("ERROR: no YTDLP_PROXY / PROXY_URL in env")
        return 1

    state = load_state()
    on_disk = count_mp4_on_disk(args.out)
    log(f"start on_disk={on_disk} target={args.target} workers={args.workers}")

    lookup = load_csv_lookup(GAMEPLAY_CSV)
    state_lock = threading.Lock()
    work_q: queue.Queue[dict | None] = queue.Queue(maxsize=args.workers * 4)
    stop_event = threading.Event()

    if args.no_producer:
        queue_items = discover_items(proxy, state, args.target, set(state.get("seen_urls", [])))
        save_state(state)
        with _stats_lock:
            _stats["queued"] = len(queue_items)
        for item in queue_items:
            work_q.put(item)
    else:
        prod = threading.Thread(
            target=producer_loop,
            args=(work_q, proxy, state, args.target, stop_event),
            daemon=True,
        )
        prod.start()

    def worker() -> None:
        while True:
            item = work_q.get()
            try:
                if item is None:
                    return
                process_item(
                    item, args.out, proxy, lookup, args.strict_csv_reject, state, state_lock
                )
                if count_mp4_on_disk(args.out) >= args.target:
                    stop_event.set()
            except Exception as exc:
                log(f"worker error: {exc}")
            finally:
                work_q.task_done()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(args.workers)]
    for thread in threads:
        thread.start()

    if not args.no_producer:
        prod.join(timeout=7200)

    stop_event.set()
    for _ in range(args.workers):
        work_q.put(None)
    for thread in threads:
        thread.join(timeout=600)
    state["mass_last_stats"] = dict(_stats)
    state["mass_on_disk"] = count_mp4_on_disk(args.out)
    save_state(state)
    log(json.dumps(_stats, ensure_ascii=False) + f" on_disk={state['mass_on_disk']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
