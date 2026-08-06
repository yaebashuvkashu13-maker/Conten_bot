#!/usr/bin/env python3
"""
PUBG / Standoff / Genshin / WoT VOD segment feed — same calibration loop as MLBB.

Run with VOD_SEGMENT_GAME=pubg|standoff|genshin|wot (or argv[1]).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from daily_game_cycle import can_send_for_game, profile_for_game, record_send as cycle_record_send
from mlbb_vod_segment_feed import (
    VodPipelineDownloader,
    _ffprobe_duration,
    _vod_target_dur_sec,
    render_single_segment,
    send_message,
    send_video,
)
from pubg_combat_gate import pubg_passes_combat_gate
from pubg_metro_royale_gate import title_metro_hint
from shooter_vod_segment_store import (
    keyboard,
    labeled_ids,
    load_feed_sent,
    load_index,
    mark_feed_sent,
    segment_id,
    stats,
    upsert_segment,
    vod_youtube_id,
    _paths,
)
from shooter_owner_montage import (
    merge_owner_hints_into_pool,
    owner_good_pool,
    soft_allow_owner_montage_part,
    vod_has_owner_montage_anchors,
)
from strict_montage_direct import discover_strict_candidates, file_sha256
from vod_peak_gap import peak_too_close, segment_gap_sec, used_peak_times_shooter
from vod_scan_state import (
    max_peak_tries,
    minimal_pool_from_entry,
    peak_values_from_entry,
    peaks_from_pool,
    pool_cache_valid,
    pool_peaks_fully_blocked,
    record_vod_scan,
    scan_zero_detail,
    should_mark_vod_exhausted,
    should_skip_vod_rescan,
    used_peaks_for_vod,
)
from vod_game_registry import VOD_PIPELINE_REV
from youtube_download import load_env

log = logging.getLogger("shooter_vod_feed")


def _vod_min_sec() -> float:
    """Shooter/montage games need longer VODs than MLBB singles (~3 fights)."""
    raw = os.environ.get("SHOOTER_VOD_MIN_SEC") or os.environ.get("MLBB_VOD_MIN_SEC")
    if raw:
        try:
            base = float(raw)
        except ValueError:
            base = 300.0
    else:
        base = 300.0
    if os.environ.get("SHOOTER_VOD_MONTAGE", "1") == "1":
        montage_floor = float(os.environ.get("SHOOTER_VOD_MONTAGE_MIN_VOD_SEC", "600"))
        return max(base, montage_floor)
    return base


def _vod_max_sec() -> float:
    # Never inherit MLBB_VOD_MAX_SEC=1200 — that purged 20–30 min montage streams.
    raw = os.environ.get("SHOOTER_VOD_MAX_SEC")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return 3600.0


def _shooter_vod_length_ok(path: Path, dur: float | None = None) -> bool:
    length = dur if dur is not None else _ffprobe_duration(path)
    return _vod_min_sec() <= length <= _vod_max_sec()


ENV_PATH = Path("/root/.video_bot.env")
EXTENDED_GAMES = frozenset({"genshin", "wot"})
FEED_GAMES = frozenset({"pubg", "standoff", *EXTENDED_GAMES})


def _game() -> str:
    raw = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("VOD_SEGMENT_GAME", "pubg")).strip().lower()
    return raw if raw in FEED_GAMES else "pubg"


def _adaptive_gate(game: str):
    if game in EXTENDED_GAMES:
        import extended_vod_adaptive_gate as gate

        return gate
    import shooter_vod_adaptive_gate as gate

    return gate


def _profile(game: str) -> str:
    return profile_for_game(game)


def _load_state(game: str) -> dict:
    from vod_state_io import load_json_state

    p = _paths(game)["state"]
    return load_json_state(
        p,
        lambda: {"vods": [], "used_youtube_ids": [], "discovery_cycle": 0},
    )


def _save_state(game: str, state: dict) -> None:
    from vod_state_io import save_json_state

    save_json_state(_paths(game)["state"], state)


def _bootstrap_owner_exemplars(game: str) -> dict:
    """Load owner 👍/👎 exemplars into CLIP cache for VOD peak scoring."""
    from daily_game_cycle import profile_for_game
    from vod_owner_learning import (
        backfill_owner_labels_from_vod_segments,
        clear_learning_cache,
        exemplar_counts,
    )

    profile = profile_for_game(game)
    good_n, bad_n = exemplar_counts(profile)
    out: dict = {
        "game": game,
        "profile": profile,
        "good_exemplars": good_n,
        "bad_exemplars": bad_n,
    }
    if os.environ.get("SHOOTER_VOD_OWNER_BACKFILL", "1") == "1":
        try:
            out["backfill_added"] = backfill_owner_labels_from_vod_segments(profile)
        except Exception as exc:
            log.warning("owner backfill failed game=%s: %s", game, exc)
    try:
        clear_learning_cache()
    except Exception as exc:
        log.warning("exemplar cache clear failed game=%s: %s", game, exc)
    log.info(
        "shooter owner exemplars game=%s good=%s bad=%s backfill=%s",
        game,
        good_n,
        bad_n,
        out.get("backfill_added", 0),
    )
    return out


def _feed_lock(game: str):
    lock_path = Path(f"/tmp/{game}_vod_segment_feed.lock")
    handle = lock_path.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        log.warning("another %s feed running — exit", game)
        return None
    return handle


def _vod_registry_entry(state: dict, vod: Path) -> dict | None:
    vod_path = str(vod)
    vid = vod_youtube_id(vod)
    for entry in state.get("vods", []):
        if entry.get("path") == vod_path or entry.get("id") == vid:
            return entry
    return None


def _vod_registry_entries(state: dict, vod: Path) -> list[dict]:
    vod_path = str(vod)
    vid = vod_youtube_id(vod)
    return [
        entry
        for entry in state.get("vods", [])
        if entry.get("path") == vod_path or entry.get("id") == vid
    ]


def _mark_vod_exhausted(
    state: dict,
    vod: Path,
    *,
    reason: str,
    delete_file: bool = False,
) -> dict:
    """Mark every duplicate registry row exhausted; optionally delete the mp4."""
    rows = _vod_registry_entries(state, vod)
    if not rows:
        row = {
            "id": vod_youtube_id(vod),
            "path": str(vod),
            "title": "",
            "exhausted": True,
            "reject_reason": reason,
        }
        state.setdefault("vods", []).append(row)
        rows = [row]
    for entry in rows:
        entry["exhausted"] = True
        entry["reject_reason"] = reason
        entry["path"] = str(vod)
        record_vod_scan(entry, sent=0, pool_peaks=[], blocked=True)
    if delete_file:
        try:
            if vod.exists():
                vod.unlink()
                log.info("deleted exhausted vod=%s reason=%s", vod.name, reason)
        except OSError as exc:
            log.warning("delete exhausted vod failed %s: %s", vod.name, exc)
    return rows[0]


def _upsert_vod_registry(
    state: dict,
    *,
    vid: str,
    path: str,
    title: str = "",
    exhausted: bool = False,
    reject_reason: str = "",
) -> dict:
    """One row per youtube id/path — never append duplicates."""
    registry = state.setdefault("vods", [])
    matches = [
        row
        for row in registry
        if row.get("id") == vid or row.get("path") == path
    ]
    if matches:
        primary = matches[0]
        primary["id"] = vid
        primary["path"] = path
        if title:
            primary["title"] = title
        if exhausted:
            primary["exhausted"] = True
        if reject_reason:
            primary["reject_reason"] = reject_reason
        # Collapse duplicates into the primary row.
        for extra in matches[1:]:
            if extra.get("exhausted"):
                primary["exhausted"] = True
            if extra.get("reject_reason") and not primary.get("reject_reason"):
                primary["reject_reason"] = extra.get("reject_reason")
            try:
                registry.remove(extra)
            except ValueError:
                pass
        return primary
    row = {
        "id": vid,
        "path": path,
        "title": title,
        "exhausted": exhausted,
    }
    if reject_reason:
        row["reject_reason"] = reject_reason
    registry.append(row)
    return row


def _vod_title(state: dict, vod: Path) -> str:
    entry = _vod_registry_entry(state, vod)
    return str((entry or {}).get("title") or "")


def _pubg_metro_should_exhaust(title: str, streak: int) -> bool:
    """Only permanently skip VOD when title and soften cannot override metro reject."""
    from pubg_metro_royale_gate import title_metro_hint
    from shooter_vod_adaptive_gate import soften_level

    if title_metro_hint(title):
        return False
    if soften_level(streak) >= 2:
        return False
    return True


def _pubg_metro_vod_ok(
    vod: Path,
    *,
    title: str = "",
    streak: int = 0,
) -> tuple[bool, str]:
    from pubg_metro_royale_gate import vod_looks_metro_royale
    from shooter_vod_adaptive_gate import soften_level

    ok, reason = vod_looks_metro_royale(vod, title=title or None)
    if ok:
        return True, reason
    if soften_level(streak) >= 2:
        log.warning("metro soften override vod=%s streak=%s reason=%s", vod.name, streak, reason)
        return True, f"metro_soften_L{soften_level(streak)} ({reason})"
    return False, reason


def _discover_candidates(game: str, env: dict[str, str], used: set[str]) -> list[dict]:
    from youtube_download import run_ytdlp, ytdlp_cmd, ytdlp_extra_args

    if game in EXTENDED_GAMES:
        from youtube_extended_vod_prefs import title_ok as ext_title_ok, vod_discovery_search_cycle as ext_cycle

        title_ok_fn = ext_title_ok
        cycle_fn = ext_cycle
    else:
        from youtube_shooter_vod_prefs import title_ok as shooter_title_ok, vod_discovery_search_cycle as shooter_cycle

        title_ok_fn = shooter_title_ok
        cycle_fn = shooter_cycle

    state = _load_state(game)
    # Honor discovery pause (403 / empty) — don't hammer yt-dlp every idle tick.
    pause_until = float(state.get("discovery_pause_until") or 0)
    if pause_until > time.time():
        log.info(
            "discovery paused game=%s remain=%.0fs",
            game,
            pause_until - time.time(),
        )
        return []
    cycle = int(state.get("discovery_cycle", 0))
    params = cycle_fn(cycle, game, env)
    state["discovery_cycle"] = cycle + 1
    _save_state(game, state)

    out: list[dict] = []
    saw_403 = False
    for url in params.get("urls", []):
        cmd = ytdlp_cmd(env) + [
            "--flat-playlist",
            "--print",
            "%(id)s|%(title)s|%(duration)s|%(uploader)s|%(live_status)s",
            url,
        ]
        cmd += ytdlp_extra_args(env)
        proc = run_ytdlp(cmd, env, timeout=90, label=f"search-{game}")
        if proc.returncode != 0:
            err = (proc.stderr or "")[:400]
            log.warning("search failed %s: %s", url, err)
            if "403" in err or "Forbidden" in err:
                saw_403 = True
            continue
        for line in (proc.stdout or "").splitlines():
            parts = line.split("|", 4)
            if len(parts) < 2:
                continue
            vid, title = parts[0][:11], parts[1]
            if vid in used or len(vid) != 11:
                continue
            live_status = (parts[4] if len(parts) > 4 else "").strip().lower()
            if live_status in ("is_live", "is_upcoming"):
                log.info("skip live/upcoming id=%s status=%s", vid, live_status)
                used.add(vid)
                continue
            if not title_ok_fn(game, title):
                continue
            try:
                dur = float(parts[2]) if len(parts) > 2 and parts[2] not in ("NA", "None", "") else 0.0
            except ValueError:
                dur = 0.0
            # Live streams often report duration 0 / NA — never download those.
            if dur <= 0:
                log.info("skip zero-duration (likely live) id=%s title=%s", vid, title[:40])
                used.add(vid)
                continue
            if not _shooter_vod_length_ok(Path("x.mp4"), dur):
                continue
            out.append(
                {
                    "id": vid,
                    "title": title[:120],
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "duration": dur,
                    "uploader": parts[3][:60] if len(parts) > 3 else "",
                }
            )
        time.sleep(float(params.get("delay", 6)))
    if not out:
        pause_sec = float(
            os.environ.get(
                "SHOOTER_VOD_DISCOVERY_PAUSE_SEC",
                "900" if saw_403 else "300",
            )
        )
        state = _load_state(game)
        state["discovery_pause_until"] = time.time() + pause_sec
        state["discovery_last_empty_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        state["discovery_last_empty_403"] = bool(saw_403)
        _save_state(game, state)
        log.warning(
            "discovery empty game=%s 403=%s pause=%.0fs",
            game,
            saw_403,
            pause_sec,
        )
    return out


def _download_vod(game: str, pick: dict, env: dict[str, str]) -> Path | None:
    from youtube_download import download_one

    inbox = _paths(game)["inbox"]
    inbox.mkdir(parents=True, exist_ok=True)
    # Hard reject known live / upcoming before yt-dlp hangs on HLS.
    title = str(pick.get("title") or "").lower()
    if "live event will begin" in title:
        log.warning("skip live-title pick id=%s", pick.get("id"))
        return None
    try:
        path = download_one(str(pick["url"]), inbox, env)
        return path
    except Exception as exc:
        msg = str(exc)
        log.warning("download failed %s: %s", pick.get("id"), exc)
        if "live event" in msg.lower() or "is live" in msg.lower():
            state = _load_state(game)
            used = set(state.get("used_youtube_ids", []))
            used.add(str(pick.get("id") or ""))
            state["used_youtube_ids"] = sorted(u for u in used if u)
            _save_state(game, state)
        return None

def _row_window_start(row: dict) -> float:
    """Use the rendered clip start — never peak_start alone (that misaligned gates)."""
    clip = row.get("clip") if isinstance(row.get("clip"), dict) else {}
    for key in ("start",):
        if clip.get(key) is not None:
            try:
                return float(clip[key])
            except (TypeError, ValueError):
                pass
    if row.get("start") is not None:
        try:
            return float(row["start"])
        except (TypeError, ValueError):
            pass
    return float(row.get("peak_start", 0) or 0)


def _validate_shooter_presend(
    game: str,
    vod: Path,
    row: dict,
    rendered: Path,
    *,
    montage_part: bool = False,
) -> tuple[bool, str, dict]:
    profile = _profile(game)
    # Gate the same window that was rendered (peak-centered clip start), not peak_start.
    start = _row_window_start(row)
    dur = _ffprobe_duration(rendered)
    if dur <= 0:
        clip = row.get("clip") if isinstance(row.get("clip"), dict) else {}
        dur = float(clip.get("input_duration") or clip.get("output_duration") or row.get("duration") or 15)
    if game == "pubg":
        from pubg_metro_royale_gate import segment_looks_metro_royale

        ok_metro, metro_reason = segment_looks_metro_royale(vod, start, dur)
        if not ok_metro:
            return False, metro_reason, {"metro": metro_reason}
    if game in EXTENDED_GAMES:
        from strict_segment_gate import passes_strict_gate

        ok, reason, metrics = passes_strict_gate(vod, start, dur, profile)
        if game == "genshin" and ok:
            from genshin_boss_segment import validate_genshin_boss_segment

            boss_ok, boss_reason, boss_metrics = validate_genshin_boss_segment(vod, start, dur)
            metrics.update(boss_metrics)
            if not boss_ok:
                return False, boss_reason, metrics
        return ok, reason, metrics
    # Montage parts: shooting/PANNs gate is enough — full combat visual on every
    # 22s slice was rejecting valid gunfire windows (panns_trust then dropped).
    shooting_only = montage_part and os.environ.get("SHOOTER_VOD_MONTAGE_SHOOTING_ONLY", "1") == "1"
    if shooting_only and game in ("pubg", "standoff", "wot"):
        from highlight_scorer import score_panns_audio
        from pubg_shooting_gate import pubg_passes_shooting_gate

        # Gate the fight CORE around peak, not the whole 22s part (lead/tail walk
        # was falsely flagged loot_walk while the middle is clear gunfire).
        peak = float(row.get("peak_start") or start)
        core = float(os.environ.get("SHOOTER_VOD_MONTAGE_GATE_CORE_SEC", "10"))
        gate_start = max(0.0, peak - core * 0.5)
        gate_dur = min(float(dur), core)
        if gate_start + gate_dur > start + float(dur):
            gate_start = max(0.0, start + float(dur) - gate_dur)
        panns = score_panns_audio(vod, gate_start, gate_dur)
        panns_gun = float(panns.get("panns_gun_max", 0) or 0)
        ok, reason, metrics = pubg_passes_shooting_gate(
            vod, gate_start, gate_dur, panns_gun_max=panns_gun
        )
        metrics = dict(metrics or {})
        metrics["panns_gun_max"] = panns_gun
        metrics["gate_core_start"] = round(gate_start, 2)
        metrics["gate_core_dur"] = round(gate_dur, 2)
        ok, reason = soft_allow_owner_montage_part(
            game, vod, gate_start, ok, reason, montage_part=True, metrics=metrics
        )
        if not ok:
            return False, reason, metrics
        # Author must be the one fragging — reject death-cam / "author keeps dying" trash.
        from shooter_author_kill_gate import author_kill_window_ok

        kill_ok, kill_reason, kill_metrics = author_kill_window_ok(
            vod,
            gate_start,
            gate_dur,
            profile=game,
            shoot_metrics=metrics,
        )
        metrics.update(kill_metrics)
        if not kill_ok:
            return False, kill_reason, metrics
        return True, f"{reason}+{kill_reason}", metrics
    ok, reason, metrics = pubg_passes_combat_gate(vod, start, dur, profile)
    ok, reason = soft_allow_owner_montage_part(
        game,
        vod,
        start,
        ok,
        reason,
        montage_part=montage_part,
        metrics=metrics if isinstance(metrics, dict) else None,
    )
    if not ok:
        return False, reason, metrics
    if game in ("pubg", "standoff", "wot"):
        from shooter_author_kill_gate import author_kill_window_ok

        kill_ok, kill_reason, kill_metrics = author_kill_window_ok(
            vod,
            start,
            float(dur),
            profile=game,
            shoot_metrics=metrics if isinstance(metrics, dict) else None,
        )
        if isinstance(metrics, dict):
            metrics.update(kill_metrics)
        else:
            metrics = dict(kill_metrics)
        if not kill_ok:
            return False, kill_reason, metrics
        return True, f"{reason or 'shooter_combat_ok'}+{kill_reason}", metrics
    return True, reason or "shooter_combat_ok", metrics


def _used_peak_times(game: str, vod_id: str, sent_set: set[str]) -> list[float]:
    return used_peak_times_shooter(vod_id, sent_set, load_index(game).get("segments", []))


def _peak_too_close(peak: float, used_peaks: list[float], gap_sec: float) -> bool:
    return peak_too_close(peak, used_peaks, gap_sec)


def _montage_enabled(game: str) -> bool:
    """PUBG / Standoff / WoT ship as multi-clip montages; Genshin stays single by default."""
    if game == "genshin":
        return os.environ.get("GENSHIN_VOD_MONTAGE", "0") == "1"
    if os.environ.get("SHOOTER_VOD_MONTAGE", "1") != "1":
        return False
    game_key = {
        "pubg": "PUBG_VOD_MONTAGE",
        "standoff": "STANDOFF_VOD_MONTAGE",
        "wot": "WOT_VOD_MONTAGE",
    }.get(game)
    if game_key:
        return os.environ.get(game_key, "1") == "1"
    return True


def _montage_only(game: str) -> bool:
    if not _montage_enabled(game):
        return False
    if os.environ.get("SHOOTER_VOD_MONTAGE_ONLY", "1") == "1":
        return True
    game_key = {
        "pubg": "PUBG_VOD_MONTAGE_ONLY",
        "standoff": "STANDOFF_VOD_MONTAGE_ONLY",
        "wot": "WOT_VOD_MONTAGE_ONLY",
    }.get(game)
    return os.environ.get(game_key or "", "1") == "1"


def _montage_limits() -> tuple[int, int, float, float, float]:
    """min_clips, max_clips, gap_sec, part_max_sec, final_max_sec."""
    min_clips = max(2, int(os.environ.get("SHOOTER_VOD_MONTAGE_MIN_CLIPS", "3")))
    max_clips = max(min_clips, int(os.environ.get("SHOOTER_VOD_MONTAGE_MAX_CLIPS", "3")))
    gap = float(os.environ.get("SHOOTER_VOD_MONTAGE_GAP_SEC", "55"))
    part_max = float(os.environ.get("SHOOTER_VOD_MONTAGE_PART_MAX_SEC", "28"))
    final_max = float(os.environ.get("SHOOTER_VOD_MONTAGE_MAX_SEC", "70"))
    return min_clips, max_clips, gap, part_max, final_max


def _pick_montage_rows(rows: list[dict], *, min_clips: int, max_clips: int, gap_sec: float) -> list[dict]:
    """Greedy highest-score peaks spaced by montage gap.

    Returns up to max_clips * 3 candidates so rejected parts can be replaced
    without failing the whole склейка. May return fewer than min_clips — caller decides.
    """
    pool_cap = max(max_clips * 3, min_clips + 3)
    picked: list[dict] = []
    for row in sorted(rows, key=lambda r: float(r.get("score", 0)), reverse=True):
        peak = float(row.get("peak_start", row.get("start", 0)))
        if any(abs(peak - float(p.get("peak_start", p.get("start", 0)))) < gap_sec for p in picked):
            continue
        picked.append(row)
        if len(picked) >= pool_cap:
            break
    return picked


def _prepare_montage_clip(row: dict, vod: Path, *, part_max: float) -> dict:
    """Peak-center montage parts on the fight core — not a 22s walk+loot window.

    Default ship length matches what we gate (core + small pad). Longer tails
    were the main trash-send path: gate passed on 10s gun core, shipped 22s with
    loot/run edges.
    """
    clip = dict(row.get("clip") or {})
    start_hint = float(row.get("start", clip.get("start", 0)) or 0)
    peak = float(row.get("peak_start", clip.get("peak_start", start_hint)) or start_hint)
    core = float(os.environ.get("SHOOTER_VOD_MONTAGE_GATE_CORE_SEC", "10"))
    pad = float(os.environ.get("SHOOTER_VOD_MONTAGE_CORE_PAD_SEC", "2"))
    # Prefer fight-core length; never exceed part_max / env part sec.
    want = min(
        part_max,
        float(os.environ.get("SHOOTER_VOD_MONTAGE_PART_SEC", str(core + pad * 2))),
        max(12.0, core + pad * 2),
    )
    half = want * 0.5
    start = max(0.0, peak - half)
    dur = want
    file_dur = _ffprobe_duration(vod)
    if file_dur > 1.0 and start + dur > file_dur:
        start = max(0.0, file_dur - dur)
        dur = max(8.0, file_dur - start)
    clip.update(
        {
            "start": start,
            "peak_start": peak,
            "input_duration": round(dur, 2),
            "output_duration": round(dur, 2),
        }
    )
    return clip


def _send_montage(
    game: str,
    token: str,
    chat_id: str,
    vod: Path,
    rows: list[dict],
    sig: str,
) -> int:
    """Render N parts, xfade-merge, send one Telegram video."""
    import shutil
    import tempfile

    from smart_video_editor import build_xfade_command, run_command

    ok_cycle, cycle_reason = can_send_for_game(game, 1)
    if not ok_cycle:
        log.info("cycle block game=%s reason=%s", game, cycle_reason)
        return 0

    min_clips, max_clips, gap_sec, part_max, final_max = _montage_limits()
    # If few peaks, retry with tighter spacing before giving up (still need distinct fights).
    picked = _pick_montage_rows(rows, min_clips=min_clips, max_clips=max_clips, gap_sec=gap_sec)
    if len(picked) < min_clips:
        tight = max(18.0, gap_sec * 0.45)
        picked = _pick_montage_rows(rows, min_clips=min_clips, max_clips=max_clips, gap_sec=tight)
        if len(picked) >= min_clips:
            log.info("montage tight-gap ok game=%s gap=%.0f→%.0f peaks=%s", game, gap_sec, tight, len(picked))
            gap_sec = tight
    if len(picked) < min_clips:
        log.warning(
            "montage insufficient peaks game=%s have=%s need=%s rows=%s",
            game,
            len(picked),
            min_clips,
            len(rows),
        )
        return 0

    # Fast discovery (PANNs) + quality: CLIP ranks only this shortlist under a budget.
    try:
        from highlight_scorer import rank_shortlist_with_clip

        picked = rank_shortlist_with_clip(vod, picked, _profile(game))
    except Exception as exc:
        log.warning("montage CLIP rank skipped: %s", exc)

    seg_root = _paths(game)["segments"]
    seg_root.mkdir(parents=True, exist_ok=True)
    max_attempts = max(1, int(os.environ.get("SHOOTER_VOD_MONTAGE_SHORTLIST_TRIES", "3")))
    rejected_sids: set[str] = set()
    remaining = list(picked)

    for attempt in range(max_attempts):
        if len(remaining) < min_clips:
            break
        temp_dir = Path(tempfile.mkdtemp(prefix=f"{game}-montage-"))
        segment_paths: list[Path] = []
        durations: list[float] = []
        accepted_rows: list[dict] = []
        try:
            for idx, row in enumerate(remaining):
                sid = str(row.get("segment_id") or "")
                if sid in rejected_sids:
                    continue
                clip = _prepare_montage_clip(row, vod, part_max=part_max)
                part = temp_dir / f"part_{idx:02d}.mp4"
                work_row = {**row, "clip": clip, "start": clip["start"], "peak_start": clip["peak_start"]}
                if not render_single_segment(vod, clip, part):
                    log.warning("montage part render fail idx=%s sid=%s", idx, sid)
                    rejected_sids.add(sid)
                    continue
                ok, reason, _report = _validate_shooter_presend(
                    game, vod, work_row, part, montage_part=True
                )
                if not ok:
                    log.warning("montage part REJECT %s: %s", sid, reason)
                    part.unlink(missing_ok=True)
                    rejected_sids.add(sid)
                    continue
                dur = _ffprobe_duration(part)
                if dur < 6.0:
                    part.unlink(missing_ok=True)
                    rejected_sids.add(sid)
                    continue
                segment_paths.append(part)
                durations.append(dur)
                accepted_rows.append(work_row)
                if len(segment_paths) >= max_clips:
                    break

            if len(segment_paths) < min_clips:
                log.warning(
                    "montage after-presend insufficient game=%s parts=%s need=%s attempt=%s",
                    game,
                    len(segment_paths),
                    min_clips,
                    attempt + 1,
                )
                # Drop failed peaks; next attempt uses remaining unused dense rows.
                remaining = [r for r in remaining if str(r.get("segment_id") or "") not in rejected_sids]
                if not remaining:
                    remaining = [
                        r
                        for r in rows
                        if str(r.get("segment_id") or "") not in rejected_sids
                    ]
                    remaining = _pick_montage_rows(
                        remaining, min_clips=min_clips, max_clips=max_clips, gap_sec=gap_sec
                    )
                continue

            ordered = sorted(
                zip(accepted_rows, segment_paths, durations),
                key=lambda t: float(t[0].get("peak_start", t[0].get("start", 0))),
            )
            accepted_rows = [t[0] for t in ordered]
            segment_paths = [t[1] for t in ordered]
            durations = [t[2] for t in ordered]

            while len(segment_paths) > min_clips:
                est = sum(durations) - 0.28 * (len(segment_paths) - 1)
                if est <= final_max:
                    break
                segment_paths.pop()
                durations.pop()
                accepted_rows.pop()

            montage_id = f"{vod_youtube_id(vod)}_mtg_{int(time.time())}"
            out = seg_root / f"montage_{montage_id}.mp4"
            run_command(build_xfade_command(segment_paths, durations, out))
            final_dur = _ffprobe_duration(out)
            if final_dur < 18.0:
                log.warning("montage too short game=%s dur=%.1f", game, final_dur)
                out.unlink(missing_ok=True)
                return 0

            peaks = ",".join(str(int(r.get("peak_start", r["start"]))) for r in accepted_rows)
            caption = (
                f"{game.upper()} склейка ×{len(accepted_rows)} · {final_dur:.0f}s\n"
                f"{vod_youtube_id(vod)} peaks {peaks}\n"
                f"👍 Ок / 👎 Не ок"
            )
            primary_sid = accepted_rows[0]["segment_id"]
            if not send_video(
                token,
                chat_id,
                out,
                caption,
                seg_id=primary_sid,
                record_learning=False,
                reply_markup=keyboard(game, primary_sid),
                cycle_game=game,
            ):
                return 0

            for row in accepted_rows:
                sid = row["segment_id"]
                upsert_segment(
                    game,
                    {
                        "segment_id": sid,
                        "path": str(out),
                        "vod": str(vod),
                        "vod_id": vod_youtube_id(vod),
                        "start": row["start"],
                        "duration": final_dur,
                        "peak_start": row.get("peak_start", row["start"]),
                        "score": row.get("score", 0),
                        "sig": sig,
                        "montage_id": montage_id,
                        "montage_parts": [str(r["segment_id"]) for r in accepted_rows],
                        "montage_part_count": len(accepted_rows),
                        "montage_peaks": [
                            float(r.get("peak_start", r["start"])) for r in accepted_rows
                        ],
                        "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    },
                )
            mark_feed_sent(game, [r["segment_id"] for r in accepted_rows])
            st = stats(game)
            send_message(
                token,
                chat_id,
                f"✅ {game.upper()} склейка ×{len(accepted_rows)} | 👍{st['feedback_yes']} 👎{st['feedback_no']}",
            )
            log.info(
                "montage sent game=%s parts=%s dur=%.1fs file=%s",
                game,
                len(accepted_rows),
                final_dur,
                out.name,
            )
            return 1
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    log.warning(
        "montage all shortlist tries failed game=%s rejected=%s",
        game,
        len(rejected_sids),
    )
    return 0


def _send_batch(game: str, token: str, chat_id: str, vod: Path, to_send: list[dict], sig: str) -> int:
    if _montage_enabled(game):
        n = _send_montage(game, token, chat_id, vod, to_send, sig)
        if n > 0:
            return n
        if _montage_only(game):
            log.warning("montage-only: refuse single-clip fallback game=%s", game)
            return 0
    ok_cycle, cycle_reason = can_send_for_game(game, 1)
    if not ok_cycle:
        log.info("cycle block game=%s reason=%s", game, cycle_reason)
        return 0

    seg_root = _paths(game)["segments"]
    seg_root.mkdir(parents=True, exist_ok=True)
    sent = 0
    # Final CLIP quality gate on the single chosen clip (discovery stays CLIP-off).
    to_send_ranked = list(to_send)
    if os.environ.get("SHOOTER_VOD_MONTAGE_CLIP_RANK", "1") == "1":
        try:
            from highlight_scorer import rank_shortlist_with_clip

            to_send_ranked = rank_shortlist_with_clip(
                vod, to_send[:6], _profile(game), max_n=min(6, len(to_send))
            )
        except Exception as exc:
            log.warning("single CLIP rank skipped: %s", exc)
            to_send_ranked = list(to_send)
    for row in to_send_ranked[:1]:
        sid = row["segment_id"]
        out = seg_root / f"seg_{sid}.mp4"
        if not render_single_segment(vod, row["clip"], out):
            continue
        # Soft reject: if CLIP scored this window and it is trash vs exemplars, skip.
        clip_s = float(row.get("clip_score") or (row.get("highlight_metrics") or {}).get("clip_score") or 0)
        try:
            clip_min = float(os.environ.get("SHOOTER_VOD_MONTAGE_CLIP_MIN", "0.08"))
        except ValueError:
            clip_min = 0.08
        if (
            os.environ.get("SHOOTER_VOD_SINGLE_CLIP_REJECT", "1") == "1"
            and row.get("highlight_metrics", {}).get("clip_rank")
            and clip_s < clip_min
        ):
            log.warning("presend CLIP REJECT %s: clip=%.3f < %.2f", sid, clip_s, clip_min)
            continue
        presend_ok, presend_reason, presend_report = _validate_shooter_presend(game, vod, row, out)
        if not presend_ok:
            log.warning("presend REJECT %s: %s", sid, presend_reason)
            continue
        peak = int(row.get("peak_start", row["start"]))
        caption = (
            f"{game.upper()} Metro Royale #{sid}\n"
            f"{vod_youtube_id(vod)} @ {int(row['start'])}s (пик {peak}s)\n"
            f"Metro ✓ | {presend_reason}\n"
            f"👍 Ок / 👎 Не ок"
        ) if game == "pubg" else (
            f"{game.upper()} кусок #{sid}\n"
            f"{vod_youtube_id(vod)} @ {int(row['start'])}s (пик {peak}s)\n"
            f"{'Boss' if game == 'genshin' else 'Combat' if game == 'wot' else 'POV combat'} ✓ | {presend_reason}\n"
            f"👍 Ок / 👎 Не ок"
        )
        if send_video(
            token,
            chat_id,
            out,
            caption,
            seg_id=sid,
            record_learning=False,
            reply_markup=keyboard(game, sid),
            cycle_game=game,
        ):
            upsert_segment(
                game,
                {
                    "segment_id": sid,
                    "path": str(out),
                    "vod": str(vod),
                    "vod_id": vod_youtube_id(vod),
                    "start": row["start"],
                    "duration": _ffprobe_duration(out),
                    "peak_start": peak,
                    "score": row.get("score", 0),
                    "sig": sig,
                    "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
            )
            mark_feed_sent(game, [sid])
            sent += 1
    st = stats(game)
    if sent:
        send_message(token, chat_id, f"✅ {game.upper()} sent={sent} | 👍{st['feedback_yes']} 👎{st['feedback_no']}")
    return sent


def _inbox_order_key(mp4: Path, registry: list[dict], *, game: str = "pubg") -> tuple:
    """Unscanned VODs first; owner-anchor VODs before others; Metro + Russian next."""
    from pubg_metro_royale_gate import title_metro_hint
    from youtube_game_prefs import russian_score

    entry = next((r for r in registry if r.get("path") == str(mp4)), None)
    scanned = float((entry or {}).get("last_scan_at") or 0)
    title = str((entry or {}).get("title") or "")
    owner_prio = 0 if vod_has_owner_montage_anchors(game, mp4) else 1
    metro_prio = 0 if title_metro_hint(title) else 1
    ru = russian_score({"title": title, "uploader": str((entry or {}).get("uploader") or "")})
    ru_prio = 0 if ru >= 0.10 else (1 if ru >= 0.05 else 2)
    fast_fail = 1 if str((entry or {}).get("reject_reason") or "").startswith("fast_panns_0") else 0
    # Prefer larger (longer) VODs — short junk burns stall budget without combat.
    try:
        size_prio = -int(mp4.stat().st_size)
    except OSError:
        size_prio = 0
    return (1 if scanned else 0, owner_prio, fast_fail, metro_prio, ru_prio, size_prio, scanned, mp4.stat().st_mtime)


def _scan_vod(
    game: str,
    token: str,
    chat_id: str,
    vod: Path,
    env: dict[str, str],
    *,
    soften_level: int = 0,
    entry: dict | None = None,
) -> int:
    profile = _profile(game)
    sig = file_sha256(vod)
    labeled = labeled_ids(game)
    sent_set = load_feed_sent(game)
    vid = vod_youtube_id(vod)
    lead = float(os.environ.get("MLBB_VOD_LEAD_SEC", "4"))
    seg_gap = segment_gap_sec(game, soften_level=soften_level)
    index_segments = load_index(game).get("segments", [])
    used_peaks = used_peaks_for_vod(game, vid, sent_set, index_segments)
    blocked_ids = labeled | sent_set
    montage = _montage_enabled(game)
    min_clips = max(2, int(os.environ.get("SHOOTER_VOD_MONTAGE_MIN_CLIPS", "3"))) if montage else 1
    owner_hints_all = owner_good_pool(game, vod, lead_sec=max(lead, 6.0)) if montage else []
    owner_hints = [
        c
        for c in owner_hints_all
        if not _peak_too_close(float(c.get("start", 0)), used_peaks, 12.0)
    ]
    vod_dur = _ffprobe_duration(vod)
    long_vod = vod_dur >= float(os.environ.get("SHOOTER_VOD_LONG_REDISCOVER_MIN_SEC", "1800"))
    force_rediscover = os.environ.get("SHOOTER_VOD_FORCE_REDISCOVER", "0") == "1"

    if entry and entry.get("last_pool_peaks"):
        cached = peak_values_from_entry(entry)
        cached_blocked = pool_peaks_fully_blocked(
            cached,
            used_peaks=used_peaks,
            gap_sec=seg_gap,
            blocked_sids=blocked_ids,
            vod_id=vid,
            lead_sec=lead,
        )
        # No unused owner hints either — do not burn hours rediscovering a dead long VOD.
        if cached_blocked and len(owner_hints) < min_clips:
            log.info(
                "skip highlight rescan — cached peaks blocked vod=%s peaks=%s owner_hints=%s",
                vod.name,
                cached[:4],
                len(owner_hints),
            )
            record_vod_scan(entry, sent=0, pool_peaks=cached, blocked=True)
            return 0

    # Normal pool: cache first. Full rediscover on long VODs is a known multi-hour hang —
    # only do it when forced or the file is short enough.
    if entry and pool_cache_valid(entry):
        pool = minimal_pool_from_entry(entry)
        log.info("reuse cached peak pool vod=%s peaks=%s", vod.name, len(pool))
    elif long_vod and not force_rediscover:
        pool = []
        log.warning(
            "skip full rediscover on long vod=%.0fs name=%s — use owner hints / download next",
            vod_dur,
            vod.name,
        )
    else:
        pool = discover_strict_candidates(vod, profile, sig, blocked_ids)

    if montage and owner_hints:
        pool = merge_owner_hints_into_pool(pool, owner_hints)
        log.info(
            "owner hints merged vod=%s hints=%s pool=%s",
            vod.name,
            len(owner_hints),
            len(pool),
        )

    pool_peaks = peaks_from_pool(pool)
    if not pool:
        log.info("no candidates %s", vod.name)
        if entry is not None:
            # Empty pool on a long/exhausted VOD must exhaust so discovery can fetch the next.
            record_vod_scan(entry, sent=0, pool_peaks=[], blocked=True)
        return 0

    probe_limit = int(os.environ.get("MLBB_VOD_PROBE_LIMIT", "24"))
    skip_peaks: set[float] = set()
    peak_tries = 0
    gate = _adaptive_gate(game)
    max_tries = max_peak_tries(soften_level, game=game, soft_max_fn=gate.soft_max_peak_tries)
    min_clip = float(os.environ.get("SHOOTER_VOD_MIN_CLIP_SCORE", "0.03"))
    owner_exemplars = os.environ.get("SHOOTER_VOD_OWNER_EXEMPLARS", "1") == "1"
    if montage:
        # Collect denser candidates; spacing for the final склейка is applied in _pick_montage_rows.
        cand_gap = min(seg_gap, float(os.environ.get("SHOOTER_VOD_MONTAGE_CANDIDATE_GAP_SEC", "12")))
        min_clip = float(
            os.environ.get(
                "SHOOTER_VOD_MONTAGE_MIN_CLIP_SCORE",
                str(min(min_clip, 0.01)),
            )
        )
        probe_limit = max(probe_limit, 32)
    else:
        cand_gap = seg_gap

    while peak_tries < max_tries:
        rows: list[dict] = []
        for clip in pool[:probe_limit]:
            peak = float(clip.get("start", 0))
            if any(abs(peak - s) <= 4.0 for s in skip_peaks):
                continue
            if _peak_too_close(peak, used_peaks, cand_gap):
                continue
            hm = clip.get("highlight_metrics") or {}
            clip_score = float(hm.get("clip_score") or 0.0)
            row_score = float(clip.get("score") or 0.0)
            pass_reason = str(hm.get("pass_reason") or "")
            clip_off = os.environ.get("HIGHLIGHT_CLIP_DISABLED", "0") == "1"
            # With CLIP disabled, clip_score is always 0 — do NOT drop rule/boss PASS
            # windows (Genshin was finding genshin_boss_ok then exhausting as blocked).
            if owner_exemplars and clip_score < min_clip and not clip.get("owner_anchor"):
                accept_noclip = clip_off and (
                    bool(hm.get("rule_pass"))
                    or pass_reason.startswith(
                        ("genshin_boss", "fight", "panns", "hist", "noclip", "boss")
                    )
                    or row_score >= min_clip
                    or float(hm.get("hook_score") or 0) >= 0.12
                )
                if not accept_noclip:
                    continue
            start = max(0.0, peak - lead)
            sid = segment_id(vid, start)
            if sid in blocked_ids:
                continue
            rows.append(
                {
                    "segment_id": sid,
                    "start": start,
                    "peak_start": peak,
                    "score": max(row_score, clip_score, float(hm.get("hook_score") or 0)),
                    "clip": {**clip, "start": start, "peak_start": peak},
                }
            )
        if not rows:
            blocked = pool_peaks_fully_blocked(
                pool_peaks,
                used_peaks=used_peaks,
                gap_sec=cand_gap,
                blocked_sids=blocked_ids,
                vod_id=vid,
                lead_sec=lead,
            )
            # Score-filter with CLIP off is NOT "all peaks blocked" — don't exhaust
            # a VOD that still has unused boss/fight windows.
            clip_off = os.environ.get("HIGHLIGHT_CLIP_DISABLED", "0") == "1"
            if not blocked and clip_off:
                log.warning(
                    "no rows after score filter (CLIP off) vod=%s pool=%s — keep for retry, no exhaust",
                    vod.name,
                    len(pool),
                )
                if entry is not None:
                    record_vod_scan(
                        entry, sent=0, pool_peaks=pool_peaks, blocked=False, pool=pool
                    )
                    entry["reject_reason"] = "clip_off_score_filter"
                return 0
            if not blocked:
                blocked = True
            log.warning(
                "all peaks blocked vod=%s pool=%s used_peaks=%s gap=%.0fs soften=%s blocked=%s",
                vod.name,
                len(pool),
                used_peaks,
                cand_gap,
                soften_level,
                blocked,
            )
            if entry is not None:
                record_vod_scan(entry, sent=0, pool_peaks=pool_peaks, blocked=blocked, pool=pool)
            return 0
        rows.sort(key=lambda r: float(r.get("score", 0)), reverse=True)
        # Montage path needs several spaced peaks; singles take the top one.
        batch = rows if montage else rows[:1]
        if montage and len(batch) < min_clips:
            log.warning(
                "montage need more peaks have=%s need=%s vod=%s — next vod (no exhaust)",
                len(batch),
                min_clips,
                vod.name,
            )
            if entry is not None:
                record_vod_scan(
                    entry,
                    sent=0,
                    pool_peaks=pool_peaks,
                    blocked=False,
                    pool=pool,
                )
                entry["reject_reason"] = f"montage_need_{min_clips}_have_{len(batch)}"
            return 0
        n = _send_batch(game, token, chat_id, vod, batch, sig)
        if n > 0:
            if entry is not None:
                record_vod_scan(entry, sent=n, pool_peaks=pool_peaks, blocked=False, pool=pool)
            return n
        # Rejected — skip the top candidate(s) and retry.
        for row in batch[:3]:
            skip_peaks.add(round(float(row.get("peak_start", row["start"])), 1))
        peak_tries += 1
        log.warning(
            "presend/montage rejected — try next (%s/%s) vod=%s game=%s batch=%s",
            peak_tries,
            max_tries,
            vod.name,
            game,
            len(batch),
        )
    if entry is not None:
        record_vod_scan(entry, sent=0, pool_peaks=pool_peaks, blocked=False, pool=pool)
    return 0


def _scan_vod_with_adaptive(
    game: str,
    token: str,
    chat_id: str,
    vod: Path,
    env: dict[str, str],
    state: dict,
) -> int:
    gate = _adaptive_gate(game)
    from mlbb_vod_adaptive_gate import should_notify_soften

    if game in EXTENDED_GAMES:
        from extended_vod_adaptive_gate import telegram_exhaust_notice, telegram_soften_notice
    else:
        from shooter_vod_adaptive_gate import telegram_exhaust_notice, telegram_soften_notice

    vid = vod_youtube_id(vod)
    title = _vod_title(state, vod)
    streak_in = gate.streak_from_state(state)
    entry = _vod_registry_entry(state, vod)

    if game == "pubg":
        ok_metro, metro_reason = _pubg_metro_vod_ok(vod, title=title, streak=streak_in)
        if not ok_metro:
            log.warning("metro reject scan vod=%s reason=%s", vod.name, metro_reason)
            entry = _vod_registry_entry(state, vod)
            if entry:
                entry["reject_reason"] = metro_reason
                if _pubg_metro_should_exhaust(title, streak_in):
                    entry["exhausted"] = True
            _save_state(game, state)
            return 0

    prev_level = int(state.get("last_adaptive_level") or 0)
    active_level = 0
    sent = 0
    clear_fast_seeds = None

    if game in ("pubg", "standoff", "wot") and os.environ.get("SHOOTER_VOD_FAST_PROBE", "1") == "1":
        from shooter_vod_fast_scan import (
            apply_fast_probe_seeds,
            clear_fast_probe_seeds,
            vod_fast_combat_check,
        )

        clear_fast_seeds = clear_fast_probe_seeds
        ok_fast, fast_reason, seed_peaks = vod_fast_combat_check(vod, _profile(game))
        if not ok_fast:
            # Owner anchors alone must NOT keep a dead VOD alive forever —
            # that spun every idle tick with zero Telegram and paid CPU.
            # Still try dense montage once when anchors exist; otherwise exhaust.
            has_anchors = game != "wot" and vod_has_owner_montage_anchors(game, vod)
            if not has_anchors:
                log.info("fast-skip vod=%s reason=%s", vod.name, fast_reason)
                _mark_vod_exhausted(
                    state,
                    vod,
                    reason=fast_reason,
                    delete_file=os.environ.get("SHOOTER_VOD_DELETE_EXHAUSTED", "1") == "1",
                )
                if entry is not None:
                    record_vod_scan(entry, sent=0, pool_peaks=[], blocked=True)
                _save_state(game, state)
                if os.environ.get("SHOOTER_VOD_FAST_SKIP_NOTIFY", "0") == "1":
                    send_message(token, chat_id, f"⏭ {game.upper()} {vid}: быстрый skip — {fast_reason}")
                return 0
            log.info(
                "fast-probe weak vod=%s reason=%s — one dense montage try via owner anchors",
                vod.name,
                fast_reason,
            )
            seed_peaks = seed_peaks or []
            apply_fast_probe_seeds(seed_peaks)
            ok_fast = True  # allow montage branch; if it fails we exhaust
        if ok_fast:
            apply_fast_probe_seeds(seed_peaks or [])
            # Fast ×3 montage: dense gun peaks → snap → presend → send.
            # NEVER fall into CLIP/highlight — that is the paid CPU hang.
            if (
                _montage_enabled(game)
                and os.environ.get("SHOOTER_VOD_FAST_MONTAGE", "1") == "1"
            ):
                from shooter_vod_fast_scan import discover_montage_gun_peaks

                min_clips, _max_c, gap_sec, part_max, _final = _montage_limits()
                dense_peaks, dense_reason = discover_montage_gun_peaks(
                    vod,
                    _profile(game),
                    min_clips=min_clips,
                    gap_sec=gap_sec,
                )
                log.info(
                    "fast-montage probe vod=%s reason=%s peaks=%s",
                    vod.name,
                    dense_reason,
                    dense_peaks[:8],
                )
                part_sec = min(
                    part_max,
                    float(
                        os.environ.get(
                            "SHOOTER_VOD_MONTAGE_PART_SEC",
                            str(
                                float(os.environ.get("SHOOTER_VOD_MONTAGE_GATE_CORE_SEC", "10"))
                                + 2.0 * float(os.environ.get("SHOOTER_VOD_MONTAGE_CORE_PAD_SEC", "2"))
                            ),
                        )
                    ),
                )
                sent_set = load_feed_sent(game)
                used_peaks = _used_peak_times(game, vid, sent_set)
                blocked_ids = labeled_ids(game) | sent_set

                def _build_rows(peak_gap: float) -> list[dict]:
                    out_rows: list[dict] = []
                    for idx, peak in enumerate(dense_peaks):
                        if _peak_too_close(float(peak), used_peaks, peak_gap):
                            continue
                        start = max(0.0, float(peak) - part_sec * 0.5)
                        sid = segment_id(vid, start)
                        if sid in blocked_ids:
                            continue
                        out_rows.append(
                            {
                                "segment_id": sid,
                                "start": start,
                                "peak_start": float(peak),
                                "score": max(0.2, 0.95 - idx * 0.03),
                                "clip": {
                                    "start": start,
                                    "peak_start": float(peak),
                                    "input_duration": part_sec,
                                    "output_duration": part_sec,
                                },
                            }
                        )
                    return out_rows

                rows = _build_rows(gap_sec * 0.9)
                if len(rows) < min_clips:
                    # Same VOD can still yield another ×3 if fights are denser.
                    tight = max(22.0, gap_sec * 0.45)
                    rows = _build_rows(tight)
                    if len(rows) >= min_clips:
                        log.info(
                            "fast-montage tight unused-gap vod=%s gap=%.0f→%.0f rows=%s",
                            vod.name,
                            gap_sec,
                            tight,
                            len(rows),
                        )
                if len(rows) >= min_clips:
                    n_fast = _send_montage(game, token, chat_id, vod, rows, file_sha256(vod))
                    if n_fast > 0:
                        if entry is not None:
                            record_vod_scan(
                                entry,
                                sent=n_fast,
                                pool_peaks=dense_peaks,
                                blocked=False,
                            )
                        _save_state(game, state)
                        if clear_fast_seeds:
                            clear_fast_seeds()
                        log.info(
                            "fast-montage SENT game=%s vod=%s n=%s peaks=%s",
                            game,
                            vod.name,
                            n_fast,
                            dense_peaks[:6],
                        )
                        return n_fast
                    log.warning(
                        "fast-montage rejected by gates vod=%s peaks=%s — skip slow highlight hang",
                        vod.name,
                        len(rows),
                    )
                    # Only exhaust when remaining unused dense peaks cannot form another shortlist.
                    remaining_unused = [
                        p
                        for p in dense_peaks
                        if not _peak_too_close(float(p), used_peaks, max(22.0, gap_sec * 0.45))
                    ]
                    if len(remaining_unused) < min_clips:
                        _mark_vod_exhausted(
                            state,
                            vod,
                            reason="fast_montage_presend_reject",
                            delete_file=False,
                        )
                    elif entry is not None:
                        record_vod_scan(
                            entry,
                            sent=0,
                            pool_peaks=dense_peaks,
                            blocked=False,
                        )
                        entry["reject_reason"] = "fast_montage_presend_reject_retry"
                    _save_state(game, state)
                    if clear_fast_seeds:
                        clear_fast_seeds()
                    return 0
                log.warning(
                    "fast-montage insufficient unused peaks vod=%s have=%s need=%s used=%s — exhaust for discovery",
                    vod.name,
                    len(rows),
                    min_clips,
                    len(used_peaks),
                )
                _mark_vod_exhausted(
                    state,
                    vod,
                    reason=f"fast_montage_need_{min_clips}_have_{len(rows)}",
                    delete_file=False,
                )
                if entry is not None:
                    record_vod_scan(entry, sent=0, pool_peaks=dense_peaks, blocked=True)
                _save_state(game, state)
                if clear_fast_seeds:
                    clear_fast_seeds()
                if _montage_only(game):
                    return 0

    if game == "genshin" and os.environ.get("GENSHIN_VOD_FAST_PROBE", "1") == "1":
        from genshin_vod_fast_scan import (
            apply_fast_probe_seeds as apply_genshin_seeds,
            clear_fast_probe_seeds as clear_genshin_seeds,
            vod_fast_boss_check,
        )

        clear_fast_seeds = clear_genshin_seeds
        ok_fast, fast_reason, seed_peaks = vod_fast_boss_check(vod, _profile(game))
        if not ok_fast:
            log.info("fast-skip vod=%s reason=%s", vod.name, fast_reason)
            if entry is None:
                entry = _vod_registry_entry(state, vod) or {
                    "id": vid,
                    "path": str(vod),
                    "title": title,
                    "exhausted": False,
                }
            entry["reject_reason"] = fast_reason
            entry["exhausted"] = True
            record_vod_scan(entry, sent=0, pool_peaks=[], blocked=False)
            _save_state(game, state)
            return 0
        apply_genshin_seeds(seed_peaks)

    # WoT covered by shooter fast-montage above when WOT_VOD_MONTAGE=1.
    # Keep legacy impact probe only when montage fast-path is off.
    if (
        game == "wot"
        and os.environ.get("WOT_VOD_FAST_PROBE", "1") == "1"
        and not (
            _montage_enabled(game) and os.environ.get("SHOOTER_VOD_FAST_MONTAGE", "1") == "1"
        )
    ):
        from wot_vod_fast_scan import (
            apply_fast_probe_seeds as apply_wot_seeds,
            clear_fast_probe_seeds as clear_wot_seeds,
            vod_fast_impact_check,
        )

        clear_fast_seeds = clear_wot_seeds
        ok_fast, fast_reason, seed_peaks = vod_fast_impact_check(vod, _profile(game))
        if not ok_fast:
            log.info("fast-skip vod=%s reason=%s", vod.name, fast_reason)
            if entry is None:
                entry = _vod_registry_entry(state, vod) or {
                    "id": vid,
                    "path": str(vod),
                    "title": title,
                    "exhausted": False,
                }
            entry["reject_reason"] = fast_reason
            entry["exhausted"] = True
            record_vod_scan(entry, sent=0, pool_peaks=[], blocked=False)
            _save_state(game, state)
            return 0
        apply_wot_seeds(seed_peaks)

    # Hard anti-hang: montage-only shooters never enter CLIP/hist highlight.
    # Fast-montage above is the only productive path; fallback burns paid CPU.
    if (
        game in ("pubg", "standoff", "wot")
        and _montage_only(game)
        and os.environ.get("SHOOTER_VOD_FAST_PROBE", "1") == "1"
        and os.environ.get("SHOOTER_VOD_FAST_MONTAGE", "1") == "1"
        and os.environ.get("SHOOTER_VOD_ALLOW_HIGHLIGHT_FALLBACK", "0") != "1"
    ):
        log.warning(
            "montage-only anti-hang: skip highlight fallback game=%s vod=%s",
            game,
            vod.name,
        )
        if clear_fast_seeds is not None:
            clear_fast_seeds()
        if entry is not None:
            entry["reject_reason"] = entry.get("reject_reason") or "montage_fast_path_no_send"
            record_vod_scan(entry, sent=0, pool_peaks=[], blocked=True)
        _mark_vod_exhausted(
            state,
            vod,
            reason="montage_fast_path_no_send",
            delete_file=False,
        )
        _save_state(game, state)
        return 0

    try:
        ctx = gate.adaptive_env(game, streak_in) if game in EXTENDED_GAMES else gate.adaptive_env(streak_in)
        with ctx as level:
            active_level = level
            if game == "pubg":
                os.environ["PUBG_METRO_SEGMENT_TRUST_VOD"] = "1"
            if should_notify_soften(streak_in, level, prev_level=prev_level) and os.environ.get(
                "SHOOTER_VOD_ADAPTIVE_NOTIFY", os.environ.get("MLBB_VOD_ADAPTIVE_NOTIFY", "1")
            ) == "1":
                log.warning(
                    "adaptive soften active game=%s streak=%s level=%s vod=%s",
                    game,
                    streak_in,
                    level,
                    vod.name,
                )
                send_message(token, chat_id, telegram_soften_notice(game, streak_in, level))
            elif level > 0:
                log.warning(
                    "adaptive soften active game=%s streak=%s level=%s vod=%s (no tg spam)",
                    game,
                    streak_in,
                    level,
                    vod.name,
                )
            sent = _scan_vod(game, token, chat_id, vod, env, soften_level=level, entry=entry)
            if game == "pubg":
                os.environ.pop("PUBG_METRO_SEGMENT_TRUST_VOD", None)
    finally:
        if clear_fast_seeds is not None:
            clear_fast_seeds()

    new_streak = gate.record_vod_outcome(state, vod_id=vid, sent=sent)
    state["last_adaptive_level"] = active_level
    _save_state(game, state)

    if sent == 0 and os.environ.get("SHOOTER_VOD_EXHAUST_NOTIFY", os.environ.get("MLBB_VOD_EXHAUST_NOTIFY", "1")) == "1":
        if active_level == 0 or new_streak % 2 == 0:
            entry = _vod_registry_entry(state, vod)
            send_message(
                token,
                chat_id,
                telegram_exhaust_notice(
                    game,
                    vid,
                    level=active_level,
                    streak=new_streak,
                    detail=scan_zero_detail(entry),
                ),
            )

    return sent


def _purge_junk_inbox_vods(game: str, inbox: Path) -> int:
    """Park/delete too-short inbox files so discovery isn't blocked on trash."""
    parked = inbox.parent / "parked"
    parked.mkdir(parents=True, exist_ok=True)
    min_sec = _vod_min_sec()
    removed = 0
    for mp4 in list(inbox.glob("yt_*.mp4")):
        dur = _ffprobe_duration(mp4)
        if dur >= min_sec and _shooter_vod_length_ok(mp4, dur):
            continue
        dest = parked / mp4.name
        try:
            if dest.exists():
                mp4.unlink(missing_ok=True)
            else:
                mp4.rename(dest)
            removed += 1
            log.info("parked short inbox vod=%s dur=%.0fs min=%.0fs", mp4.name, dur, min_sec)
        except OSError as exc:
            log.warning("park short inbox fail %s: %s", mp4.name, exc)
    return removed


