#!/usr/bin/env python3
"""
MLBB-only: ingest YouTube Shorts and MLBB highlight clips for owner calibration.

Duration: MLBB_SHORTS_MIN_DURATION_SEC .. MLBB_SHORTS_MAX_DURATION_SEC (default 3s–20min).
Long clips are trimmed to ~45s for Telegram; full file kept for training archive on 👍.
"""

from __future__ import annotations

import argparse
import fcntl
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
    DATA_MLBB,
    SHORTS_ROOT,
    ingest_skip_ids,
    labeled_ids,
    mark_ingest_skip,
    pending_candidates,
    rebuild_index_from_disk,
    repair_index,
    upsert_candidate,
)
from viral_scorer import hook_score
from youtube_download import load_env, subprocess_env_no_proxy, ytdlp_cmd, ytdlp_extra_args
from youtube_video_fix import ensure_readable

SEARCH_QUERIES = (
    "mlbb ranked gameplay savage 2026",
    "mobile legends mythic rank teamfight gameplay",
    "mlbb solo rank maniac double kill",
    "mobile legends bang bang ranked match highlights",
    "mlbb savage maniac shorts gameplay",
    "mlbb teamfight ranked epic comeback",
    "mobile legends streamer ranked gameplay",
    "mlbb double kill triple kill savage",
)

# Vertical Shorts / phone recordings — always searched with #shorts suffix.
VERTICAL_SHORTS_QUERIES = (
    "mlbb savage",
    "mobile legends maniac",
    "mlbb ranked gameplay",
    "mlbb double kill savage",
    "mobile legends mythic rank",
    "mlbb teamfight savage",
)

# Owner-pasted Chou / curated channels — searched last (often already labeled/sent).
OWNER_CURATED_FEEDS = (
    "https://www.youtube.com/@hanz.legends/shorts",
    "https://www.youtube.com/@silent_chou/shorts",
    "https://www.youtube.com/@officiallazychouu/shorts",
    "https://www.youtube.com/@rikkchoou/shorts",
    "https://www.youtube.com/@kyro-plays-o/shorts",
    "https://www.youtube.com/@run-yss/shorts",
)

GENERAL_MLBB_FEEDS = (
    "https://www.youtube.com/@Betosky/shorts",
    "https://www.youtube.com/@JessNoLimit/shorts",
    "https://www.youtube.com/@akosidogie/shorts",
)

STREAMER_SHORTS_FEEDS = OWNER_CURATED_FEEDS + GENERAL_MLBB_FEEDS

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


def shorts_upload_cutoff(env: dict[str, str] | None = None, *, days: int | None = None) -> str:
    """Earliest allowed upload date YYYYMMDD — 2026 floor + rolling window."""
    env = env or dict(os.environ)
    min_date = str(env.get("MLBB_SHORTS_MIN_UPLOAD_DATE", "20260101")).strip()
    day_n = days if days is not None else int(env.get("MLBB_SHORTS_INGEST_DAYS", "365"))
    rolling = (datetime.now(timezone.utc) - timedelta(days=day_n)).strftime("%Y%m%d")
    if min_date.isdigit() and len(min_date) == 8:
        return max(min_date, rolling)
    return rolling


def shorts_ytdlp_date_after(env: dict[str, str] | None = None) -> str | None:
    """yt-dlp --dateafter; None = skip filter."""
    env = env or dict(os.environ)
    if env.get("MLBB_SHORTS_SKIP_DATE_FILTER", "0") == "1":
        return None
    explicit = str(env.get("MLBB_SHORTS_DATE_AFTER", "")).strip()
    if explicit.isdigit() and len(explicit) == 8:
        return explicit
    return shorts_upload_cutoff(env)


MLBB_POSITIVE_TITLE = re.compile(
    r"(mobile\s*legends|\bmlbb\b|bang\s*bang|savage|maniac|mythic|ranked|teamfight|"
    r"double\s*kill|triple\s*kill|chou|gusion|fanny|ling|brody|beatrix)",
    re.I,
)


def _title_looks_mlbb(title: str) -> bool:
    return bool(MLBB_POSITIVE_TITLE.search(title or ""))


def shorts_min_duration_sec(env: dict[str, str] | None = None) -> float:
    env = env or dict(os.environ)
    return float(env.get("MLBB_SHORTS_MIN_DURATION_SEC", "3"))


def shorts_max_duration_sec(env: dict[str, str] | None = None) -> float:
    env = env or dict(os.environ)
    return float(env.get("MLBB_SHORTS_MAX_DURATION_SEC", "1200"))


