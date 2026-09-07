#!/usr/bin/env python3
"""Autonomous PUBG Metro Shorts watcher → feature table → ranges → TG reports.

Polite YouTube access (sleep + hourly caps) to reduce ban risk.
Every N Shorts: Telegram progress report with forming parameter ranges.
Optionally cut a few VOD probes for owner 👍/👎 to check learning.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pubg_shorts_feature_table import (
    append_feature_row,
    build_silver_ranges,
    data_root,
    format_ranges_table,
    iter_feature_rows,
    load_state,
    reports_dir,
    row_from_short_metrics,
    save_state,
)
from vod_telegram_env import send_message
from youtube_download import load_env, subprocess_env_no_proxy, ytdlp_cmd, ytdlp_extra_args
from youtube_game_prefs import has_metro_royale

REPO = Path(os.environ.get("CONTENT_BOT_REPO", Path(__file__).resolve().parent.parent))

DEFAULT_QUERIES = (
    "метро роял пабг перестрелка shorts",
    "метро роял 7 карта shorts пабг",
    "метро роял соло против отряда shorts",
    "pubg metro royale gunfight shorts",
    "metro royale clutch shorts",
)

HIGHLIGHT_QUERIES = (
    "метро роял пабг лучшие моменты",
    "метро роял клатч пабг",
    "pubg metro royale highlights",
    "pubg metro royale clutch",
    "метро роял один против сквада",
)

TITLE_BLOCK = re.compile(
    r"(giveaway|#ad\b|sponsored|tutorial|guide|tips|reaction|meme|funny|"
    r"montage|compilation|lobby only|skin showcase)",
    re.I,
)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, str(default))))
    except ValueError:
        return default


def shorts_root() -> Path:
    return Path(
        os.environ.get(
            "PUBG_SHORTS_AUTOLEARN_MEDIA",
            "/root/datasets/pubg/shorts_autolearn",
        )
    )


def local_pool_dirs() -> list[tuple[str, Path]]:
    """Local silver pools — no YouTube rate limit."""
    raw = os.environ.get("PUBG_SHORTS_LOCAL_POOLS", "").strip()
    if raw:
        out: list[tuple[str, Path]] = []
        for part in raw.split("|"):
            part = part.strip()
            if not part:
                continue
            if ":" in part:
                tag, path_s = part.split(":", 1)
            else:
                tag, path_s = "local", part
            out.append((tag, Path(path_s)))
        return out
    return [
        ("youtube_shorts_calib", Path("/root/datasets/pubg/youtube_shorts")),
        ("viral_reference", Path("/root/datasets/viral_reference/pubg")),
        ("exemplar_good", Path("/root/data/highlight_exemplars/pubg/good")),
        ("exemplar_bad", Path("/root/data/highlight_exemplars/pubg/bad")),
        ("shorts_autolearn_media", shorts_root()),
        ("tiktok_pubg", Path("/root/datasets/tiktok/pubg")),
        ("tiktok_metro", Path("/root/datasets/tiktok/metro")),
    ]


def _video_id_from_path(path: Path) -> str:
    stem = path.stem
    if stem.startswith("yt_") and len(stem) >= 14:
        return stem[3:14]
    # Stable synthetic id for non-YouTube local files.
    import hashlib

    return hashlib.sha1(str(path).encode()).hexdigest()[:11]


def list_local_candidates(*, seen: set[str], limit: int) -> list[dict[str, Any]]:
    cands: list[dict[str, Any]] = []
    for source, root in local_pool_dirs():
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.mp4")):
            if path.stat().st_size < 20_000:
                continue
            vid = _video_id_from_path(path)
            key = f"local:{path}"
            if vid in seen or key in seen:
                continue
            cands.append(
                {
                    "video_id": vid,
                    "seen_key": key,
                    "title": path.stem.replace("_", " ")[:200],
                    "path": path,
                    "source": source,
                    "source_url": f"file://{path}",
                    "search_query": f"local:{source}",
                    "view_count": 0,
                    "duration": 0.0,
                }
            )
            if len(cands) >= limit * 3:
                break
        if len(cands) >= limit * 3:
            break
    random.shuffle(cands)
    return cands[:limit]


def ingest_one_local(hit: dict[str, Any], *, batch_id: int) -> dict[str, Any] | None:
    path = Path(hit["path"])
    try:
        q_ok, q_reason, report = extract_short_features(path)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"score:{exc}"[:160], "video_id": hit.get("video_id")}
    row = row_from_short_metrics(
        video_id=str(hit["video_id"]),
        title=hit.get("title") or "",
        source_url=hit.get("source_url") or "",
        search_query=hit.get("search_query") or "",
        path=str(path),
        meta=hit,
        quality_report=report,
        quality_ok=q_ok,
        reject_reason="" if q_ok else q_reason,
        batch_id=batch_id,
        source=str(hit.get("source") or "local"),
    )
    return row


def run_local_batch(
    state: dict[str, Any],
    *,
    max_items: int | None = None,
    workers: int | None = None,
) -> dict[str, Any]:
    """Score already-on-disk clips in parallel — accelerates learning without YT."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    max_items = max_items or _env_int("PUBG_SHORTS_LOCAL_BATCH", 25)
    workers = workers or _env_int("PUBG_SHORTS_LOCAL_WORKERS", 2)
    seen = set(state.get("seen_ids") or [])
    hits = list_local_candidates(seen=seen, limit=max_items)
    if not hits:
        return {"saved": 0, "attempted": 0, "errors": [], "pool_empty": True}

    saved = 0
    errors: list[str] = []
    batch_base = int(state.get("batches") or 0)

    def _job(hit: dict[str, Any], idx: int) -> dict[str, Any] | None:
        return ingest_one_local(hit, batch_id=batch_base + idx + 1)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(_job, hit, i): hit for i, hit in enumerate(hits)}
        for fut in as_completed(futs):
            hit = futs[fut]
            try:
                row = fut.result()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"local:{exc}"[:160])
                continue
            if not row:
                continue
            if row.get("error"):
                errors.append(str(row["error"]))
                continue
            append_feature_row(row)
            seen.add(str(hit.get("seen_key") or hit["video_id"]))
            seen.add(str(hit["video_id"]))
            saved += 1
            state["batches"] = int(state.get("batches") or 0) + 1
            state["watched_total"] = int(state.get("watched_total") or 0) + 1
            state["local_watched"] = int(state.get("local_watched") or 0) + 1
            state["seen_ids"] = list(seen)
            if saved % 5 == 0:
                save_state(state)

    save_state(state)
    return {"saved": saved, "attempted": len(hits), "errors": errors[:20], "pool_empty": False}