def _recycle_parked_vod(game: str, state: dict, inbox: Path) -> Path | None:
    """
    When YouTube discovery is 403/empty, pull ONE parked VOD back — with memory.

    Without recycle_count / cooldown this became park→dense-PANNs→exhaust→park
    forever (busy-idle on paid CPU). Never recycle VODs that already failed
    gate/shortlist unless unused peaks remain and attempts remain.
    """
    if os.environ.get("SHOOTER_VOD_RECYCLE_PARKED", "1") != "1":
        return None
    max_recycles = max(1, int(os.environ.get("SHOOTER_VOD_RECYCLE_MAX_PER_VOD", "1")))
    cooldown = float(os.environ.get("SHOOTER_VOD_RECYCLE_COOLDOWN_SEC", "1800"))
    dead_reasons = {
        "fast_montage_presend_reject",
        "montage_fast_path_no_send",
        "no_combat_peaks",
        "all_peaks_blocked",
    }
    parked = inbox.parent / "parked"
    if not parked.is_dir():
        return None
    min_sec = _vod_min_sec()
    candidates: list[tuple[float, Path, dict]] = []
    registry = state.setdefault("vods", [])
    now = time.time()
    for mp4 in parked.glob("yt_*.mp4"):
        dur = _ffprobe_duration(mp4)
        if dur < min_sec or not _shooter_vod_length_ok(mp4, dur):
            continue
        vid = vod_youtube_id(mp4)
        entry = next((r for r in registry if str(r.get("id") or "") == vid), None) or {}
        recycles = int(entry.get("recycle_count") or 0)
        if recycles >= max_recycles:
            continue
        last_rec = float(entry.get("last_recycle_at") or 0)
        if last_rec > 0 and (now - last_rec) < cooldown:
            continue
        reason = str(entry.get("reject_reason") or "")
        reason_base = reason.split("=", 1)[0]
        if reason_base in dead_reasons or reason.startswith("fast_panns_0"):
            # Already failed productive path — don't burn another dense scan.
            continue
        if reason.startswith("fast_montage_need_"):
            continue
        candidates.append((dur, mp4, entry if entry else {"id": vid}))
    if not candidates:
        # Arm discovery pause so cycle doesn't thrash every 8s.
        pause = float(os.environ.get("SHOOTER_VOD_DISCOVERY_PAUSE_SEC", "900"))
        state["discovery_pause_until"] = max(
            float(state.get("discovery_pause_until") or 0),
            now + pause,
        )
        log.warning(
            "recycle: no eligible parked VOD game=%s — discovery pause %.0fs",
            game,
            pause,
        )
        return None
    candidates.sort(key=lambda t: -t[0])
    _dur, src, entry = candidates[0]
    dest = inbox / src.name
    try:
        if dest.exists():
            dest.unlink()
        src.rename(dest)
    except OSError as exc:
        log.warning("recycle parked fail %s: %s", src.name, exc)
        return None
    upserted = _upsert_vod_registry(
        state,
        vid=vod_youtube_id(dest),
        path=str(dest),
        title=str(entry.get("title") or ""),
        exhausted=False,
        reject_reason="",
    )
    upserted["exhausted"] = False
    upserted.pop("reject_reason", None)
    upserted["recycle_count"] = int(entry.get("recycle_count") or 0) + 1
    upserted["last_recycle_at"] = now
    log.info(
        "recycled parked vod=%s dur=%.0fs recycle=%s → inbox",
        dest.name,
        _dur,
        upserted["recycle_count"],
    )
    return dest


