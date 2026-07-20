#!/usr/bin/env python3
"""
Ingest viral silver reference clips (YouTube Shorts + TikTok) per game.

Pilot: pubg, mobile_legends. Outputs metadata CSV, feature CSV, cluster JSON,
and top exemplar cuts into highlight_exemplars/{game}/good|bad/.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gameplay_gate import is_gameplay_video
from highlight_scorer import WINDOW_SEC, normalize_profile, score_candidate_window
from viral_scorer import hook_score
from youtube_download import load_env, ytdlp_cmd, ytdlp_extra_args

REPO = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml"))
DATA_ROOT = REPO / "data" / "viral_reference"
DATASET_ROOT = Path(os.environ.get("VIRAL_REFERENCE_ROOT", "/root/datasets/viral_reference"))
EXEMPLAR_ROOT = Path(os.environ.get("HIGHLIGHT_EXEMPLAR_ROOT", str(REPO / "data" / "highlight_exemplars")))

GAME_SEARCHES: dict[str, list[str]] = {
    "pubg": [
        "pubg mobile highlights",
        "pubg best moments",
        "pubg mobile clutch",
        "pubg gunfight",
        "pubg metro royale",
    ],
    "mobile_legends": [
        "mobile legends highlights",
        "mlbb savage",
        "mlbb teamfight",
        "mlbb mythic rank",
        "mobile legends bang bang clutch",
    ],
    "standoff": [
        "standoff 2 highlights",
        "standoff 2 best moments",
        "standoff 2 clutch",
    ],
    "genshin": [
        "genshin impact boss fight",
        "genshin impact highlights",
        "genshin boss rush",
    ],
    "wot": [
        "wot blitz best moments shorts",
        "world of tanks blitz epic frag shorts",
        "wot blitz ace tanker shorts",
        "wot blitz clutch 1v3 shorts",
        "танки блиц эпичный выстрел shorts",
        "wot blitz 7 kills ace shorts",
        "world of tanks blitz brawl explosion shorts",
        "wot blitz kolobanov shorts",
        "танки блиц фраг shorts русский",
        "wot blitz double kill tank shorts",
    ],
}

NEGATIVE_TITLE = re.compile(
    r"(funny|meme|intro|outro|tutorial|guide|tips|tricks|reaction|rank push only|"
    r"giveaway|promo|skin showcase|lobby|menu)",
    re.I,
)


def _ffprobe_duration(path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
    try:
        return float((proc.stdout or "0").strip())
    except ValueError:
        return 0.0


def _ffprobe_aspect(path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "csv=p=0:s=x",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
    raw = (proc.stdout or "").strip()
    if "x" not in raw:
        return 0.0
    w, h = raw.split("x", 1)
    try:
        return round(float(w) / max(float(h), 1), 4)
    except ValueError:
        return 0.0


def search_youtube_shorts(query: str, *, limit: int, env: dict[str, str]) -> list[dict]:
    """yt-dlp flat search — returns entries with id, title, view_count, duration."""
    search_n = max(limit * 4, 40)
    cmd = ytdlp_cmd(env, use_proxy=False) + [
        f"ytsearch{search_n}:{query} #shorts",
        "--flat-playlist",
        "--print",
        "%(id)s\t%(title)s\t%(view_count)s\t%(duration)s\t%(webpage_url)s",
        "--no-download",
        *ytdlp_extra_args(env),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=180)
    entries: list[dict] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        vid, title, views, dur, url = parts[0], parts[1], parts[2], parts[3], parts[4]
        if not vid or len(vid) != 11:
            continue
        try:
            duration = float(dur or 0)
            view_count = int(float(views or 0))
        except (ValueError, TypeError):
            continue
        if duration <= 3 or duration > 60:
            continue
        if NEGATIVE_TITLE.search(title):
            continue
        if len(entries) >= limit:
            break
        entries.append(
            {
                "video_id": vid,
                "title": title,
                "view_count": view_count,
                "duration": duration,
                "webpage_url": url,
                "source": "youtube_shorts",
                "search_query": query,
            }
        )
    return entries


def download_short(url: str, out_dir: Path, env: dict[str, str]) -> Path | None:
    out_dir.mkdir(parents=True, exist_ok=True)
    template = str(out_dir / "yt_%(id)s.%(ext)s")
    cmd = ytdlp_cmd(env, use_proxy=False) + [
        "-f",
        env.get("YOUTUBE_SHORTS_FORMAT", "bv*[height<=1080]+ba/b[height<=720]/b"),
        "--merge-output-format",
        "mp4",
        "-o",
        template,
        "--no-playlist",
        *ytdlp_extra_args(env),
        url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=300)
    if proc.returncode != 0:
        return None
    # Find newest mp4 in out_dir matching this download
    mp4s = sorted(out_dir.glob("yt_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    return mp4s[0] if mp4s else None


def clip_embedding(path: Path, profile: str) -> np.ndarray | None:
    """Mean CLIP image embedding from hook/mid frames."""
    try:
        import open_clip
        import torch
    except ImportError:
        return None

    from gameplay_gate import _read_frame_at, detect_game_viewport_crop
    from highlight_scorer import _clip_bundle, _frame_to_pil

    profile = normalize_profile(profile)
    dur = _ffprobe_duration(path)
    if dur <= 0:
        dur = 10.0
    crop = detect_game_viewport_crop(path, 0.0, min(dur, 30.0))
    model, preprocess, _, device = _clip_bundle()
    times = (0.3, dur * 0.5, max(0.5, dur - 0.5))
    embs: list[np.ndarray] = []
    for t in times:
        frame = _read_frame_at(path, t)
        if frame is None:
            continue
        if crop is not None:
            x, y, w, h = crop
            frame = frame[y : y + h, x : x + w]
        tensor = preprocess(_frame_to_pil(frame)).unsqueeze(0).to(device)
        with torch.no_grad():
            emb = model.encode_image(tensor)
            emb = (emb / emb.norm(dim=-1, keepdim=True)).cpu().numpy()[0]
        embs.append(emb)
    if not embs:
        return None
    return np.mean(embs, axis=0)


def extract_features(path: Path, profile: str, meta: dict) -> dict:
    profile = normalize_profile(profile)
    dur = _ffprobe_duration(path)
    window = min(WINDOW_SEC, max(4.0, dur * 0.8))
    m = score_candidate_window(path, 0.2, window, profile)
    hook, hook_meta = hook_score(path, 0.2, profile, duration_sec=window)
    combat = float(m.panns_gun_max) + max(0.0, float(m.clip_score)) * 0.4
    return {
        **meta,
        "path": str(path),
        "file_name": path.name,
        "duration_sec": round(dur, 2),
        "aspect_ratio": _ffprobe_aspect(path),
        "panns_gunshot": round(float(m.panns_gunshot), 4),
        "panns_machine_gun": round(float(m.panns_machine_gun), 4),
        "panns_gun_max": round(float(m.panns_gun_max), 4),
        "clip_score": round(float(m.clip_score), 4),
        "center_motion": round(float(m.center_motion), 4),
        "hook_score": round(hook, 4),
        "combat_score": round(combat, 4),
        "rule_pass": int(bool(m.rule_pass and m.visual_pass)),
        "hook_menu": hook_meta.get("menu_overlay", 0),
    }


def ingest_tiktok_mlbb(
    profile: str,
    *,
    limit: int,
    gameplay_csv: Path,
) -> list[dict]:
    """Reuse already-downloaded TikTok gameplay clips for MLBB silver."""
    if profile != "mobile_legends":
        return []
    tiktok_root = Path("/root/datasets/tiktok/mlbb")
    if not tiktok_root.exists():
        tiktok_root = REPO / "data" / "samples" / "tiktok_mlbb"
    if not tiktok_root.exists():
        return []

    lookup: dict[str, dict] = {}
    ranked = REPO / "data" / "mlbb" / "current_mlbb_ranked_videos.csv"
    if ranked.exists():
        with ranked.open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                lookup[row.get("video_id", "")] = row

    allowed_ids: set[str] = set()
    if gameplay_csv.exists():
        with gameplay_csv.open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                raw = str(row.get("is_gameplay", row.get("gameplay_score", ""))).lower()
                if raw in ("1", "true", "yes") or (raw.replace(".", "", 1).isdigit() and float(raw) >= 0.5):
                    allowed_ids.add(str(row.get("video_id", "")))

    out: list[dict] = []
    for mp4 in sorted(tiktok_root.glob("*.mp4"))[: limit * 3]:
        vid = mp4.stem
        if allowed_ids and vid not in allowed_ids:
            continue
        meta_row = lookup.get(vid, {})
        out.append(
            {
                "video_id": vid,
                "title": (meta_row.get("description") or mp4.stem)[:200],
                "view_count": int(meta_row.get("view_count") or 0),
                "duration": float(meta_row.get("duration") or _ffprobe_duration(mp4)),
                "webpage_url": meta_row.get("webpage_url", ""),
                "source": "tiktok",
                "search_query": "current_mlbb_ranked_videos",
                "local_path": str(mp4),
            }
        )
        if len(out) >= limit:
            break
    return out


def cluster_clips(rows: list[dict], profile: str, *, k: int = 6) -> dict:
    embs: list[np.ndarray] = []
    valid_idx: list[int] = []
    for i, row in enumerate(rows):
        path = Path(row.get("path", ""))
        if not path.exists():
            continue
        emb = clip_embedding(path, profile)
        if emb is not None:
            embs.append(emb)
            valid_idx.append(i)

    if len(embs) < k:
        return {"status": "insufficient", "n": len(embs), "clusters": []}

    from sklearn.cluster import KMeans

    mat = np.stack(embs)
    km = KMeans(n_clusters=k, random_state=42, n_init=10)
    labels = km.fit_predict(mat)

    clusters: list[dict] = []
    for cid in range(k):
        members = [valid_idx[i] for i, lab in enumerate(labels) if lab == cid]
        if not members:
            continue
        member_rows = [rows[i] for i in members]
        member_rows.sort(key=lambda r: float(r.get("view_count") or 0) * float(r.get("hook_score") or 0), reverse=True)
        centroid = km.cluster_centers_[cid].tolist()
        clusters.append(
            {
                "cluster_id": cid,
                "size": len(members),
                "centroid": [round(x, 6) for x in centroid[:8]] + ["..."],
                "top_clips": [r.get("file_name") for r in member_rows[:3]],
                "archetype_views": int(member_rows[0].get("view_count") or 0),
            }
        )
    clusters.sort(key=lambda c: c["archetype_views"], reverse=True)
    return {"status": "ok", "n": len(embs), "k": k, "clusters": clusters}


def copy_exemplar(src: Path, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        str(src),
        "-t",
        "8",
        "-c",
        "copy",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, check=False, timeout=120)
    return proc.returncode == 0 and dest.exists()


def ingest_game(
    profile: str,
    *,
    max_download: int,
    skip_download: bool,
    tiktok_limit: int,
) -> int:
    profile = normalize_profile(profile)
    os.environ.setdefault("HIGHLIGHT_HEATMAP", "0")
    os.environ.setdefault("HIGHLIGHT_USE_OWNER_ANCHORS", "0")
    env = {**os.environ, **load_env()}
    out_dir = DATASET_ROOT / profile
    meta_path = DATA_ROOT / f"{profile}.csv"
    feat_path = DATA_ROOT / f"{profile}_features.csv"
    cluster_path = DATA_ROOT / f"{profile}_clusters.json"
    DATA_ROOT.mkdir(parents=True, exist_ok=True)

    candidates: list[dict] = []
    if not skip_download:
        per_query = max(5, max_download // max(1, len(GAME_SEARCHES.get(profile, []))))
        for query in GAME_SEARCHES.get(profile, []):
            candidates.extend(search_youtube_shorts(query, limit=per_query, env=env))

    if profile == "mobile_legends":
        gameplay_csv = Path("/root/data/mlbb/gameplay_filter_latest.csv")
        if not gameplay_csv.exists():
            gameplay_csv = REPO / "data" / "mlbb" / "gameplay_filter_latest.csv"
        candidates.extend(ingest_tiktok_mlbb(profile, limit=tiktok_limit, gameplay_csv=gameplay_csv))

    seen: set[str] = set()
    unique: list[dict] = []
    for row in sorted(candidates, key=lambda r: int(r.get("view_count") or 0), reverse=True):
        key = row.get("video_id") or row.get("local_path") or row.get("webpage_url")
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(row)
    unique = unique[:max_download]

    meta_rows: list[dict] = []
    feature_rows: list[dict] = []

    for row in unique:
        local_path = row.get("local_path")
        if local_path:
            mp4 = Path(local_path)
        else:
            mp4 = out_dir / f"yt_{row['video_id']}.mp4"
            if not mp4.exists() and not skip_download:
                url = row.get("webpage_url") or f"https://www.youtube.com/watch?v={row['video_id']}"
                mp4 = download_short(url, out_dir, env) or mp4
        if not mp4.exists():
            continue

        ok, score, reason = is_gameplay_video(mp4, csv_lookup={}, description=row.get("title", ""))
        if not ok:
            row["reject_reason"] = reason
            row["is_gameplay"] = 0
            if NEGATIVE_TITLE.search(row.get("title", "")):
                bad_dest = EXEMPLAR_ROOT / profile / "bad" / f"viral_{mp4.stem}.mp4"
                copy_exemplar(mp4, bad_dest)
            continue

        row["is_gameplay"] = 1
        row["gameplay_score"] = round(float(score), 4)
        meta_rows.append(row)

        feats = extract_features(mp4, profile, row)
        feature_rows.append(feats)

        if float(feats.get("combat_score") or 0) < 0.08 and int(feats.get("view_count") or 0) < 10000:
            bad_dest = EXEMPLAR_ROOT / profile / "bad" / f"viral_{mp4.stem}.mp4"
            copy_exemplar(mp4, bad_dest)

    if meta_rows:
        with meta_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=sorted({k for r in meta_rows for k in r}))
            writer.writeheader()
            writer.writerows(meta_rows)

    if feature_rows:
        with feat_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=sorted({k for r in feature_rows for k in r}))
            writer.writeheader()
            writer.writerows(feature_rows)

    cluster_report = cluster_clips(feature_rows, profile)
    cluster_path.write_text(json.dumps(cluster_report, indent=2, ensure_ascii=False), encoding="utf-8")

    good_added = 0
    if cluster_report.get("status") == "ok":
        by_name = {r["file_name"]: r for r in feature_rows}
        for cluster in cluster_report["clusters"][:3]:
            for fname in cluster.get("top_clips", [])[:1]:
                row = by_name.get(fname)
                if not row:
                    continue
                src = Path(row["path"])
                dest = EXEMPLAR_ROOT / profile / "good" / f"viral_{src.stem}.mp4"
                if copy_exemplar(src, dest):
                    good_added += 1

    print(
        f"OK profile={profile} meta={len(meta_rows)} features={len(feature_rows)} "
        f"clusters={cluster_report.get('status')} exemplars_good+={good_added}"
    )
    print(f"  meta_csv={meta_path}")
    print(f"  features_csv={feat_path}")
    print(f"  clusters_json={cluster_path}")
    return 0 if feature_rows else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--profile",
        default="pubg",
        choices=["pubg", "mobile_legends", "mlbb", "standoff", "genshin", "wot", "all"],
    )
    parser.add_argument("--max-download", type=int, default=50)
    parser.add_argument("--tiktok-limit", type=int, default=30)
    parser.add_argument("--skip-download", action="store_true", help="Only process existing local clips")
    args = parser.parse_args()

    profiles = (
        ("pubg", "mobile_legends")
        if args.profile == "all"
        else (normalize_profile(args.profile),)
    )
    code = 0
    for profile in profiles:
        if ingest_game(
            profile,
            max_download=args.max_download,
            skip_download=args.skip_download,
            tiktok_limit=args.tiktok_limit,
        ) != 0:
            code = 1
    return code


if __name__ == "__main__":
    raise SystemExit(main())