def shorts_short_max_sec(env: dict[str, str] | None = None) -> float:
    """Clips at or below this length use Shorts-style opening trim (default 60s)."""
    env = env or dict(os.environ)
    return float(env.get("MLBB_SHORTS_SHORT_MAX_SEC", "60"))


def duration_in_ingest_range(duration: float, env: dict[str, str] | None = None) -> bool:
    return shorts_min_duration_sec(env) < duration <= shorts_max_duration_sec(env)


def streamer_channel_urls() -> list[str]:
    owner_last = os.environ.get("MLBB_OWNER_CHANNELS_LAST", "1") == "1"
    include_owner = os.environ.get("MLBB_OWNER_CHANNELS_DISABLED", "0") != "1"
    if owner_last:
        base = list(GENERAL_MLBB_FEEDS)
        if include_owner:
            base.extend(OWNER_CURATED_FEEDS)
    else:
        base = list(OWNER_CURATED_FEEDS) + list(GENERAL_MLBB_FEEDS) if include_owner else list(GENERAL_MLBB_FEEDS)
    urls = list(base)
    if os.environ.get("MLBB_SHORTS_INCLUDE_VIDEOS_TAB", "1") == "1":
        extra: list[str] = []
        for url in base:
            if url.endswith("/shorts"):
                videos = url[: -len("/shorts")] + "/videos"
                if videos not in urls and videos not in extra:
                    extra.append(videos)
        urls.extend(extra)
    return urls


def _limit_owner_channel_feeds(feeds: list[str], *, limit: int) -> list[str]:
    if limit <= 0 or os.environ.get("MLBB_OWNER_CHANNELS_DISABLED", "0") == "1":
        owner_markers = tuple(u.replace("/shorts", "") for u in OWNER_CURATED_FEEDS)
        return [u for u in feeds if not any(m in u for m in owner_markers)]
    owner_markers = tuple(u.replace("/shorts", "") for u in OWNER_CURATED_FEEDS)
    general: list[str] = []
    owner: list[str] = []
    for url in feeds:
        if any(m in url for m in owner_markers):
            owner.append(url)
        else:
            general.append(url)
    return general + owner[: max(limit * 2, limit)]


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

        kill = score_mlbb_kill_ui(
            path,
            start,
            window,
            sample_frames=int(os.environ.get("MLBB_SHORTS_KILL_SAMPLE_FRAMES", "4")),
            strict=False,
        )
        has_kill = bool(kill.has_kill_notification)
        kill_score = float(kill.score)
    except ImportError:
        pass

    strict = os.environ.get("MLBB_SHORTS_STRICT_VERIFY", "1") == "1"
    require_kill = os.environ.get("MLBB_SHORTS_REQUIRE_KILL_UI", "1") == "1"

    if strict and not m.rule_pass:
        return False, f"rule_fail:{m.pass_reason} kill={kill_score:.2f}"

    if require_kill and not has_kill:
        return False, f"mlbb_kill_required score={kill_score:.2f} combined={m.combined_score:.3f}"

    if m.combined_score < min_combined:
        return False, f"weak_combat combined={m.combined_score:.3f}"

    return True, "ok"


def passes_mlbb_shorts_kill_ui_gate(path: Path, *, start_sec: float = 0.15) -> tuple[bool, str]:
    """Hard MLBB discriminator — Savage/Maniac/kill feed OCR (other MOBAs fail)."""
    if os.environ.get("MLBB_SHORTS_REQUIRE_KILL_UI", "1") != "1":
        return True, "kill_ui_skipped"
    dur = _ffprobe_duration(path)
    window = min(WINDOW_SEC, max(4.0, dur * 0.85))
    try:
        from mlbb_kill_ui import score_mlbb_kill_ui

        kill = score_mlbb_kill_ui(
            path,
            start_sec,
            window,
            sample_frames=int(os.environ.get("MLBB_SHORTS_KILL_SAMPLE_FRAMES", "4")),
            strict=True,
        )
    except ImportError:
        return True, "kill_ui_unavailable"
    min_score = float(os.environ.get("MLBB_SHORTS_MIN_KILL_SCORE", "0.18"))
    if kill.has_kill_notification:
        return True, kill.reason or "kill_ok"
    if float(kill.score) >= min_score:
        return True, f"kill_score={kill.score:.2f}"
    return False, f"no_mlbb_kill_ui:{kill.reason} score={kill.score:.2f}"