def polite_sleep(kind: str = "download") -> None:
    """Jittered sleep between YouTube calls — anti-ban."""
    if kind == "search":
        lo = _env_float("PUBG_SHORTS_SEARCH_SLEEP_MIN", 18.0)
        hi = _env_float("PUBG_SHORTS_SEARCH_SLEEP_MAX", 40.0)
    else:
        lo = _env_float("PUBG_SHORTS_DOWNLOAD_SLEEP_MIN", 40.0)
        hi = _env_float("PUBG_SHORTS_DOWNLOAD_SLEEP_MAX", 80.0)
    if hi < lo:
        lo, hi = hi, lo
    delay = random.uniform(lo, hi)
    time.sleep(delay)


def _rate_ok(state: dict[str, Any], *, kind: str) -> bool:
    now = time.time()
    hour_key = f"{kind}_hour_ts"
    count_key = f"{kind}_hour_count"
    window_start = float(state.get(hour_key) or 0.0)
    count = int(state.get(count_key) or 0)
    if now - window_start >= 3600:
        state[hour_key] = now
        state[count_key] = 0
        count = 0
    cap = _env_int(
        "PUBG_SHORTS_MAX_SEARCH_PER_HOUR" if kind == "search" else "PUBG_SHORTS_MAX_DL_PER_HOUR",
        8 if kind == "search" else 17,
    )
    if count >= cap:
        return False
    if kind == "download":
        day_key = "download_day_ts"
        day_count_key = "download_day_count"
        day_start = float(state.get(day_key) or 0.0)
        day_count = int(state.get(day_count_key) or 0)
        if now - day_start >= 86400:
            state[day_key] = now
            state[day_count_key] = 0
            day_count = 0
        day_cap = _env_int("PUBG_SHORTS_MAX_DL_PER_DAY", 385)
        if day_cap > 0 and day_count >= day_cap:
            return False
    return True


