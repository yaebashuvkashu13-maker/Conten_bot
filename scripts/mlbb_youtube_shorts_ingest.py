#!/usr/bin/env python3
"""
MLBB-only: ingest top YouTube Shorts (≤60s, ~90 days) for owner calibration.

Searches: mobile legends highlights, mlbb teamfight, mlbb savage
Filters: gameplay_gate, highlight_scorer (mobile_legends)
Output: /root/datasets/mlbb/youtube_shorts/ + data/mlbb/youtube_shorts_index.json
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gameplay_gate import is_gameplay_video
from highlight_scorer import WINDOW_SEC, score_candidate_window
from mlbb_calibration_store import (
    SHORTS_ROOT,
    labeled_ids,
    pending_candidates,
    rebuild_index_from_disk,
    repair_index,
    upsert_candidate,
)
from viral_scorer import hook_score
from youtube_download import load_env, subprocess_env_no_proxy, ytdlp_cmd, ytdlp_extra_args
from youtube_video_fix import ensure_readable

SEARCH_QUERIES = (
    "mlbb ranked gameplay savage",
    "mobile legends streamer ranked teamfight",
    "mlbb solo rank maniac gameplay",
    "mlbb live gameplay highlights",
    "mlbb double kill triple kill savage",
    "mlbb teamfight shorts",
    "mobile legends savage maniac shorts",
    "mlbb mythic rank fight",
)

STREAMER_SHORTS_FEEDS = (
    # Owner-curated MLBB gameplay (Chou / ranked streamers)
    "https://www.youtube.com/@hanz.legends/shorts",
    "https://www.youtube.com/@silent_chou/shorts",
    "https://www.youtube.com/@officiallazychouu/shorts",
    "https://www.youtube.com/@rikkchoou/shorts",
    "https://www.youtube.com/@kyro-plays-o/shorts",
    "https://www.youtube.com/@run-yss/shorts",
    # Extra ranked gameplay sources
    "https://www.youtube.com/@Betosky/shorts",
    "https://www.youtube.com/@JessNoLimit/shorts",
    "https://www.youtube.com/@Insectos/shorts",
)

NEGATIVE_TITLE = re.compile(
    r"(#ad\b|sponsored|giveaway|promo\b|free\s+diamond|skin\s+gratis|"
    r"log\s*in\s+mlbb|mailbox|official\s+event|allstar|collab|cctv|"
    r"tutorial|guide|tips|funny|meme|intro|reaction|dance|tiktok|"
    r"rank\s+push\s+only|lobby|menu|event|login|diamond|free\s+skin|"
    r"sound\s*effect|sfx\b|notification\s*sound|ringtone|audio\s*only|"
    r"kill\s*sound|voice\s*line|ost\b|music\s*only|wallpaper|thumbnail|"
    r"hero\s*(reveal|showcase|preview|intro|spawn|appearance)|skin\s*reveal|"
    r"character\s*preview|cinematic|new\s*hero|spawn\s*preview|hero\s*spawn)",
    re.I,
)

PROFILE = "mobile_legends"


MLBB_POSITIVE_TITLE = re.compile(
    r"(mobile\s*legends|\bmlbb\b|bang\s*bang|savage|maniac|mythic|ranked|teamfight|"
    r"double\s*kill|triple\s*kill|chou|gusion|fanny|ling|brody|beatrix)",
    re.I,
)


def _title_looks_mlbb(title: str) -> bool:
    return bool(MLBB_POSITIVE_TITLE.search(title or ""))


def passes_mlbb_shorts_identity_gate(path: Path, *, title: str = "") -> tuple[bool, str]:
    """Mandatory MLBB-only check — always run before owner send (even in lenient mode)."""
    from gameplay_gate import (
        is_gameplay_video,
        path_blocked_by_calibration,
        score_segment_combat,
        segment_minimap_presence_rate,
        source_has_valid_gameplay_window,
    )

    label = title or path.stem
    if path_blocked_by_calibration(path):
        return False, "calibration_bad"
    if NEGATIVE_TITLE.search(label):
        return False, "negative_title"
    min_heuristic = float(os.environ.get("MLBB_SHORTS_MIN_GAMEPLAY_HEURISTIC", "0.78"))
    ok, gscore, reason = is_gameplay_video(
        path, csv_lookup={}, description=label, min_score=min_heuristic
    )
    if not ok:
        return False, f"not_mlbb:{reason}"
    dur = _ffprobe_duration(path)
    window = min(12.0, max(4.0, dur * 0.85))
    ok_win, win_reason = source_has_valid_gameplay_window(
        path, profile=PROFILE, windows=3, window_sec=min(10.0, window)
    )
    if not ok_win:
        return False, f"not_live_match:{win_reason}"
    motion, mini, _skill, center_text = score_segment_combat(path, 0.0, window)
    min_motion = float(os.environ.get("MLBB_IDENTITY_MIN_MOTION", "0.012"))
    min_mini = float(os.environ.get("MLBB_IDENTITY_MIN_MINIMAP", "0.006"))
    if mini < min_mini and motion < min_motion:
        return False, f"no_mlbb_hud mini={mini:.3f} motion={motion:.3f}"
    if center_text > float(os.environ.get("MLBB_IDENTITY_MAX_CENTER_TEXT", "0.42")):
        return False, f"text_heavy={center_text:.3f}"
    mini_pres = segment_minimap_presence_rate(path, 0.0, window, sample_frames=3)
    if mini_pres < float(os.environ.get("MLBB_IDENTITY_MIN_MINIMAP_PRESENCE", "0.55")):
        return False, f"no_minimap={mini_pres:.2f}"
    hud_delta = max(mini, _skill)
    min_hud_delta = float(os.environ.get("MLBB_IDENTITY_MIN_HUD_DELTA", "0.004"))
    if hud_delta < min_hud_delta and motion < float(os.environ.get("MLBB_IDENTITY_MIN_MOTION", "0.012")) * 1.2:
        return False, f"static_hud motion={motion:.3f} hud_delta={hud_delta:.4f}"
    return True, "ok"


def passes_mlbb_shorts_activity_gate(path: Path, *, title: str = "") -> tuple[bool, str]:
    """Reject static image + music slides — mandatory before owner send."""
    from gameplay_gate import score_segment_combat, segment_music_bed_score

    dur = _ffprobe_duration(path)
    window = min(15.0, max(5.0, dur * 0.92))
    motion, mini, skill, center_text = score_segment_combat(path, 0.0, window, sample_frames=8)
    min_motion = float(os.environ.get("MLBB_ACTIVITY_MIN_MOTION", "0.018"))
    min_hud_delta = float(os.environ.get("MLBB_ACTIVITY_MIN_HUD_DELTA", "0.0045"))
    hud_delta = max(mini, skill)

    if motion < min_motion and hud_delta < min_hud_delta:
        return False, f"static_slide motion={motion:.3f} hud_delta={hud_delta:.4f}"

    bed = segment_music_bed_score(path, 0.0, window)
    max_bed = float(os.environ.get("MLBB_SHORTS_MAX_MUSIC_BED", "0.42"))
    if bed >= max_bed and motion < min_motion * 1.25:
        return False, f"music_slide bed={bed:.2f} motion={motion:.3f}"

    if center_text > 0.28 and motion < min_motion and hud_delta < min_hud_delta * 1.5:
        return False, f"text_slide text={center_text:.2f}"

    if dur > 7.0:
        mid = max(0.5, dur * 0.25)
        m2, mini2, skill2, _ct2 = score_segment_combat(
            path, mid, min(10.0, dur - mid - 0.2), sample_frames=6
        )
        if m2 < min_motion * 0.9 and max(mini2, skill2) < min_hud_delta:
            return False, f"static_mid motion={m2:.3f}"

    # Hero spawn cinematic: character animates but minimap/skills stay frozen.
    cinematic_motion = float(os.environ.get("MLBB_ACTIVITY_CINEMATIC_MOTION", "0.014"))
    if motion >= cinematic_motion and hud_delta < min_hud_delta * 0.8:
        return False, f"cinematic_hud_dead motion={motion:.3f} hud_delta={hud_delta:.4f}"

    return True, "ok"


def passes_mlbb_shorts_gameplay_gate(path: Path, *, title: str = "") -> tuple[bool, str]:
    """Reject hero spawn preview, showcase, lobby — not live teamfight."""
    from gameplay_gate import (
        detect_game_viewport_crop,
        score_segment_combat,
        segment_looks_like_draft_or_queue,
        segment_looks_like_hero_showcase,
        segment_looks_like_promo_or_cinematic,
        segment_minimap_presence_rate,
    )

    label = title or path.stem
    dur = _ffprobe_duration(path)
    window = min(15.0, max(5.0, dur * 0.92))
    crop = detect_game_viewport_crop(path, 0.0, window)

    if segment_looks_like_hero_showcase(
        path, 0.0, window, crop_box=crop, sample_frames=6
    ):
        return False, "hero_showcase"

    if segment_looks_like_promo_or_cinematic(
        path, 0.0, window, crop_box=crop, sample_frames=6
    ):
        return False, "promo_cinematic"

    if segment_looks_like_draft_or_queue(path, 0.0, window, crop_box=crop):
        return False, "spawn_or_draft"

    motion, mini, skill, _center_text = score_segment_combat(
        path, 0.0, window, crop_box=crop, sample_frames=8
    )
    hud_delta = max(mini, skill)
    min_motion = float(os.environ.get("MLBB_GAMEPLAY_MIN_MOTION", "0.020"))
    min_hud_delta = float(os.environ.get("MLBB_GAMEPLAY_MIN_HUD_DELTA", "0.005"))
    if motion >= min_motion * 0.6 and hud_delta < min_hud_delta * 0.85:
        return False, f"hero_spawn_cinematic motion={motion:.3f} hud={hud_delta:.4f}"

    mini_pres = segment_minimap_presence_rate(
        path, 0.0, window, crop_box=crop, sample_frames=5
    )
    if mini_pres < float(os.environ.get("MLBB_GAMEPLAY_MIN_MINIMAP_PRES", "0.68")):
        if motion < min_motion * 1.1 or hud_delta < min_hud_delta * 1.2:
            return False, f"weak_match mini_pres={mini_pres:.2f}"

    return True, "ok"


def passes_mlbb_shorts_verify_gate(path: Path, *, title: str = "") -> tuple[bool, str]:
    """MLBB scorer + HUD verify — blocks other MOBAs that mimic generic minimap layout."""
    from gameplay_gate import (
        detect_game_viewport_crop,
        reject_example_similarity,
        source_has_valid_gameplay_window,
    )

    dur = _ffprobe_duration(path)
    window = min(WINDOW_SEC, max(4.0, dur * 0.85))
    start = 0.15
    crop = detect_game_viewport_crop(path, start, window)
    reject_sim = reject_example_similarity(path, start, window, crop_box=crop)
    max_reject = float(os.environ.get("MLBB_SHORTS_MAX_REJECT_SIM", "0.72"))
    if reject_sim >= max_reject:
        return False, f"reject_example_sim={reject_sim:.2f}"

    m = score_candidate_window(path, start, window, PROFILE)

    if not m.visual_pass:
        return False, f"visual:{m.pass_reason or 'fail'}"

    min_mini = float(os.environ.get("MLBB_VERIFY_MIN_MINIMAP", "0.011"))
    min_skill = float(os.environ.get("MLBB_VERIFY_MIN_SKILL", "0.0065"))
    if m.minimap_delta < min_mini or m.skill_delta < min_skill:
        return False, f"hud_not_mlbb mini={m.minimap_delta:.3f} skill={m.skill_delta:.3f}"

    min_clip = float(os.environ.get("MLBB_SHORTS_MIN_CLIP_SCORE", "0.03"))
    min_combined = float(os.environ.get("MLBB_SHORTS_MIN_COMBINED", "0.14"))
    if m.clip_score < min_clip and m.combined_score < min_combined:
        return False, f"exemplar_miss clip={m.clip_score:.3f} combined={m.combined_score:.3f}"

    ok_win, win_reason = source_has_valid_gameplay_window(
        path, profile=PROFILE, windows=4, window_sec=min(10.0, window)
    )
    if not ok_win:
        return False, f"montage_fail:{win_reason}"

    has_kill = False
    kill_score = 0.0
    try:
        from mlbb_kill_ui import score_mlbb_kill_ui

        kill = score_mlbb_kill_ui(path, start, window, sample_frames=6, strict=False)
        has_kill = bool(kill.has_kill_notification)
        kill_score = float(kill.score)
    except ImportError:
        pass

    strict = os.environ.get("MLBB_SHORTS_STRICT_VERIFY", "1") == "1"
    require_kill = os.environ.get("MLBB_SHORTS_REQUIRE_KILL_UI", "1") == "1"

    if strict and not m.rule_pass:
        return False, f"rule_fail:{m.pass_reason} kill={kill_score:.2f}"

    if require_kill and not has_kill:
        if m.combined_score < float(os.environ.get("MLBB_SHORTS_MIN_COMBINED_NO_KILL", "0.22")):
            return False, f"no_kill_ui combined={m.combined_score:.3f}"

    if not has_kill and m.combined_score < min_combined:
        return False, f"weak_combat combined={m.combined_score:.3f}"

    return True, "ok"


def passes_shorts_calibration_gate(path: Path, *, title: str = "") -> tuple[bool, str]:
    """Reject static slides, SFX compilations, and non-gameplay before owner send."""
    from gameplay_gate import score_segment_combat

    id_ok, id_reason = passes_mlbb_shorts_identity_gate(path, title=title)
    if not id_ok:
        return False, id_reason

    act_ok, act_reason = passes_mlbb_shorts_activity_gate(path, title=title)
    if not act_ok:
        return False, act_reason

    gp_ok, gp_reason = passes_mlbb_shorts_gameplay_gate(path, title=title)
    if not gp_ok:
        return False, gp_reason

    ver_ok, ver_reason = passes_mlbb_shorts_verify_gate(path, title=title)
    if not ver_ok:
        return False, ver_reason

    label = title or path.stem
    dur = _ffprobe_duration(path)
    window = min(15.0, max(4.0, dur * 0.9))
    motion, mini, skill, center_text = score_segment_combat(path, 0.0, window)
    min_motion = float(os.environ.get("MLBB_SHORTS_MIN_MOTION", "0.016"))
    if motion < min_motion and mini < float(os.environ.get("MLBB_SHORTS_MIN_MINIMAP", "0.008")):
        return False, f"static_motion={motion:.3f}"
    if center_text > float(os.environ.get("MLBB_SHORTS_MAX_CENTER_TEXT", "0.38")):
        return False, f"text_heavy={center_text:.3f}"
    if center_text > 0.32 and motion < min_motion * 1.15:
        return False, f"text_slide={center_text:.3f}"
    if motion < float(os.environ.get("MLBB_SHORTS_MIN_GAMEPLAY_MOTION", "0.028")):
        return False, f"low_action={motion:.3f}"
    try:
        from mlbb_kill_ui import score_mlbb_kill_ui

        kill = score_mlbb_kill_ui(path, 0.15, window, sample_frames=4)
        if not kill.has_kill_notification and motion < min_motion * 1.6:
            return False, f"no_kill_ui:{kill.reason}"
    except ImportError:
        pass
    return True, "ok"


def _ffprobe_duration(path: Path) -> float:
    import subprocess

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




def fetch_streamer_shorts(channel_url: str, *, limit: int, env: dict[str, str], days: int) -> list[dict]:
    import subprocess

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y%m%d")
    cmd = ytdlp_cmd(env, use_proxy=False) + [
        channel_url,
        "--flat-playlist",
        "--playlistend",
        str(max(limit * 3, 40)),
        "--sleep-requests",
        env.get("YTDLP_SLEEP_REQUESTS", "1.5"),
        "--print",
        "%(id)s\t%(title)s\t%(view_count)s\t%(duration)s\t%(upload_date)s\t%(webpage_url)s",
        "--no-download",
        *ytdlp_extra_args(env),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, check=False, timeout=240, env=subprocess_env_no_proxy(env)
    )
    entries: list[dict] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        vid, title, views, dur, upload_date, url = parts[:6]
        if not vid or len(vid) != 11:
            continue
        if upload_date and upload_date not in ("NA", "N/A") and upload_date.isdigit() and upload_date < cutoff:
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
        entries.append(
            {
                "video_id": vid,
                "title": title[:240],
                "view_count": view_count,
                "duration": duration,
                "upload_date": upload_date,
                "url": url or f"https://www.youtube.com/shorts/{vid}",
                "search_query": channel_url,
                "source_type": "streamer_channel",
            }
        )
        if len(entries) >= limit:
            break
    return entries


def search_shorts(query: str, *, limit: int, env: dict[str, str], days: int) -> list[dict]:
    import subprocess

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y%m%d")
    search_n = max(limit * 8, 80)
    cmd = ytdlp_cmd(env, use_proxy=False) + [
        f"ytsearch{search_n}:{query} #shorts",
        "--flat-playlist",
        "--sleep-requests",
        env.get("YTDLP_SLEEP_REQUESTS", "1.5"),
        "--print",
        "%(id)s\t%(title)s\t%(view_count)s\t%(duration)s\t%(upload_date)s\t%(webpage_url)s",
        "--no-download",
        *ytdlp_extra_args(env),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, check=False, timeout=180, env=subprocess_env_no_proxy(env)
    )
    entries: list[dict] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        vid, title, views, dur, upload_date, url = parts[:6]
        if not vid or len(vid) != 11:
            continue
        if upload_date and upload_date not in ("NA", "N/A") and upload_date.isdigit() and upload_date < cutoff:
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
        entries.append(
            {
                "video_id": vid,
                "title": title[:240],
                "view_count": view_count,
                "duration": duration,
                "upload_date": upload_date,
                "url": url or f"https://www.youtube.com/shorts/{vid}",
                "search_query": query,
            }
        )
        if len(entries) >= limit:
            break
    return entries


def download_short(url: str, out_dir: Path, env: dict[str, str], video_id: str) -> Path | None:
    import subprocess

    out_dir.mkdir(parents=True, exist_ok=True)
    template = str(out_dir / "yt_%(id)s.%(ext)s")
    date_after = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y%m%d")
    cmd = ytdlp_cmd(env, use_proxy=False) + [
        "-f",
        env.get(
            "YOUTUBE_SHORTS_FORMAT",
            "bv*[vcodec^=avc1][height<=1080]+ba/bv*[height<=1080]+ba/b[height<=720]/b",
        ),
        "--merge-output-format",
        "mp4",
    ]
    if env.get("MLBB_SHORTS_CALIBRATION_BURST", "0") != "1":
        cmd.extend(["--dateafter", date_after])
    cmd.extend(
        [
        "--sleep-requests",
        env.get("YTDLP_SLEEP_REQUESTS", "1.5"),
        "--sleep-interval",
        env.get("YTDLP_SLEEP_INTERVAL", "4"),
        "--max-sleep-interval",
        env.get("YTDLP_MAX_SLEEP_INTERVAL", "12"),
        "-o",
        template,
        "--no-playlist",
        *ytdlp_extra_args(env),
        url,
        ]
    )
    dest = out_dir / f"yt_{video_id}.mp4"
    if dest.exists():
        return dest
    proc = subprocess.run(
        cmd, capture_output=True, text=True, check=False, timeout=300, env=subprocess_env_no_proxy(env)
    )
    if proc.returncode != 0:
        if env.get("MLBB_SHORTS_CALIBRATION_BURST", "0") == "1":
            err = (proc.stderr or proc.stdout or "")[-200:]
            print(f"download failed {video_id}: {err}", flush=True)
        return None
    if dest.exists():
        if not ensure_readable(dest):
            dest.unlink(missing_ok=True)
            return None
        return dest
    return None


def score_clip(path: Path) -> dict:
    os.environ.setdefault("HIGHLIGHT_HEATMAP", "0")
    os.environ.setdefault("HIGHLIGHT_USE_OWNER_ANCHORS", "0")
    dur = _ffprobe_duration(path)
    window = min(WINDOW_SEC, max(4.0, dur * 0.85))
    m = score_candidate_window(path, 0.15, window, PROFILE)
    hook, hook_meta = hook_score(path, 0.15, PROFILE, duration_sec=window)
    combat = (
        float(m.panns_gun_max) * 0.25
        + max(0.0, float(m.clip_score)) * 0.35
        + float(m.minimap_delta) * 2.0
        + float(m.skill_delta) * 2.0
        + hook * 0.25
    )
    kill_score = 0.0
    kill_pass = 0
    kill_reason = ""
    try:
        from mlbb_kill_ui import score_mlbb_kill_ui

        kill = score_mlbb_kill_ui(path, 0.15, window, sample_frames=6)
        kill_score = float(kill.score)
        kill_pass = int(kill.has_kill_notification)
        kill_reason = kill.reason
    except ImportError:
        pass
    gate = bool(m.rule_pass and m.visual_pass)
    combined = combat + (0.15 if gate else 0.0) + kill_score * 0.35
    return {
        "score": round(combined, 4),
        "kill_ui_score": round(kill_score, 4),
        "kill_ui_pass": kill_pass,
        "kill_ui_reason": kill_reason,
        "combat_score": round(combat, 4),
        "clip_score": round(float(m.clip_score), 4),
        "hook_score": round(hook, 4),
        "panns_gun_max": round(float(m.panns_gun_max), 4),
        "minimap_delta": round(float(m.minimap_delta), 4),
        "skill_delta": round(float(m.skill_delta), 4),
        "rule_pass": int(gate),
        "pass_reason": m.pass_reason or "",
        "hook_menu": hook_meta.get("menu_overlay", 0),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-per-query", type=int, default=30)
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--min-score", type=float, default=0.12)
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="Throttled cron mode: 1 query, few downloads, long pauses",
    )
    parser.add_argument("--max-downloads", type=int, default=0, help="0 = no limit")
    parser.add_argument("--download-delay", type=float, default=12.0)
    parser.add_argument("--search-delay", type=float, default=5.0)
    parser.add_argument(
        "--skip-if-pending",
        type=int,
        default=0,
        help="Skip YouTube if this many unevaluated candidates already queued",
    )
    args = parser.parse_args()

    burst = os.environ.get("MLBB_SHORTS_CALIBRATION_BURST", "0") == "1"
    if args.incremental and burst:
        if args.max_downloads <= 0:
            args.max_downloads = int(os.environ.get("MLBB_INGEST_MAX_DOWNLOADS", "40"))
        args.max_per_query = int(os.environ.get("MLBB_INGEST_MAX_PER_QUERY", str(args.max_per_query)))
        args.skip_if_pending = 0
        args.download_delay = float(os.environ.get("MLBB_INGEST_DOWNLOAD_DELAY", "5"))
        args.search_delay = float(os.environ.get("MLBB_INGEST_SEARCH_DELAY", "2"))
    elif args.incremental:
        if args.max_downloads <= 0:
            args.max_downloads = int(os.environ.get("MLBB_INGEST_MAX_DOWNLOADS", "3"))
        if args.max_per_query > 12:
            args.max_per_query = 12
        if args.skip_if_pending <= 0:
            args.skip_if_pending = int(os.environ.get("MLBB_INGEST_SKIP_IF_PENDING", "12"))

    os.environ.setdefault("HIGHLIGHT_HEATMAP", "0")
    os.environ.setdefault("CONTENT_BOT_REPO", "/root/content_bot_ml")
    env = {**os.environ, **load_env()}
    SHORTS_ROOT.mkdir(parents=True, exist_ok=True)
    pruned = repair_index()
    if pruned:
        print(f"repair_index removed={pruned}")
    rebuilt = rebuild_index_from_disk()
    if rebuilt:
        print(f"rebuild_index_from_disk added={rebuilt}")

    pending_n = len(pending_candidates(limit=9999))
    if args.skip_if_pending > 0 and pending_n >= args.skip_if_pending:
        print(f"SKIP ingest pending={pending_n} >= {args.skip_if_pending} (no YouTube calls)")
        return 0

    queries = list(SEARCH_QUERIES)
    if args.incremental and not burst:
        # Rotate one query per run — less search load on YouTube.
        slot = int(time.time() // 10800) % len(queries)  # ~3h rotation
        queries = [queries[slot]]
        print(f"incremental query={queries[0]} pending={pending_n}")
    elif burst:
        print(f"calibration_burst queries={len(queries)} pending={pending_n}")

    seen: set[str] = set()
    pool: list[dict] = []
    channel_feeds = list(STREAMER_SHORTS_FEEDS)
    if args.incremental and channel_feeds and not burst:
        slot = int(time.time() // 7200) % len(channel_feeds)
        channel_feeds = [channel_feeds[slot]]
        print(f"incremental channel={channel_feeds[0]}")
    elif burst:
        print(f"calibration_burst channels={len(channel_feeds)}")

    streamer_only = os.environ.get("MLBB_SHORTS_STREAMER_ONLY", "1") == "1"
    if streamer_only:
        queries = []
        print(f"streamer_only=1 channels={len(channel_feeds)} (no ytsearch)")

    for channel_url in channel_feeds:
        for row in fetch_streamer_shorts(
            channel_url, limit=args.max_per_query, env=env, days=args.days
        ):
            vid = row["video_id"]
            if vid in seen:
                continue
            seen.add(vid)
            pool.append(row)
        if args.search_delay > 0:
            time.sleep(args.search_delay)
    for query in queries:
        for row in search_shorts(query, limit=args.max_per_query, env=env, days=args.days):
            vid = row["video_id"]
            if vid in seen:
                continue
            if not _title_looks_mlbb(str(row.get("title", ""))):
                continue
            seen.add(vid)
            pool.append(row)
        if args.search_delay > 0 and len(queries) > 1:
            time.sleep(args.search_delay)

    pool.sort(key=lambda r: int(r.get("view_count") or 0), reverse=True)
    cap = args.max_per_query * len(queries)
    pool = pool[: cap * 3]  # extra headroom — many rows already labeled

    known = labeled_ids()
    from mlbb_calibration_store import load_feed_sent

    already_sent = load_feed_sent()["ids"]
    sent_pending = {str(r.get("video_id", "")) for r in pending_candidates(limit=9999)}
    fresh_pool: list[dict] = []
    for row in pool:
        vid = row["video_id"]
        if vid in known:
            continue
        if vid in sent_pending or vid in already_sent:
            continue
        fresh_pool.append(row)
    pool = fresh_pool[:cap]

    if not pool and args.incremental:
        deep: list[dict] = []
        for query in queries:
            for row in search_shorts(
                query,
                limit=max(args.max_per_query * 4, 40),
                env=env,
                days=args.days,
            ):
                vid = row["video_id"]
                if vid in known or vid in sent_pending or vid in already_sent:
                    continue
                deep.append(row)
            if args.search_delay > 0:
                time.sleep(args.search_delay)
        pool = deep[: cap * 2]

    saved = rejected = downloads = skipped_known = 0
    min_score = float(os.environ.get("MLBB_CALIBRATION_MIN_SCORE", "0.05" if burst else "0.12"))
    for row in pool:
        if args.max_downloads > 0 and downloads >= args.max_downloads:
            break
        vid = row["video_id"]
        if vid in known:
            skipped_known += 1
            continue
        mp4 = SHORTS_ROOT / f"yt_{vid}.mp4"
        if not mp4.exists() and not args.skip_download:
            mp4 = download_short(row["url"], SHORTS_ROOT, env, vid) or mp4
            downloads += 1
            time.sleep(max(2.0, args.download_delay))
        if not mp4.exists() or mp4.name != f"yt_{vid}.mp4":
            continue

        if NEGATIVE_TITLE.search(row.get("title", "")):
            rejected += 1
            continue

        id_ok, id_reason = passes_mlbb_shorts_identity_gate(mp4, title=row.get("title", ""))
        if not id_ok:
            print(f"REJECT {vid} identity={id_reason}", flush=True)
            rejected += 1
            continue

        act_ok, act_reason = passes_mlbb_shorts_activity_gate(mp4, title=row.get("title", ""))
        if not act_ok:
            print(f"REJECT {vid} activity={act_reason}", flush=True)
            rejected += 1
            continue

        gp_ok, gp_reason = passes_mlbb_shorts_gameplay_gate(mp4, title=row.get("title", ""))
        if not gp_ok:
            print(f"REJECT {vid} gameplay={gp_reason}", flush=True)
            rejected += 1
            continue

        ver_ok, ver_reason = passes_mlbb_shorts_verify_gate(mp4, title=row.get("title", ""))
        if not ver_ok:
            print(f"REJECT {vid} verify={ver_reason}", flush=True)
            rejected += 1
            continue

        lenient = os.environ.get("MLBB_CALIBRATION_LENIENT", "1") == "1"
        if not lenient:
            gate_ok, gate_reason = passes_shorts_calibration_gate(mp4, title=row.get("title", ""))
            if not gate_ok:
                print(f"REJECT {vid} gate={gate_reason}", flush=True)
                rejected += 1
                continue

        feats = score_clip(mp4)
        if int(feats.get("rule_pass") or 0) != 1:
            print(f"REJECT {vid} rule_pass=0 {feats.get('pass_reason','')}", flush=True)
            rejected += 1
            continue
        if feats["score"] < min_score and not feats["rule_pass"] and not lenient:
            rejected += 1
            continue

        upsert_candidate(
            {
                **row,
                **feats,
                "path": str(mp4),
                "gameplay_pass": 1,
                "identity_pass": 1,
                "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        saved += 1
        print(f"OK {vid} score={feats['score']:.3f} views={row.get('view_count')} {row.get('title','')[:50]}")

    print(
        f"SUMMARY saved={saved} rejected={rejected} downloads={downloads} skipped_known={skipped_known} "
        f"pool={len(pool)} pending={pending_n} dir={SHORTS_ROOT}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