def verify_shorts_send_file(path: Path, *, title: str = "") -> tuple[bool, str]:
    """Verify mp4 before Telegram."""
    lenient = os.environ.get("MLBB_CALIBRATION_LENIENT", "1") == "1"
    if lenient:
        act_ok, act_reason = passes_mlbb_shorts_activity_gate(path, title=title)
        if not act_ok:
            return False, act_reason
        return passes_mlbb_shorts_kill_ui_gate(path)
    for check in (
        passes_mlbb_shorts_identity_gate,
        passes_mlbb_shorts_activity_gate,
        passes_mlbb_shorts_verify_gate,
    ):
        ok, reason = check(path, title=title)
        if not ok:
            return False, reason
    return True, "ok"


def _opening_window_junk(
    path: Path,
    start_sec: float,
    *,
    crop_box: tuple[int, int, int, int] | None,
    window_sec: float,
) -> bool:
    from gameplay_gate import (
        score_segment_combat,
        segment_looks_like_draft_or_queue,
        segment_opens_with_training,
        segment_minimap_presence_rate,
    )

    if segment_opens_with_training(path, start_sec, crop_box=crop_box):
        return True
    if segment_looks_like_draft_or_queue(path, start_sec, window_sec, crop_box=crop_box):
        return True
    motion, mini, skill, center_text = score_segment_combat(
        path, start_sec, window_sec, crop_box=crop_box, sample_frames=4
    )
    if motion < float(os.environ.get("MLBB_OPENING_MIN_MOTION", "0.016")) and max(mini, skill) < 0.0045:
        return True
    if center_text > float(os.environ.get("MLBB_OPENING_MAX_TEXT", "0.36")) and motion < 0.02:
        return True
    mini_pres = segment_minimap_presence_rate(
        path, start_sec, window_sec, crop_box=crop_box, sample_frames=3
    )
    if mini_pres < float(os.environ.get("MLBB_OPENING_MIN_MINIMAP_PRES", "0.50")):
        return True
    return False


def find_best_long_clip_start(path: Path) -> tuple[float, str]:
    """Pick best ~45s window in long MLBB uploads (kill peak or combat scan)."""
    from mlbb_kill_ui import scan_vod_kill_peaks

    dur = _ffprobe_duration(path)
    short_max = shorts_short_max_sec()
    if dur <= short_max:
        return find_best_shorts_start(path)

    min_peak = float(os.environ.get("MLBB_CALIB_SCAN_MIN_SEC", "20"))
    step = float(os.environ.get("MLBB_CALIB_SCAN_STEP_SEC", "25"))
    window = float(os.environ.get("MLBB_CALIB_SCAN_WINDOW_SEC", "10"))
    peaks = scan_vod_kill_peaks(
        path,
        min_peak_sec=min_peak,
        step_sec=step,
        window_sec=window,
        limit=int(os.environ.get("MLBB_CALIB_SCAN_LIMIT", "8")),
    )
    if peaks:
        lead = float(os.environ.get("MLBB_VOD_LEAD_SEC", "4"))
        start = max(0.0, float(peaks[0]["start_sec"]) - lead)
        return round(start, 2), "kill_peak"

    crop = None
    from gameplay_gate import detect_game_viewport_crop, score_segment_combat

    crop = detect_game_viewport_crop(path, 0.0, min(dur, 30.0))
    probe = float(os.environ.get("MLBB_LONG_CLIP_PROBE", "4.0"))
    step_combat = float(os.environ.get("MLBB_LONG_CLIP_STEP", "20"))
    scan_end = min(dur * 0.85, float(os.environ.get("MLBB_LONG_CLIP_SCAN_SEC", "600")))
    best_start = -1.0
    best_score = -1.0
    t = 0.0
    while t < scan_end:
        window_sec = min(probe, max(2.5, dur - t - 0.2))
        if window_sec < 2.5:
            break
        motion, mini, skill, _text = score_segment_combat(
            path, t, window_sec, crop_box=crop, sample_frames=4
        )
        score = motion + mini + skill
        if score > best_score:
            best_score = score
            best_start = t
        t += step_combat
    if best_start >= 0 and best_score > 0.02:
        return round(best_start, 2), "combat_scan"
    return -1.0, "no_clip_in_long"