def _rate_bump(state: dict[str, Any], *, kind: str) -> None:
    now = time.time()
    hour_key = f"{kind}_hour_ts"
    count_key = f"{kind}_hour_count"
    if now - float(state.get(hour_key) or 0.0) >= 3600:
        state[hour_key] = now
        state[count_key] = 0
    state[count_key] = int(state.get(count_key) or 0) + 1
    if kind == "download":
        day_key = "download_day_ts"
        day_count_key = "download_day_count"
        if now - float(state.get(day_key) or 0.0) >= 86400:
            state[day_key] = now
            state[day_count_key] = 0
        state[day_count_key] = int(state.get(day_count_key) or 0) + 1


def _title_ok(title: str) -> bool:
    if TITLE_BLOCK.search(title or ""):
        return False
    return has_metro_royale({"title": title or ""}) or bool(
        re.search(r"metro\s*royale|метро\s*роял", title or "", re.I)
    )


def search_youtube_clips(
    query: str,
    *,
    limit: int,
    env: dict[str, str],
    mode: str = "shorts",
) -> list[dict[str, Any]]:
    search_n = max(limit * 3, 20)
    cmd = ytdlp_cmd(env) + [
        f"ytsearch{search_n}:{query}",
        "--flat-playlist",
        "--print",
        "%(id)s|%(title)s|%(duration)s|%(view_count)s|%(upload_date)s",
        *ytdlp_extra_args(env),
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        timeout=150,
        env=subprocess_env_no_proxy(env),
    )
    rows: list[dict[str, Any]] = []
    dur_lo, dur_hi = (4.0, 75.0) if mode == "shorts" else (12.0, 90.0)
    source = "youtube_shorts" if mode == "shorts" else "youtube_highlights"
    for line in (proc.stdout or "").splitlines():
        parts = line.split("|", 4)
        if len(parts) < 2:
            continue
        vid = parts[0][:11]
        if len(vid) != 11:
            continue
        title = parts[1][:200]
        if not _title_ok(title):
            continue
        try:
            dur = float(parts[2]) if len(parts) > 2 and parts[2] else 0.0
        except ValueError:
            dur = 0.0
        if dur and (dur < dur_lo or dur > dur_hi):
            continue
        try:
            views = int(float(parts[3])) if len(parts) > 3 and parts[3] else 0
        except ValueError:
            views = 0
        url = (
            f"https://www.youtube.com/shorts/{vid}"
            if mode == "shorts"
            else f"https://www.youtube.com/watch?v={vid}"
        )
        rows.append(
            {
                "video_id": vid,
                "title": title,
                "duration": dur,
                "view_count": views,
                "upload_date": parts[4] if len(parts) > 4 else "",
                "url": url,
                "search_query": query,
                "source": source,
            }
        )
        if len(rows) >= limit:
            break
    return rows


def search_shorts(query: str, *, limit: int, env: dict[str, str]) -> list[dict[str, Any]]:
    return search_youtube_clips(query, limit=limit, env=env, mode="shorts")