def _run(game: str, env: dict[str, str], token: str, chat_id: str) -> int:
    log.info("shooter feed start game=%s rev=%s", game, VOD_PIPELINE_REV)
    ok_cycle, reason = can_send_for_game(game, 1)
    if not ok_cycle:
        log.info("skip feed game=%s reason=%s", game, reason)
        return 0

    state = _load_state(game)
    registry = state.setdefault("vods", [])
    used = set(state.get("used_youtube_ids", []))
    inbox = _paths(game)["inbox"]
    inbox.mkdir(parents=True, exist_ok=True)
    purged = _purge_junk_inbox_vods(game, inbox)
    if purged:
        log.info("purged short inbox vods game=%s count=%s", game, purged)

    inbox_files = sorted(inbox.glob("yt_*.mp4"), key=lambda p: _inbox_order_key(p, registry, game=game))
    max_vods = max(1, int(os.environ.get("SHOOTER_VOD_MAX_VODS_PER_RUN", "3")))
    tried = 0
    for mp4 in inbox_files:
        if tried >= max_vods:
            log.info("max vods per run reached game=%s n=%s", game, max_vods)
            break
        entries = _vod_registry_entries(state, mp4)
        if any(r.get("exhausted") for r in entries):
            # Collapse duplicate rows so the next cycle stays clean.
            _mark_vod_exhausted(
                state,
                mp4,
                reason=str(entries[0].get("reject_reason") or "exhausted"),
                delete_file=os.environ.get("SHOOTER_VOD_DELETE_EXHAUSTED", "1") == "1",
            )
            continue
        entry = entries[0] if entries else None
        if should_skip_vod_rescan(entry, game=game):
            log.info("skip scan cooldown vod=%s", mp4.name)
            continue
        if _ffprobe_duration(mp4) < _vod_min_sec():
            continue
        if not _shooter_vod_length_ok(mp4):
            log.info(
                "skip length vod=%s dur=%.0f min=%.0f max=%.0f",
                mp4.name,
                _ffprobe_duration(mp4),
                _vod_min_sec(),
                _vod_max_sec(),
            )
            continue
        tried += 1
        if entry is None:
            entry = _upsert_vod_registry(
                state,
                vid=vod_youtube_id(mp4),
                path=str(mp4),
                title="",
                exhausted=False,
            )
        if game == "pubg":
            streak_in = _adaptive_gate(game).streak_from_state(state)
            title = str(entry.get("title") or "")
            ok_metro, metro_reason = _pubg_metro_vod_ok(mp4, title=title, streak=streak_in)
            if not ok_metro:
                log.warning("metro skip inbox vod=%s reason=%s", mp4.name, metro_reason)
                if _pubg_metro_should_exhaust(title, streak_in):
                    _mark_vod_exhausted(state, mp4, reason=metro_reason, delete_file=True)
                else:
                    entry["reject_reason"] = metro_reason
                _save_state(game, state)
                continue
        n = _scan_vod_with_adaptive(game, token, chat_id, mp4, env, state)
        state["vods"] = registry
        if n == 0:
            entry = _vod_registry_entry(state, mp4) or entry
            if entry and not entry.get("exhausted") and should_mark_vod_exhausted(entry):
                reason = (
                    "no_combat_peaks"
                    if not entry.get("last_pool_peaks")
                    else "all_peaks_blocked"
                )
                _mark_vod_exhausted(
                    state,
                    mp4,
                    reason=reason,
                    delete_file=os.environ.get("SHOOTER_VOD_DELETE_EXHAUSTED", "1") == "1",
                )
                log.info("exhausted vod=%s reason=%s", mp4.name, reason)
        _save_state(game, state)
        if n > 0:
            print(f"pipeline done sent={n} vods=1 game={game}")
            return 0
        # Keep going to next inbox VOD in same run (no 25s idle tax per reject).
        log.info("zero-send continue next inbox vod game=%s tried=%s", game, tried)

    # If discovery is paused and we have nothing to scan this tick, stop immediately
    # (do not fall into empty search / recycle loops that burn idle every 8s).
    pause_until = float(state.get("discovery_pause_until") or 0)
    if pause_until > time.time() and tried == 0:
        log.warning(
            "no scannable inbox + discovery paused — yield game=%s until=%.0f",
            game,
            pause_until,
        )
        print(f"pipeline done sent=0 vods=0 game={game} inbox_dead=1")
        return 0

    # All inbox VODs exhausted — park them and discover a fresh VOD.
    # Old behavior skipped discovery entirely (inbox_dead=1), which left the
    # cycle spinning every idle tick with zero productive work.
    if inbox_files:
        usable = False
        for mp4 in inbox_files:
            entries = _vod_registry_entries(state, mp4)
            if not entries or not any(r.get("exhausted") for r in entries):
                if _ffprobe_duration(mp4) >= _vod_min_sec() and _shooter_vod_length_ok(mp4):
                    usable = True
                    break
        if not usable:
            parked = inbox.parent / "parked"
            parked.mkdir(parents=True, exist_ok=True)
            for mp4 in list(inbox.glob("yt_*.mp4")):
                entries = _vod_registry_entries(state, mp4)
                if entries and any(r.get("exhausted") for r in entries):
                    dest = parked / mp4.name
                    try:
                        if dest.exists():
                            mp4.unlink(missing_ok=True)
                        else:
                            mp4.rename(dest)
                        log.info("parked exhausted inbox vod=%s → discovery", mp4.name)
                    except OSError as exc:
                        log.warning("park exhausted fail %s: %s", mp4.name, exc)
            # Still block discovery only when recently paused (403 / empty search).
            pause_until = float(state.get("discovery_pause_until") or 0)
            if pause_until > time.time() and os.environ.get(
                "SHOOTER_VOD_SKIP_DISCOVERY_WHEN_INBOX_DEAD", "1"
            ) == "1":
                log.warning(
                    "inbox exhausted + discovery paused — wait game=%s until=%.0f",
                    game,
                    pause_until,
                )
                print(f"pipeline done sent=0 vods=0 game={game} inbox_dead=1")
                return 0
            log.info("inbox exhausted parked — fall through discovery game=%s", game)

    if os.environ.get("SHOOTER_VOD_SKIP_DISCOVERY", "0") == "1":
        log.info("skip discovery — inbox exhausted game=%s", game)
        print(f"pipeline done sent=0 vods=0 game={game} skip_discovery=1")
        return 0

    candidates = _discover_candidates(game, env, used)
    if not candidates:
        # YouTube 403 / empty search — recycle longest parked VOD that still has room.
        recycled = _recycle_parked_vod(game, state, inbox)
        if recycled is not None:
            _save_state(game, state)
            n = _scan_vod_with_adaptive(game, token, chat_id, recycled, env, state)
            print(f"pipeline done sent={n} vods=1 game={game} recycled=1")
            return 0
        # Nothing to download and nothing to recycle — hard idle, signal cycle.
        pause_until = float(state.get("discovery_pause_until") or 0)
        dead = "inbox_dead=1" if pause_until > time.time() else "discovery_miss=1"
        if os.environ.get("SHOOTER_VOD_DISCOVERY_MISS_NOTIFY", "0") == "1":
            send_message(token, chat_id, f"⚠️ Не нашёл новый {game.upper()} стрим. Повторю позже.")
        print(f"pipeline done sent=0 vods=0 game={game} {dead}")
        return 0

    pick = None
    if game in ("pubg", "standoff"):
        from youtube_shooter_vod_prefs import pick_discovery_candidate

        pick = pick_discovery_candidate(game, candidates)
    if pick is None:
        pick = candidates[0]
    if os.environ.get("SHOOTER_VOD_DOWNLOAD_NOTIFY", "0") == "1":
        send_message(token, chat_id, f"📥 Качаю {game.upper()} VOD с YouTube…")
    vod = _download_vod(game, pick, env)
    if not vod:
        print(f"pipeline done sent=0 vods=0 game={game}")
        return 0

    if game == "pubg":
        streak_dl = _adaptive_gate(game).streak_from_state(state)
        ok_metro, metro_reason = _pubg_metro_vod_ok(
            vod,
            title=str(pick.get("title") or ""),
            streak=streak_dl,
        )
        if not ok_metro:
            log.warning("metro reject vod=%s title=%s reason=%s", pick.get("id"), pick.get("title", ""), metro_reason)
            if os.environ.get("SHOOTER_VOD_METRO_REJECT_NOTIFY", "0") == "1":
                send_message(
                    token,
                    chat_id,
                    f"⏭ Пропускаю VOD — не Metro Royale: {pick.get('title', pick.get('id'))[:80]}\n{metro_reason}",
                )
            exhausted = _pubg_metro_should_exhaust(str(pick.get("title") or ""), streak_dl)
            _upsert_vod_registry(
                state,
                vid=pick["id"],
                path=str(vod),
                title=str(pick.get("title") or ""),
                exhausted=exhausted,
                reject_reason=metro_reason,
            )
            used.add(pick["id"])
            state["used_youtube_ids"] = sorted(used)
            _save_state(game, state)
            if exhausted and os.environ.get("SHOOTER_VOD_DELETE_EXHAUSTED", "1") == "1":
                try:
                    Path(vod).unlink(missing_ok=True)
                except OSError:
                    pass
            print(f"pipeline done sent=0 vods=1 game={game} metro_reject=1")
            return 0

    _upsert_vod_registry(
        state,
        vid=pick["id"],
        path=str(vod),
        title=str(pick.get("title") or ""),
        exhausted=False,
    )
    used.add(pick["id"])
    state["used_youtube_ids"] = sorted(used)
    _save_state(game, state)

    n = _scan_vod_with_adaptive(game, token, chat_id, vod, env, state)
    print(f"pipeline done sent={n} vods=1 game={game}")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    game = _game()
    os.environ.setdefault("HIGHLIGHT_HEATMAP", "0")
    os.environ.setdefault("SHOOTER_VOD_FEED", "1")
    os.environ.setdefault("SHOOTER_VOD_FAST_PROBE", "1")
    os.environ.setdefault("SHOOTER_VOD_FAST_MONTAGE", "1")
    os.environ.setdefault("SHOOTER_VOD_MAX_VODS_PER_RUN", "3")
    os.environ.setdefault("SHOOTER_VOD_SKIP_DISCOVERY_WHEN_INBOX_DEAD", "1")
    os.environ.setdefault("SHOOTER_VOD_MAX_SEC", "3600")
    os.environ.setdefault("SHOOTER_VOD_MIN_SEC", "600")
    os.environ.setdefault("HIGHLIGHT_ALLOW_NO_CLIP", "1")
    # Never re-enable CLIP via setdefault(…, "0") after this — paid hang.
    os.environ["HIGHLIGHT_CLIP_DISABLED"] = os.environ.get("HIGHLIGHT_CLIP_DISABLED", "1") or "1"
    if os.environ.get("HIGHLIGHT_CLIP_DISABLED") != "1":
        os.environ["HIGHLIGHT_CLIP_DISABLED"] = "1"
    os.environ.setdefault("SHOOTER_VOD_PREFER_RUSSIAN", "1")
    os.environ.setdefault("SHOOTER_VOD_SKIP_INTELLICLIP", "1")
    os.environ.setdefault("SHOOTER_VOD_MAX_PANN_PROBE", "24")
    os.environ.setdefault("HIGHLIGHT_MAX_STAGE1", "32")
    if os.environ.get("SHOOTER_VOD_OWNER_EXEMPLARS", "1") == "1":
        os.environ["HIGHLIGHT_USE_OWNER_ANCHORS"] = "1"
    else:
        os.environ.setdefault("HIGHLIGHT_USE_OWNER_ANCHORS", "0")
    lock = _feed_lock(game)
    if lock is None:
        return 0
    env = {**os.environ, **load_env(ENV_PATH)}
    for key in (
        "SHOOTER_VOD_FEED",
        "SHOOTER_VOD_FAST_PROBE",
        "SHOOTER_VOD_PREFER_RUSSIAN",
        "SHOOTER_VOD_SKIP_INTELLICLIP",
        "SHOOTER_VOD_MAX_PANN_PROBE",
        "HIGHLIGHT_MAX_STAGE1",
        "SHOOTER_VOD_OWNER_EXEMPLARS",
        "SHOOTER_VOD_OWNER_BACKFILL",
        "SHOOTER_VOD_MIN_CLIP_SCORE",
        "HIGHLIGHT_USE_OWNER_ANCHORS",
    ):
        if key in env:
            os.environ[key] = str(env[key])
    _bootstrap_owner_exemplars(game)
    token = env.get("TG_BOT_TOKEN", "").strip()
    chat_id = env.get("TG_CHAT_ID", "").strip()
    if not token or not chat_id:
        log.error("TG_BOT_TOKEN / TG_CHAT_ID missing")
        return 1
    try:
        return _run(game, env, token, chat_id)
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