def find_best_shorts_start(path: Path) -> tuple[float, str]:
    """
    Skip junk at t=0 (intro/lobby/menu). Returns (start_sec, reason).
    start_sec < 0 means no clean opening found.
    """
    from gameplay_gate import detect_game_viewport_crop, score_segment_combat, segment_minimap_presence_rate

    dur = _ffprobe_duration(path)
    if dur < 4.5:
        return 0.0, "short_ok"
    crop = detect_game_viewport_crop(path, 0.0, min(dur, 12.0))
    probe = float(os.environ.get("MLBB_SHORTS_OPENING_PROBE", "3.5"))
    step = float(os.environ.get("MLBB_SHORTS_OPENING_STEP", "0.45"))
    max_skip = min(float(os.environ.get("MLBB_SHORTS_MAX_OPENING_SKIP", "7.5")), dur * 0.42)
    min_motion = float(os.environ.get("MLBB_OPENING_MIN_MOTION", "0.016"))

    best_start = -1.0
    best_score = -1.0
    t = 0.0
    while t <= max_skip + 1e-6:
        window = min(probe, max(2.5, dur - t - 0.15))
        if window < 2.5:
            break
        if not _opening_window_junk(path, t, crop_box=crop, window_sec=window):
            motion, mini, skill, _text = score_segment_combat(
                path, t, window, crop_box=crop, sample_frames=5
            )
            mini_pres = segment_minimap_presence_rate(
                path, t, window, crop_box=crop, sample_frames=3
            )
            if motion >= min_motion and mini_pres >= float(os.environ.get("MLBB_OPENING_MIN_MINIMAP_PRES", "0.50")):
                score = motion + mini + skill + mini_pres
                if score > best_score:
                    best_score = score
                    best_start = t
        t += step

    if best_start >= 0:
        if best_start <= 0.2:
            return 0.0, "ok"
        return round(best_start, 2), f"skip_opening@{best_start:.1f}s"

    if _opening_window_junk(path, 0.0, crop_box=crop, window_sec=min(probe, dur * 0.5)):
        return -1.0, "bad_opening"
    return 0.0, "ok"


def passes_mlbb_shorts_opening_gate(path: Path, *, title: str = "") -> tuple[bool, str]:
    start, reason = find_best_shorts_start(path)
    if start < 0:
        return False, reason
    return True, reason


def trim_short_mp4(src: Path, start_sec: float) -> Path | None:
    """Trim junk head; cache under MLBB_CALIBRATION_TRIM_DIR."""
    import subprocess

    dur = _ffprobe_duration(src)
    remain = max(3.0, dur - start_sec - 0.08)
    max_out = float(os.environ.get("MLBB_SHORTS_TRIM_MAX_SEC", "58"))
    out_dur = min(remain, max_out)
    trim_dir = Path(os.environ.get("MLBB_CALIBRATION_TRIM_DIR", "/root/data/mlbb/calibration_trimmed"))
    trim_dir.mkdir(parents=True, exist_ok=True)
    tag = int(round(start_sec * 10))
    dest = trim_dir / f"{src.stem}_from{tag}{src.suffix}"
    if dest.exists() and dest.stat().st_size > 2048:
        return dest
    size_mb = src.stat().st_size / (1024 * 1024)
    timeout = min(600, max(90, int(size_mb * 3)))
    base = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-ss",
        f"{start_sec:.3f}",
        "-i",
        str(src),
        "-t",
        f"{out_dur:.3f}",
        "-movflags",
        "+faststart",
    ]
    for cmd in (
        [*base, "-c", "copy", str(dest)],
        [
            *base,
            "-c:v",
            "libx264",
            "-preset",
            os.environ.get("MLBB_SHORTS_TRIM_PRESET", "veryfast"),
            "-crf",
            os.environ.get("MLBB_SHORTS_TRIM_CRF", "23"),
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(dest),
        ],
    ):
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
        if proc.returncode == 0 and dest.exists() and dest.stat().st_size > 2048:
            return dest
    return None


def resolve_shorts_send_path(
    path: Path, *, clip_start: float | None = None
) -> tuple[Path | None, float, str]:
    """Pick file to send — trim to best clip (Shorts opening or long-video peak)."""
    if os.environ.get("MLBB_CALIBRATION_LENIENT", "1") == "1" and os.environ.get(
        "MLBB_SHORTS_TRIM_OPENING", "1"
    ) != "1":
        return path, 0.0, "lenient_no_trim"
    if os.environ.get("MLBB_SHORTS_TRIM_OPENING", "1") != "1":
        return path, 0.0, "trim_disabled"
    dur = _ffprobe_duration(path)
    if clip_start is not None and clip_start >= 0:
        start, reason = clip_start, "cached_clip"
    elif dur > shorts_short_max_sec():
        start, reason = find_best_long_clip_start(path)
    else:
        start, reason = find_best_shorts_start(path)
    if start < 0:
        return None, 0.0, reason
    min_trim = float(os.environ.get("MLBB_SHORTS_MIN_TRIM_SEND", "0.35"))
    if start < min_trim and dur <= shorts_short_max_sec():
        return path, 0.0, reason
    trimmed = trim_short_mp4(path, start)
    if trimmed is None:
        return None, 0.0, "trim_failed"
    return trimmed, start, reason


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