def download_short(
    vid: str,
    dest: Path,
    env: dict[str, str],
    *,
    url: str | None = None,
) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 10_000:
        return True
    # Prefer progressive mp4 to avoid stuck f###.webm.part merges on Shorts.
    fmt = env.get(
        "YOUTUBE_SHORTS_FORMAT",
        "b[ext=mp4][height<=720]/bv*[ext=mp4][height<=720]+ba[ext=m4a]/b[height<=720]/b",
    )
    url = url or f"https://www.youtube.com/shorts/{vid}"
    # Clean stale partials from previous failed attempts.
    for stale in dest.parent.glob(f"yt_{vid}.mp4*"):
        if stale.suffix in {".part", ".ytdl"} or ".f" in stale.name:
            try:
                stale.unlink()
            except OSError:
                pass
    out_tmpl = str(dest.parent / f"yt_{vid}.%(ext)s")
    cmd = ytdlp_cmd(env) + [
        "-f",
        fmt,
        "--no-playlist",
        "--retries",
        "3",
        "--fragment-retries",
        "5",
        "--merge-output-format",
        "mp4",
        "--newline",
        "-o",
        out_tmpl,
        url,
        *ytdlp_extra_args(env),
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        timeout=_env_int("YOUTUBE_SHORTS_TIMEOUT", 420),
        env=subprocess_env_no_proxy(env),
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "")[-500:]
        if re.search(r"429|Too Many Requests|Sign in to confirm|bot", err, re.I):
            raise RuntimeError(f"youtube_rate_limited:{err[:120]}")
        return False
    # Resolve actual output (mp4 preferred).
    if dest.is_file() and dest.stat().st_size > 10_000:
        return True
    cands = sorted(
        [
            p
            for p in dest.parent.glob(f"yt_{vid}.*")
            if p.suffix.lower() in {".mp4", ".webm", ".mkv"} and p.stat().st_size > 10_000
        ],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not cands:
        return False
    if cands[0] != dest:
        try:
            if dest.exists():
                dest.unlink()
            cands[0].replace(dest)
        except OSError:
            return cands[0].is_file() and cands[0].stat().st_size > 10_000
    return dest.is_file() and dest.stat().st_size > 10_000


def extract_short_features(path: Path) -> tuple[bool, str, dict[str, Any]]:
    """Score a Short with the same PUBG window scorer used for VOD cuts."""
    from pubg_quality_score import score_pubg_window

    # Shorts are vertical full clips — score almost the whole file.
    try:
        from gameplay_gate import prefer_ffmpeg_decode  # noqa: F401
    except Exception:
        pass
    dur_cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    proc = subprocess.run(dur_cmd, capture_output=True, text=True, check=False, timeout=30)
    try:
        dur = float((proc.stdout or "0").strip() or 0)
    except ValueError:
        dur = 0.0
    if dur <= 1:
        dur = 20.0
    start = 0.2
    window = min(max(dur - 0.4, 4.0), 55.0)
    # Autolearn wants metrics even when gates would hard-reject — still record rejects.
    prev_require = os.environ.get("PUBG_REQUIRE_AUTHOR_KILL_SINGLES")
    os.environ["PUBG_REQUIRE_AUTHOR_KILL_SINGLES"] = os.environ.get(
        "PUBG_SHORTS_REQUIRE_KILL", "0"
    )
    try:
        ok, reason, report = score_pubg_window(
            path,
            start,
            window,
            single=True,
            use_cache=False,
        )
    finally:
        if prev_require is None:
            os.environ.pop("PUBG_REQUIRE_AUTHOR_KILL_SINGLES", None)
        else:
            os.environ["PUBG_REQUIRE_AUTHOR_KILL_SINGLES"] = prev_require
    report = dict(report or {})
    report["duration"] = round(window, 2)
    return bool(ok), str(reason or ""), report


def _queries(*, mode: str = "shorts") -> list[str]:
    if mode == "highlights":
        raw = os.environ.get("PUBG_SHORTS_HIGHLIGHT_QUERIES", "").strip()
        if raw:
            return [q.strip() for q in raw.split("|") if q.strip()]
        return list(HIGHLIGHT_QUERIES)
    raw = os.environ.get("PUBG_SHORTS_AUTOLEARN_QUERIES", "").strip()
    if raw:
        return [q.strip() for q in raw.split("|") if q.strip()]
    # Prefer YAML config when present.
    try:
        from game_shorts_calibration import load_games

        for g in load_games():
            if g.id == "pubg" and g.queries:
                return list(g.queries)
    except Exception:
        pass
    return list(DEFAULT_QUERIES)


def maybe_send_batch_report(state: dict[str, Any], ranges_blob: dict[str, Any]) -> bool:
    every = _env_int("PUBG_SHORTS_REPORT_EVERY", 100)
    total = int(state.get("watched_total") or 0)
    last = int(state.get("last_report_at_count") or 0)
    if every <= 0 or total < every or total - last < every:
        return False
    batch_no = total // every
    table = format_ranges_table(ranges_blob)
    text = (
        f"PUBG Shorts autolearn — отчёт #{batch_no}\n"
        f"Просмотрено {total} Shorts (+{total - last} с прошлого отчёта).\n"
        f"Сформирован диапазон параметров для нарезки VOD:\n"
        f"{table}\n"
        f"Продолжаю просмотр аккуратно (rate-limit).\n"
        f"Таблица: {data_root() / 'features.csv'}"
    )
    ok = send_message(text)
    report_path = reports_dir() / f"report_{total:05d}.txt"
    report_path.write_text(text, encoding="utf-8")
    if ok:
        state["last_report_at_count"] = total
        state["last_report_ok"] = True
    else:
        state["last_report_ok"] = False
    return ok


def find_inbox_vod() -> Path | None:
    roots = [
        Path(os.environ.get("PUBG_VOD_INBOX", "/root/data/mlbb/youtube_nightly/inbox")),
        Path("/root/videos/pubg"),
        Path("/root/data/pubg/vods"),
    ]
    cands: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.glob("yt_*.mp4"):
            if path.stat().st_size > 5_000_000:
                cands.append(path)
    if not cands:
        return None
    cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0]


def probe_vod_cuts(
    *,
    ranges_blob: dict[str, Any],
    count: int | None = None,
) -> list[dict[str, Any]]:
    """Cut a few VOD windows that match silver ranges for owner rating."""
    count = count or _env_int("PUBG_SHORTS_PROBE_COUNT", 3)
    vod = find_inbox_vod()
    if vod is None:
        return []
    from pubg_sent_param_ranges import evaluate_against_ranges
    from pubg_window_label_queue import render_preview

    # Cheap peaks: prefer montage gun discovery, else fixed grid.
    peaks: list[float] = []
    try:
        from shooter_vod_fast_scan import discover_montage_gun_peaks

        peaks, _reason = discover_montage_gun_peaks(vod, "pubg", min_clips=5, gap_sec=40.0)
        peaks = [float(p) for p in (peaks or [])][:40]
    except Exception:
        peaks = []
    if not peaks:
        peaks = [30.0, 90.0, 150.0, 240.0, 360.0, 480.0, 600.0]

    preview_dir = data_root() / "vod_probes"
    preview_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    from pubg_quality_score import score_pubg_window

    for peak in peaks:
        if len(results) >= count:
            break
        start = max(0.0, float(peak) - 4.0)
        dur = 14.0
        try:
            ok, reason, report = score_pubg_window(
                vod, start, dur, single=True, use_cache=True
            )
        except Exception as exc:  # noqa: BLE001
            results.append({"peak": peak, "error": str(exc)[:120]})
            continue
        rng_ok, rng_reason, _ = evaluate_against_ranges(
            report,
            game="pubg",
            ranges_blob={**ranges_blob, "ready": True} if ranges_blob.get("ranges") else ranges_blob,
        )
        # Prefer windows inside silver envelope; still send borderline if few hits.
        if ranges_blob.get("ready") and not rng_ok and len(results) < count // 2 + 1:
            # skip obvious out-of-range early
            if "out_of_sent_range" in rng_reason:
                continue
        dest = preview_dir / f"{vod.stem}_{int(start)}_{int(time.time())}.mp4"
        try:
            render_preview(vod, start, start + dur, dest)
        except Exception as exc:  # noqa: BLE001
            results.append({"peak": peak, "error": f"render:{exc}"[:120]})
            continue
        sent = False
        try:
            from mlbb_telegram_video import send_video_file
            from vod_telegram_env import bot_token, chat_id

            token = bot_token()
            chat = chat_id()
            caption = (
                f"Shorts-autolearn проба #{len(results)+1}\n"
                f"{vod.name} @ {int(start)}s\n"
                f"quality={'OK' if ok else reason[:80]}\n"
                f"range={rng_reason}\n"
                f"Оцени: научился бот или нет? 👍/👎"
            )
            if token and chat:
                sent = bool(send_video_file(token, chat, dest, caption))
        except Exception:
            sent = False
        results.append(
            {
                "vod": str(vod),
                "start": start,
                "ok": ok,
                "reason": reason,
                "range_reason": rng_reason,
                "preview": str(dest),
                "sent": sent,
            }
        )
    return results


def maybe_probe(state: dict[str, Any], ranges_blob: dict[str, Any]) -> list[dict[str, Any]]:
    every = _env_int("PUBG_SHORTS_REPORT_EVERY", 100)
    total = int(state.get("watched_total") or 0)
    last = int(state.get("last_probe_at_count") or 0)
    if os.environ.get("PUBG_SHORTS_VOD_PROBE", "1") != "1":
        return []
    if every <= 0 or total < every or total - last < every:
        return []
    if not ranges_blob.get("ranges"):
        return []
    probes = probe_vod_cuts(ranges_blob=ranges_blob)
    state["last_probe_at_count"] = total
    state["last_probe_count"] = len(probes)
    return probes