def _log_ytdlp_fail(proc, label: str) -> None:
    if proc.returncode == 0:
        return
    err = (proc.stderr or proc.stdout or "").strip()[-500:]
    print(f"ytdlp_fail {label} rc={proc.returncode} {err}", flush=True)


def fetch_streamer_shorts(channel_url: str, *, limit: int, env: dict[str, str], days: int) -> list[dict]:
    import subprocess

    cutoff = shorts_upload_cutoff(env, days=days)
    playlist_n = max(limit * 3, 40)
    cmd = ytdlp_cmd(env, use_proxy=False) + [
        channel_url,
        "--flat-playlist",
        "-I",
        f"1:{playlist_n}",
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
    _log_ytdlp_fail(proc, channel_url)
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
        except (ValueError, TypeError):
            duration = 0.0
        try:
            view_count = int(float(views or 0))
        except (ValueError, TypeError):
            view_count = 0
        if duration > 0 and not duration_in_ingest_range(duration, env):
            continue
        if NEGATIVE_TITLE.search(title):
            continue
        if os.environ.get("MLBB_STREAMER_REQUIRE_MLBB_TITLE", "1") == "1" and not _title_looks_mlbb(title):
            continue
        entries.append(
            {
                "video_id": vid,
                "title": title[:240],
                "view_count": view_count,
                "duration": duration,
                "upload_date": upload_date,
                "url": url or f"https://www.youtube.com/watch?v={vid}",
                "search_query": channel_url,
                "source_type": "streamer_channel",
            }
        )
        if len(entries) >= limit:
            break
    return entries


def search_shorts(query: str, *, limit: int, env: dict[str, str], days: int, force_shorts: bool = False) -> list[dict]:
    import subprocess

    cutoff = shorts_upload_cutoff(env, days=days)
    search_n = max(limit * 8, 80)
    max_d = shorts_max_duration_sec(env)
    suffix = " #shorts" if force_shorts or max_d <= shorts_short_max_sec(env) else ""
    cmd = ytdlp_cmd(env, use_proxy=False) + [
        f"ytsearch{search_n}:{query}{suffix}",
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
    _log_ytdlp_fail(proc, query)
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
        except (ValueError, TypeError):
            duration = 0.0
        try:
            view_count = int(float(views or 0))
        except (ValueError, TypeError):
            view_count = 0
        if duration > 0 and not duration_in_ingest_range(duration, env):
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
                "url": url or f"https://www.youtube.com/watch?v={vid}",
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
    date_after = shorts_ytdlp_date_after(env)
    cmd = ytdlp_cmd(env, use_proxy=False) + [
        "-f",
        env.get(
            "YOUTUBE_SHORTS_FORMAT",
            "bv*[vcodec^=avc1][height<=1080]+ba/bv*[height<=1080]+ba/b[height<=720]/b",
        ),
        "--merge-output-format",
        "mp4",
    ]
    if date_after:
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
        cmd, capture_output=True, text=True, check=False, timeout=int(env.get("MLBB_SHORTS_DOWNLOAD_TIMEOUT_SEC", "900")), env=subprocess_env_no_proxy(env)
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


INGEST_LOCK = Path(os.environ.get("MLBB_SHORTS_INGEST_LOCK", str(DATA_MLBB / "youtube_shorts_ingest.lock")))


def _acquire_ingest_lock() -> object | None:
    INGEST_LOCK.parent.mkdir(parents=True, exist_ok=True)
    if INGEST_LOCK.exists():
        try:
            old_pid = int(INGEST_LOCK.read_text(encoding="utf-8").strip())
            os.kill(old_pid, 0)
        except ProcessLookupError:
            INGEST_LOCK.unlink(missing_ok=True)
        except (ValueError, OSError):
            INGEST_LOCK.unlink(missing_ok=True)
    handle = INGEST_LOCK.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        print("skip ingest: another youtube_shorts_ingest is running", flush=True)
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-per-query", type=int, default=30)
    parser.add_argument("--days", type=int, default=int(os.environ.get("MLBB_SHORTS_INGEST_DAYS", "365")))
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

    lock_handle = _acquire_ingest_lock()
    if lock_handle is None:
        return 0
    try:
        return _run_ingest(args)
    finally:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock_handle.close()


def _run_ingest(args: argparse.Namespace) -> int:
    burst = os.environ.get("MLBB_SHORTS_CALIBRATION_BURST", "0") == "1"
    if args.incremental and burst:
        if args.max_downloads <= 0:
            args.max_downloads = int(os.environ.get("MLBB_INGEST_MAX_DOWNLOADS", "8"))
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

    full_sweep = pending_n < int(os.environ.get("MLBB_INGEST_FULL_SWEEP_PENDING", "8"))
    if full_sweep:
        print(f"full_sweep=1 pending={pending_n}", flush=True)

    queries = list(SEARCH_QUERIES)
    if args.incremental and not burst and not full_sweep:
        # Rotate one query per run — less search load on YouTube.
        slot = int(time.time() // 10800) % len(queries)  # ~3h rotation
        queries = [queries[slot]]
        print(f"incremental query={queries[0]} pending={pending_n}")
    elif burst or full_sweep:
        print(f"{'calibration_burst' if burst else 'full_sweep'} queries={len(queries)} pending={pending_n}")

    seen: set[str] = set()
    pool: list[dict] = []
    channel_feeds = streamer_channel_urls()
    if args.incremental and channel_feeds and not burst and not full_sweep:
        slot = int(time.time() // 7200) % len(channel_feeds)
        channel_feeds = [channel_feeds[slot]]
        print(f"incremental channel={channel_feeds[0]}")
    elif burst or full_sweep:
        owner_limit = int(os.environ.get("MLBB_OWNER_CHANNEL_LIMIT", "2"))
        channel_feeds = _limit_owner_channel_feeds(channel_feeds, limit=owner_limit)
        print(f"{'calibration_burst' if burst else 'full_sweep'} channels={len(channel_feeds)}")

    streamer_only = os.environ.get("MLBB_SHORTS_STREAMER_ONLY", "0") == "1"
    if streamer_only:
        queries = []
        print(f"streamer_only=1 channels={len(channel_feeds)} (no ytsearch yet)")

    skip_ids = ingest_skip_ids()
    search_first = os.environ.get("MLBB_SEARCH_BEFORE_STREAMERS", "1") == "1"

    def _collect_search() -> None:
        vertical_on = os.environ.get("MLBB_SHORTS_VERTICAL", "1") == "1"
        if vertical_on:
            vqueries = list(VERTICAL_SHORTS_QUERIES)
            if args.incremental and not burst and not full_sweep:
                vslot = int(time.time() // 5400) % len(vqueries)
                vqueries = [vqueries[vslot]]
            print(f"vertical_shorts queries={len(vqueries)}", flush=True)
            for query in vqueries:
                for row in search_shorts(
                    query,
                    limit=args.max_per_query,
                    env=env,
                    days=args.days,
                    force_shorts=True,
                ):
                    vid = row["video_id"]
                    if vid in seen or vid in skip_ids:
                        continue
                    if not _title_looks_mlbb(str(row.get("title", ""))):
                        continue
                    seen.add(vid)
                    pool.append(row)
                if args.search_delay > 0:
                    time.sleep(args.search_delay)
        for query in queries:
            for row in search_shorts(query, limit=args.max_per_query, env=env, days=args.days):
                vid = row["video_id"]
                if vid in seen or vid in skip_ids:
                    continue
                if not _title_looks_mlbb(str(row.get("title", ""))):
                    continue
                seen.add(vid)
                pool.append(row)
            if args.search_delay > 0 and len(queries) > 1:
                time.sleep(args.search_delay)

    def _collect_channels() -> None:
        for channel_url in channel_feeds:
            for row in fetch_streamer_shorts(
                channel_url, limit=args.max_per_query, env=env, days=args.days
            ):
                vid = row["video_id"]
                if vid in seen or vid in skip_ids:
                    continue
                seen.add(vid)
                pool.append(row)
            if args.search_delay > 0:
                time.sleep(args.search_delay)

    if search_first:
        _collect_search()
    else:
        _collect_channels()

    min_pool = int(os.environ.get("MLBB_SHORTS_MIN_POOL", "8"))
    search_fallback = os.environ.get("MLBB_SHORTS_SEARCH_FALLBACK", "1") == "1"
    if search_fallback and len(pool) < min_pool and queries:
        fallback_queries = list(SEARCH_QUERIES) if not queries else queries
        print(
            f"search_fallback pool={len(pool)}<{min_pool} queries={len(fallback_queries)}",
            flush=True,
        )
        for query in fallback_queries:
            for row in search_shorts(query, limit=args.max_per_query, env=env, days=args.days):
                vid = row["video_id"]
                if vid in seen or vid in skip_ids:
                    continue
                if not _title_looks_mlbb(str(row.get("title", ""))):
                    continue
                seen.add(vid)
                pool.append(row)
            if args.search_delay > 0:
                time.sleep(args.search_delay)

    if search_first:
        if len(pool) >= min_pool * 3:
            print(f"skip_channels pool={len(pool)} (search enough)", flush=True)
        else:
            _collect_channels()
    elif queries:
        _collect_search()

    pool.sort(
        key=lambda r: (
            float(r.get("duration") or 9999),
            -int(r.get("view_count") or 0),
        )
    )
    cap_sources = max(len(queries), len(channel_feeds), 1)
    cap = args.max_per_query * cap_sources
    pool = pool[: cap * 3]  # extra headroom — many rows already labeled

    known = labeled_ids()
    from mlbb_calibration_store import load_feed_sent

    already_sent = load_feed_sent()["ids"]
    sent_pending = {str(r.get("video_id", "")) for r in pending_candidates(limit=9999)}
    fresh_pool: list[dict] = []
    for row in pool:
        vid = row["video_id"]
        if vid in skip_ids or vid in known:
            continue
        if vid in sent_pending or vid in already_sent:
            continue
        fresh_pool.append(row)
    pool = fresh_pool[:cap]

    if not pool and (args.incremental or full_sweep):
        deep: list[dict] = []
        for query in list(SEARCH_QUERIES):
            for row in search_shorts(
                query,
                limit=max(args.max_per_query * 4, 40),
                env=env,
                days=args.days,
            ):
                vid = row["video_id"]
                if vid in skip_ids or vid in known or vid in sent_pending or vid in already_sent:
                    continue
                deep.append(row)
            if args.search_delay > 0:
                time.sleep(args.search_delay)
        pool = deep[: cap * 2]

    saved = rejected = downloads = skipped_known = 0
    min_score = float(os.environ.get("MLBB_CALIBRATION_MIN_SCORE", "0.05" if burst else "0.12"))
    run_started = time.time()
    max_run_sec = float(os.environ.get("MLBB_INGEST_MAX_RUN_SEC", "2400"))

    def _reject(vid: str, reason: str, path: Path | None = None) -> None:
        nonlocal rejected
        rejected += 1
        mark_ingest_skip(vid, reason)
        if path and path.exists() and os.environ.get("MLBB_INGEST_DELETE_REJECTED", "1") == "1":
            try:
                path.unlink()
            except OSError:
                pass

    for row in pool:
        if time.time() - run_started > max_run_sec:
            print("ingest_max_run_exceeded", flush=True)
            break
        if args.max_downloads > 0 and downloads >= args.max_downloads:
            break
        vid = row["video_id"]
        if vid in known:
            skipped_known += 1
            continue
        mp4 = SHORTS_ROOT / f"yt_{vid}.mp4"
        if not mp4.exists() and not args.skip_download:
            got = download_short(row["url"], SHORTS_ROOT, env, vid)
            if got and got.exists():
                mp4 = got
                downloads += 1
            else:
                _reject(vid, "download_failed")
                continue
            time.sleep(max(2.0, args.download_delay))
        if not mp4.exists() or mp4.name != f"yt_{vid}.mp4":
            _reject(vid, "missing_file")
            continue

        file_dur = _ffprobe_duration(mp4)
        if file_dur > 0 and not duration_in_ingest_range(file_dur, env):
            print(f"REJECT {vid} duration={file_dur:.0f}s out_of_range", flush=True)
            _reject(vid, "duration_out_of_range", mp4)
            continue

        if NEGATIVE_TITLE.search(row.get("title", "")):
            _reject(vid, "negative_title", mp4)
            continue

        lenient = os.environ.get("MLBB_CALIBRATION_LENIENT", "1") == "1"
        clip_start = 0.15
        clip_reason = "opening"
        fast_long = os.environ.get("MLBB_INGEST_SKIP_LONG_CLIP_REJECT", "0") == "1"
        if lenient:
            act_ok, act_reason = passes_mlbb_shorts_activity_gate(mp4, title=row.get("title", ""))
            if not act_ok:
                print(f"REJECT {vid} activity={act_reason}", flush=True)
                _reject(vid, f"activity:{act_reason}", mp4)
                continue
            if file_dur > shorts_short_max_sec():
                if fast_long:
                    clip_start = min(20.0, max(0.15, file_dur * 0.06))
                    clip_reason = "fast_long"
                    print(f"fast_long {vid} start={clip_start:.1f}s dur={file_dur:.0f}", flush=True)
                else:
                    clip_start, clip_reason = find_best_long_clip_start(mp4)
                    if clip_start < 0:
                        print(f"REJECT {vid} long_clip={clip_reason}", flush=True)
                        _reject(vid, f"long_clip:{clip_reason}", mp4)
                        continue
            if os.environ.get("MLBB_SHORTS_REQUIRE_KILL_UI", "1") == "1":
                kill_ok, kill_reason = passes_mlbb_shorts_kill_ui_gate(mp4, start_sec=clip_start)
                if not kill_ok:
                    print(f"REJECT {vid} kill_ui={kill_reason}", flush=True)
                    _reject(vid, f"kill_ui:{kill_reason}", mp4)
                    continue
        else:
            id_ok, id_reason = passes_mlbb_shorts_identity_gate(mp4, title=row.get("title", ""))
            if not id_ok:
                print(f"REJECT {vid} identity={id_reason}", flush=True)
                _reject(vid, f"identity:{id_reason}", mp4)
                continue

            act_ok, act_reason = passes_mlbb_shorts_activity_gate(mp4, title=row.get("title", ""))
            if not act_ok:
                print(f"REJECT {vid} activity={act_reason}", flush=True)
                _reject(vid, f"activity:{act_reason}", mp4)
                continue

            gp_ok, gp_reason = passes_mlbb_shorts_gameplay_gate(mp4, title=row.get("title", ""))
            if not gp_ok:
                print(f"REJECT {vid} gameplay={gp_reason}", flush=True)
                _reject(vid, f"gameplay:{gp_reason}", mp4)
                continue

            ver_ok, ver_reason = passes_mlbb_shorts_verify_gate(mp4, title=row.get("title", ""))
            if not ver_ok:
                print(f"REJECT {vid} verify={ver_reason}", flush=True)
                _reject(vid, f"verify:{ver_reason}", mp4)
                continue

            if file_dur > shorts_short_max_sec():
                clip_start, clip_reason = find_best_long_clip_start(mp4)
                if clip_start < 0:
                    print(f"REJECT {vid} long_clip={clip_reason}", flush=True)
                    _reject(vid, f"long_clip:{clip_reason}", mp4)
                    continue
            kill_ok, kill_reason = passes_mlbb_shorts_kill_ui_gate(mp4, start_sec=clip_start)
            if not kill_ok:
                print(f"REJECT {vid} kill_ui={kill_reason}", flush=True)
                _reject(vid, f"kill_ui:{kill_reason}", mp4)
                continue

            gate_ok, gate_reason = passes_shorts_calibration_gate(mp4, title=row.get("title", ""))
            if not gate_ok:
                print(f"REJECT {vid} gate={gate_reason}", flush=True)
                _reject(vid, f"gate:{gate_reason}", mp4)
                continue

        feats = score_clip(mp4)
        if int(feats.get("rule_pass") or 0) != 1 and not lenient:
            print(f"REJECT {vid} rule_pass=0 {feats.get('pass_reason','')}", flush=True)
            _reject(vid, "rule_pass", mp4)
            continue
        if feats["score"] < min_score and not lenient:
            _reject(vid, "low_score", mp4)
            continue

        upsert_candidate(
            {
                **row,
                **feats,
                "path": str(mp4),
                "clip_start_sec": clip_start,
                "pass_reason": clip_reason if lenient else feats.get("pass_reason", ""),
                "gameplay_pass": 1,
                "identity_pass": 1,
                "ingest_verified": 1,
                "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        saved += 1
        print(f"OK {vid} score={feats['score']:.3f} views={row.get('view_count')} {row.get('title','')[:50]}")
        try:
            from mlbb_calibration_tier import note_ingest_saved

            note_ingest_saved(count=1)
        except ImportError:
            pass

    tier = os.environ.get("MLBB_CALIBRATION_TIER", "?")
    print(
        f"SUMMARY saved={saved} rejected={rejected} downloads={downloads} skipped_known={skipped_known} "
        f"pool={len(pool)} pending={pending_n} tier={tier} dir={SHORTS_ROOT}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