def run_youtube_batch(
    state: dict[str, Any],
    *,
    env: dict[str, str],
    max_downloads: int,
    mode: str = "shorts",
    dry_run: bool = False,
) -> dict[str, Any]:
    seen = set(state.get("seen_ids") or [])
    media = shorts_root()
    media.mkdir(parents=True, exist_ok=True)
    saved = 0
    attempted = 0
    errors: list[str] = []
    stop = False
    queries = _queries(mode=mode)
    random.shuffle(queries)

    for query in queries:
        if stop or saved >= max_downloads:
            break
        if not _rate_ok(state, kind="search"):
            errors.append("search_hour_cap")
            break
        if dry_run:
            continue
        try:
            polite_sleep("search")
            hits = search_youtube_clips(query, limit=12, env=env, mode=mode)
            _rate_bump(state, kind="search")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"search:{exc}"[:160])
            state["last_error"] = str(exc)[:200]
            time.sleep(_env_float("PUBG_SHORTS_ERROR_SLEEP", 180.0))
            break

        for hit in hits:
            if stop or saved >= max_downloads:
                break
            vid = hit["video_id"]
            if vid in seen:
                continue
            if not _rate_ok(state, kind="download"):
                errors.append("download_hour_cap")
                stop = True
                break
            dest = media / f"yt_{vid}.mp4"
            try:
                polite_sleep("download")
                ok_dl = download_short(vid, dest, env, url=hit.get("url"))
                attempted += 1
                if ok_dl:
                    _rate_bump(state, kind="download")
            except RuntimeError as exc:
                errors.append(str(exc)[:160])
                state["last_error"] = str(exc)[:200]
                time.sleep(_env_float("PUBG_SHORTS_BAN_SLEEP", 900.0))
                save_state(state)
                return {
                    "saved": saved,
                    "attempted": attempted,
                    "errors": errors,
                    "stopped": "rate_limited",
                }
            except Exception as exc:  # noqa: BLE001
                errors.append(f"dl:{exc}"[:160])
                continue
            if not ok_dl:
                errors.append(f"dl_incomplete:{vid}")
                continue

            try:
                q_ok, q_reason, report = extract_short_features(dest)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"score:{exc}"[:160])
                seen.add(vid)
                continue

            state["batches"] = int(state.get("batches") or 0) + 1
            row = row_from_short_metrics(
                video_id=vid,
                title=hit.get("title") or "",
                source_url=hit.get("url") or "",
                search_query=hit.get("search_query") or query,
                path=str(dest),
                meta=hit,
                quality_report=report,
                quality_ok=q_ok,
                reject_reason="" if q_ok else q_reason,
                batch_id=int(state["batches"]),
                source=str(hit.get("source") or mode),
            )
            append_feature_row(row)
            seen.add(vid)
            state["seen_ids"] = list(seen)
            state["watched_total"] = int(state.get("watched_total") or 0) + 1
            state[f"{mode}_watched"] = int(state.get(f"{mode}_watched") or 0) + 1
            saved += 1
            save_state(state)

    return {"saved": saved, "attempted": attempted, "errors": errors, "stopped": stop}


def run_once(
    *,
    max_downloads: int | None = None,
    dry_run: bool = False,
    local_only: bool = False,
    youtube_only: bool = False,
) -> dict[str, Any]:
    env = load_env(Path("/root/.video_bot.env"))
    state = load_state()
    max_downloads = max_downloads or _env_int("PUBG_SHORTS_BATCH_SIZE", 3)
    errors: list[str] = []
    local_stats: dict[str, Any] = {"saved": 0}
    shorts_stats: dict[str, Any] = {"saved": 0}
    highlights_stats: dict[str, Any] = {"saved": 0}

    if not youtube_only and os.environ.get("PUBG_SHORTS_LOCAL_INGEST", "1") == "1":
        local_stats = run_local_batch(state)
        errors.extend(local_stats.get("errors") or [])

    if not local_only:
        half = max(1, max_downloads // 2) if max_downloads > 1 else max_downloads
        shorts_stats = run_youtube_batch(
            state,
            env=env,
            max_downloads=max(1, max_downloads - half + 1) if max_downloads > 1 else max_downloads,
            mode="shorts",
            dry_run=dry_run,
        )
        errors.extend(shorts_stats.get("errors") or [])
        if os.environ.get("PUBG_SHORTS_HIGHLIGHTS", "1") == "1" and not shorts_stats.get(
            "stopped"
        ):
            highlights_stats = run_youtube_batch(
                state,
                env=env,
                max_downloads=half,
                mode="highlights",
                dry_run=dry_run,
            )
            errors.extend(highlights_stats.get("errors") or [])

    ranges_blob = build_silver_ranges()
    report_sent = maybe_send_batch_report(state, ranges_blob)
    probes = maybe_probe(state, ranges_blob)
    if probes:
        send_message(
            f"Shorts-autolearn: отправил {len(probes)} пробных нарезок из VOD "
            f"после {state.get('watched_total')} клипов. Оцени 👍/👎."
        )
    state["last_error"] = "; ".join(errors)[:300] if errors else ""
    save_state(state)
    return {
        "local_saved": int(local_stats.get("saved") or 0),
        "shorts_saved": int(shorts_stats.get("saved") or 0),
        "highlights_saved": int(highlights_stats.get("saved") or 0),
        "saved": int(local_stats.get("saved") or 0)
        + int(shorts_stats.get("saved") or 0)
        + int(highlights_stats.get("saved") or 0),
        "errors": errors[:30],
        "watched_total": int(state.get("watched_total") or 0),
        "ranges_ready": bool(ranges_blob.get("ready")),
        "report_sent": report_sent,
        "probes": len(probes),
        "stage": ranges_blob.get("stage"),
    }


def cmd_status() -> int:
    state = load_state()
    rows = iter_feature_rows()
    ranges_blob = build_silver_ranges(rows=rows)
    print(
        json.dumps(
            {
                "watched_total": state.get("watched_total"),
                "rows": len(rows),
                "last_report_at_count": state.get("last_report_at_count"),
                "last_probe_at_count": state.get("last_probe_at_count"),
                "stage": ranges_blob.get("stage"),
                "ready": ranges_blob.get("ready"),
                "data_root": str(data_root()),
                "last_error": state.get("last_error"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print(format_ranges_table(ranges_blob))
    return 0


def cmd_report(*, force: bool = False) -> int:
    state = load_state()
    ranges_blob = build_silver_ranges()
    if force:
        state["last_report_at_count"] = max(
            0, int(state.get("watched_total") or 0) - _env_int("PUBG_SHORTS_REPORT_EVERY", 100)
        )
    ok = maybe_send_batch_report(state, ranges_blob)
    save_state(state)
    print("report_sent" if ok else "report_skipped_or_failed")
    return 0 if ok or not force else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="PUBG Shorts autonomous learning loop")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Local pool + YouTube Shorts + highlights")
    run_p.add_argument("--max", type=int, default=0, help="Override YouTube batch size")
    run_p.add_argument("--dry-run", action="store_true")
    run_p.add_argument("--local-only", action="store_true")
    run_p.add_argument("--youtube-only", action="store_true")

    sub.add_parser("status", help="Show watched count + ranges")
    rep = sub.add_parser("report", help="Send Telegram range report if due")
    rep.add_argument("--force", action="store_true")
    probe = sub.add_parser("probe-vod", help="Cut VOD probes matching silver ranges")
    probe.add_argument("--count", type=int, default=3)
    local_p = sub.add_parser("run-local", help="Score on-disk pools only (fast, no YT)")
    local_p.add_argument("--max", type=int, default=0)

    args = parser.parse_args()
    if args.cmd == "run":
        out = run_once(
            max_downloads=args.max or None,
            dry_run=bool(args.dry_run),
            local_only=bool(args.local_only),
            youtube_only=bool(args.youtube_only),
        )
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "run-local":
        state = load_state()
        out = run_local_batch(
            state,
            max_items=args.max or None,
        )
        ranges_blob = build_silver_ranges()
        maybe_send_batch_report(state, ranges_blob)
        save_state(state)
        out["watched_total"] = state.get("watched_total")
        out["stage"] = ranges_blob.get("stage")
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "status":
        return cmd_status()
    if args.cmd == "report":
        return cmd_report(force=bool(args.force))
    if args.cmd == "probe-vod":
        ranges_blob = build_silver_ranges()
        probes = probe_vod_cuts(ranges_blob=ranges_blob, count=int(args.count))
        print(json.dumps(probes, ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
