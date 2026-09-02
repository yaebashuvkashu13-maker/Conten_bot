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
    _ffprobe_duration,
    _vod_target_dur_sec,
    render_single_segment,
    send_message,
    send_video,
)
from shooter_vod_bg_download import ShooterVodBgDownloader
from pubg_combat_gate import pubg_passes_combat_gate
from pubg_metro_royale_gate import title_metro_hint
from shooter_vod_segment_store import (
    keyboard,
    keyboard_for_parts,
    labeled_ids,
    load_feed_sent,
    load_index,
    mark_feed_sent,
    peak_label_sec,
    segment_id,
    stats,
    upsert_segment,
    vod_youtube_id,
    _paths,
)
from shooter_owner_montage import (
    merge_owner_hints_into_pool,
    owner_good_fight_peaks,
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
from vod_game_registry import VOD_PIPELINE_REV, trim_used_youtube_ids
from vod_scan_funnel import ScanFunnel
from vod_quality import dense_probe_passes, montages_per_vod, pubg_quality_strict
from youtube_download import load_env

log = logging.getLogger("shooter_vod_feed")


def _vod_min_sec() -> float:
    """Shooter VODs: prefer long streams, but allow ~4min combat VODs.

    A hard 600s floor left Standoff/WoT idle when inbox held only short
    fight clips (owner SLA: ship highlights, don't wait for 10min+ only).
    """
    raw = os.environ.get("SHOOTER_VOD_MIN_SEC") or os.environ.get("MLBB_VOD_MIN_SEC")
    if raw:
        try:
            base = float(raw)
        except ValueError:
            base = 180.0
    else:
        base = 120.0
    if os.environ.get("SHOOTER_VOD_MONTAGE", "1") == "1":
        montage_floor = float(os.environ.get("SHOOTER_VOD_MONTAGE_MIN_VOD_SEC", "120"))
        return max(base, montage_floor)
    return base


def _vod_max_sec() -> float:
    # Never inherit MLBB_VOD_MAX_SEC=1200 — that purged 20–30 min montage streams.
    # Long ranked/custom streams (1.5–3h) are the best montage sources.
    raw = os.environ.get("SHOOTER_VOD_MAX_SEC")
    if raw:
        try:
            val = float(raw)
        except ValueError:
            val = 14400.0
    else:
        val = 14400.0
    # A mistaken MAX_SEC=3600 skipped 90min combat VODs (FpMs) and left the
    # feed thrashing 3–5min junk for hours. Floor at 3h usable body.
    floor = float(os.environ.get("SHOOTER_VOD_MAX_SEC_FLOOR", "10800"))
    if val < floor:
        log.warning(
            "SHOOTER_VOD_MAX_SEC=%.0f below floor=%.0f — raising (long combat VODs)",
            val,
            floor,
        )
        val = floor
    return val


def _shooter_vod_length_ok(path: Path, dur: float | None = None) -> bool:
    length = dur if dur is not None else _ffprobe_duration(path)
    return _vod_min_sec() <= length <= _vod_max_sec()


def _inbox_mp4_files(inbox: Path) -> list[Path]:
    files = list(inbox.glob("yt_*.mp4")) + list(inbox.glob("tw_*.mp4"))
    return sorted(set(files), key=lambda p: p.name)


def _purge_junk_inbox_vods(game: str, inbox: Path) -> int:
    """Park only too-short inbox files. Never park long VODs for exceeding max."""
    parked = inbox.parent / "parked"
    parked.mkdir(parents=True, exist_ok=True)
    min_sec = _vod_min_sec()
    removed = 0
    for mp4 in list(_inbox_mp4_files(inbox)):
        dur = _ffprobe_duration(mp4)
        if dur >= min_sec:
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


ENV_PATH = Path("/root/.video_bot.env")
EXTENDED_GAMES = frozenset({"genshin", "wot"})
FEED_GAMES = frozenset({"pubg", "standoff", *EXTENDED_GAMES})
DENSE_POOL_VERSION = 3


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
    pid_path = Path(f"/tmp/{game}_vod_segment_feed.pid")
    try:
        if pid_path.exists():
            old_pid = int(pid_path.read_text().strip() or "0")
            if old_pid > 1:
                os.kill(old_pid, 0)
    except (ProcessLookupError, ValueError, OSError):
        try:
            lock_path.unlink(missing_ok=True)
            pid_path.unlink(missing_ok=True)
        except OSError:
            pass
    handle = lock_path.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        log.warning("another %s feed running — exit", game)
        return None
    pid_path.write_text(str(os.getpid()))
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
    trimmed = trim_used_youtube_ids(state, game)
    if trimmed:
        _save_state(game, state)
        used = set(state.get("used_youtube_ids", []))
        log.info(
            "trimmed used_youtube_ids game=%s removed=%s remain=%s",
            game,
            trimmed,
            len(used),
        )
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

    def _search_youtube(urls: list[str], delay: float) -> None:
        nonlocal saw_403
        for url in urls:
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
            time.sleep(delay)

    _search_youtube(list(params.get("urls", [])), float(params.get("delay", 6)))

    if not out and not saw_403 and game == "pubg":
        from youtube_shooter_vod_prefs import pubg_en_fallback_search_urls

        fb_urls = pubg_en_fallback_search_urls(int(params.get("limit", 40)))
        log.info("discovery RU/empty batch — EN fallback game=%s urls=%s", game, len(fb_urls))
        _search_youtube(fb_urls, min(float(params.get("delay", 6)), 4.0))

    if not out and not saw_403:
        state = _load_state(game)
        freed = trim_used_youtube_ids(state, game, aggressive=True)
        if freed:
            _save_state(game, state)
            used.clear()
            used.update(state.get("used_youtube_ids", []))
            log.info(
                "discovery empty — cleared stale used ids game=%s removed=%s retry",
                game,
                freed,
            )
            _search_youtube(list(params.get("urls", [])), float(params.get("delay", 6)))
            if not out and game == "pubg":
                from youtube_shooter_vod_prefs import pubg_en_fallback_search_urls

                _search_youtube(
                    pubg_en_fallback_search_urls(int(params.get("limit", 40))),
                    min(float(params.get("delay", 6)), 4.0),
                )

    from twitch_vod_prefs import twitch_vod_enabled

    if twitch_vod_enabled(game):
        from twitch_vod_prefs import (
            parse_flat_playlist_line,
            title_ok as twitch_title_ok,
            vod_discovery_search_cycle as twitch_cycle,
        )

        t_cycle = int(state.get("twitch_discovery_cycle", 0))
        t_params = twitch_cycle(t_cycle, game, env)
        state["twitch_discovery_cycle"] = t_cycle + 1
        _save_state(game, state)
        twitch_limit = int(t_params.get("limit", 12))
        for url in t_params.get("urls", []):
            channel_login = ""
            if "/videos" in url:
                parts = url.split("twitch.tv/", 1)[-1].split("/", 1)
                if parts:
                    channel_login = parts[0].strip().lower()
            cmd = ytdlp_cmd(env) + [
                "--flat-playlist",
                "--playlist-end",
                str(twitch_limit),
                "--print",
                "%(id)s|%(title)s|%(duration)s|%(uploader)s|%(live_status)s",
                url,
            ]
            cmd += ytdlp_extra_args(env)
            proc = run_ytdlp(cmd, env, timeout=120, label=f"twitch-search-{game}")
            if proc.returncode != 0:
                err = (proc.stderr or "")[:400]
                log.warning("twitch search failed %s: %s", url, err)
                continue
            for line in (proc.stdout or "").splitlines():
                row = parse_flat_playlist_line(line)
                if not row:
                    continue
                vid = row["id"]
                if vid in used:
                    continue
                title = row["title"]
                live_status = row.get("live_status", "")
                if live_status in ("is_live", "is_upcoming"):
                    used.add(vid)
                    continue
                if not twitch_title_ok(game, title, channel_login=channel_login):
                    continue
                try:
                    dur = float(row.get("duration") or 0)
                except ValueError:
                    dur = 0.0
                if dur <= 0:
                    used.add(vid)
                    continue
                if not _shooter_vod_length_ok(Path("x.mp4"), dur):
                    continue
                out.append(
                    {
                        "id": vid,
                        "title": title,
                        "url": row["url"],
                        "duration": dur,
                        "uploader": row.get("uploader", ""),
                        "source": "twitch",
                    }
                )
            time.sleep(float(t_params.get("delay", 6)))

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


def _pubg_score_mode_ready() -> bool:
    if os.environ.get("PUBG_PRESEND_SCORE_MODE", "1") != "1":
        return False
    if os.environ.get("PUBG_SCORE_REQUIRE_RANKER", "1") != "1":
        return True
    try:
        from pubg_moment_ranker import ranker_available

        return ranker_available()
    except Exception:
        return False


def _validate_shooter_presend(
    game: str,
    vod: Path,
    row: dict,
    rendered: Path,
    *,
    montage_part: bool = False,
    single: bool = False,
) -> tuple[bool, str, dict]:
    profile = _profile(game)
    # Gate the same window that was rendered (peak-centered clip start), not peak_start.
    start = _row_window_start(row)
    dur = _ffprobe_duration(rendered)
    if dur <= 0:
        clip = row.get("clip") if isinstance(row.get("clip"), dict) else {}
        dur = float(clip.get("input_duration") or clip.get("output_duration") or row.get("duration") or 15)
    if game == "pubg" and os.environ.get("PUBG_METRO_GATE", "0") == "1" and not montage_part:
        from pubg_metro_royale_gate import segment_looks_metro_royale

        ok_metro, metro_reason = segment_looks_metro_royale(vod, start, dur)
        if not ok_metro:
            return False, metro_reason, {"metro": metro_reason}
    if game == "pubg" and _pubg_score_mode_ready():
        from pubg_clip_shape_gate import validate_clip_fight_shape
        from pubg_quality_score import score_pubg_window

        peak = float(row.get("peak_start") or start)
        clip = row.get("clip") if isinstance(row.get("clip"), dict) else {}
        report = clip.get("segment_report") if isinstance(clip.get("segment_report"), dict) else {}
        if report:
            ok_shape, shape_reason = validate_clip_fight_shape(start, dur, peak, report)
            if not ok_shape:
                return False, f"shape_{shape_reason}", {"shape_reject": shape_reason}
        return score_pubg_window(vod, start, dur, single=single)
    if game in EXTENDED_GAMES:
        # WoT: never use PUBG gunfire shooting gate — tank cannon scores as
        # talk_low_gun (gun~0.01) and starved the SLA hour. Soft cruise impact
        # gate (strict_segment_gate) is the correct combat check for tanks.
        from strict_segment_gate import passes_strict_gate

        gate_start, gate_dur = start, float(dur)
        if game == "wot" and montage_part:
            peak = float(row.get("peak_start") or start)
            core = float(os.environ.get("SHOOTER_VOD_MONTAGE_GATE_CORE_SEC", "10"))
            gate_start = max(0.0, peak - core * 0.5)
            gate_dur = min(float(dur), core)
            if gate_start + gate_dur > start + float(dur):
                gate_start = max(0.0, start + float(dur) - gate_dur)
        ok, reason, metrics = passes_strict_gate(vod, gate_start, gate_dur, profile)
        metrics = dict(metrics or {})
        metrics["gate_core_start"] = round(gate_start, 2)
        metrics["gate_core_dur"] = round(gate_dur, 2)
        if game == "wot" and montage_part:
            ok, reason = soft_allow_owner_montage_part(
                game, vod, gate_start, ok, reason, montage_part=True, metrics=metrics
            )
        if game == "genshin" and ok:
            from genshin_boss_segment import validate_genshin_boss_segment

            boss_ok, boss_reason, boss_metrics = validate_genshin_boss_segment(vod, start, dur)
            metrics.update(boss_metrics)
            if not boss_ok:
                return False, boss_reason, metrics
        return ok, reason, metrics
    # Montage parts: shooting-only is faster but skips full combat visual gate.
    # Quality-strict PUBG requires the same presend stack as single sends.
    shooting_only = (
        montage_part
        and os.environ.get(
            "SHOOTER_VOD_MONTAGE_SHOOTING_ONLY",
            "0" if game == "pubg" else "1",
        )
        == "1"
        and not (game == "pubg" and pubg_quality_strict())
    )
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
    if not (game == "pubg" and pubg_quality_strict() and montage_part):
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


def _dense_probe_pass_index(entry: dict | None) -> int:
    """Rotate dense PANNs grid across revisits of the same long VOD."""
    passes = dense_probe_passes()
    if passes <= 1:
        return 0
    visit = int((entry or {}).get("dense_probe_visit") or 0)
    return visit % passes


def _bump_dense_probe_visit(entry: dict | None) -> None:
    if entry is not None:
        entry["dense_probe_visit"] = int(entry.get("dense_probe_visit") or 0) + 1


def _dense_rejected_peaks(entry: dict | None) -> list[float]:
    out: list[float] = []
    for value in (entry or {}).get("dense_rejected_peaks") or []:
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            continue
    return out


def _remember_dense_rejections(entry: dict | None, peaks: list[float]) -> None:
    if entry is None or not peaks:
        return
    merged = _dense_rejected_peaks(entry)
    for peak in peaks:
        value = float(peak)
        if not any(abs(value - old) <= 4.0 for old in merged):
            merged.append(round(value, 1))
    entry["dense_rejected_peaks"] = merged[-64:]


def _dense_pool_cache_usable(entry: dict | None, cached_peaks: list[float], min_clips: int) -> bool:
    if len(cached_peaks) < min_clips:
        return False
    if int((entry or {}).get("dense_pool_version") or 0) != DENSE_POOL_VERSION:
        return False
    reason = str((entry or {}).get("reject_reason") or "")
    if reason.startswith("fast_montage_presend_reject"):
        return False
    return True


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


def _dense_on_fast_probe_miss(game: str) -> bool:
    return (
        _montage_enabled(game)
        and os.environ.get("SHOOTER_VOD_FAST_MONTAGE", "1") == "1"
        and os.environ.get("SHOOTER_VOD_DENSE_ON_FAST_MISS", "1") == "1"
    )


def _montage_xfade_sec() -> float:
    return float(os.environ.get("SHOOTER_VOD_MONTAGE_XFADE_SEC", "0.28"))


def _montage_part_budget(num_parts: int, final_max: float) -> float:
    """Per-part seconds so N parts + xfades fit in final_max (default 55s ×3)."""
    n = max(1, int(num_parts))
    return (float(final_max) + _montage_xfade_sec() * (n - 1)) / n


def _montage_try_parts(max_clips: int, soft_min: int) -> int:
    """How many parts to attempt — up to max_clips, ship with soft_min if fewer pass."""
    raw = os.environ.get("SHOOTER_VOD_MONTAGE_PREFER_PARTS", "").strip()
    if raw:
        return max(soft_min, min(max_clips, int(raw)))
    return max_clips


def _montage_limits() -> tuple[int, int, float, float, float]:
    """min_clips, max_clips, gap_sec, part_max_sec, final_max_sec."""
    min_clips = max(1, int(os.environ.get("SHOOTER_VOD_MONTAGE_MIN_CLIPS", "2")))
    max_clips = max(min_clips, int(os.environ.get("SHOOTER_VOD_MONTAGE_MAX_CLIPS", "3")))
    gap = float(os.environ.get("SHOOTER_VOD_MONTAGE_GAP_SEC", "55"))
    part_max = float(os.environ.get("SHOOTER_VOD_MONTAGE_PART_MAX_SEC", "28"))
    final_max = float(os.environ.get("SHOOTER_VOD_MONTAGE_MAX_SEC", "55"))
    return min_clips, max_clips, gap, part_max, final_max


def _montage_soft_min_clips(game: str | None = None) -> int:
    """Minimum accepted parts before a montage may ship."""
    ideal = _montage_limits()[0]
    if game in ("pubg", "standoff", "wot"):
        key = {
            "pubg": "PUBG_VOD_MONTAGE_SOFT_MIN_CLIPS",
            "standoff": "STANDOFF_VOD_MONTAGE_SOFT_MIN_CLIPS",
            "wot": "WOT_VOD_MONTAGE_SOFT_MIN_CLIPS",
        }[game]
        raw = os.environ.get(key, "").strip()
        soft = int(raw) if raw else ideal
        if game == "pubg":
            # Owner contract: PUBG montage = minimum 2 fight clips, never a lone peak.
            return max(2, soft, ideal if pubg_quality_strict() else 0)
        if os.environ.get("SHOOTER_VOD_MONTAGE_SHIP_PARTIAL", "0") == "1":
            return max(1, soft)
        return max(ideal, soft)
    if os.environ.get("SHOOTER_VOD_MONTAGE_SHIP_PARTIAL", "0") != "1":
        return ideal
    return max(1, int(os.environ.get("SHOOTER_VOD_MONTAGE_SOFT_MIN_CLIPS", "2")))


def _pubg_fight_segmenter_enabled() -> bool:
    return os.environ.get("PUBG_FIGHT_SEGMENTER", "1") == "1"


def _filter_pubg_montage_rows(
    vod: Path,
    rows: list[dict],
    *,
    max_clips: int,
) -> list[dict]:
    if not rows or not _pubg_fight_segmenter_enabled():
        return rows
    try:
        from pubg_montage_bounds import filter_rows_distinct_fights

        return filter_rows_distinct_fights(vod, rows, max_clips=max_clips)
    except Exception as exc:
        log.warning("pubg fight-window dedup fallback vod=%s: %s", vod.name, exc)
        return rows


def _pick_montage_rows(
    rows: list[dict],
    *,
    min_clips: int,
    max_clips: int,
    gap_sec: float,
    game: str = "",
    vod: Path | None = None,
) -> list[dict]:
    """Pick montage parts — sequential clusters, biased to owner style reference."""
    from vod_montage_cluster import pick_montage_rows

    anchor_peaks: list[float] = []
    if game == "pubg" and vod is not None:
        try:
            from pubg_owner_style import style_reference_peaks

            anchor_peaks = style_reference_peaks(vod)
        except ImportError:
            pass

    picked = pick_montage_rows(
        rows,
        min_clips=min_clips,
        max_clips=max_clips,
        gap_sec=gap_sec,
        anchor_peaks=anchor_peaks,
    )
    if game == "pubg" and vod is not None:
        picked = _filter_pubg_montage_rows(vod, picked, max_clips=max_clips)
    return picked


def _pubg_duration_cap(raw_dur: float, *, single: bool) -> float:
    """Long sustained fights may ship longer; short scraps stay capped."""
    if single:
        return min(float(os.environ.get("PUBG_SINGLE_MAX_SEC", "90")), max(8.0, raw_dur))
    part_max = float(os.environ.get("SHOOTER_VOD_MONTAGE_PART_MAX_SEC", "28"))
    long_max = float(os.environ.get("PUBG_MONTAGE_PART_LONG_MAX_SEC", "45"))
    long_min = float(os.environ.get("PUBG_LONG_FIGHT_MIN_SEC", "20"))
    if raw_dur >= long_min:
        return min(long_max, raw_dur)
    return min(part_max, raw_dur)


def _pubg_single_fallback_enabled() -> bool:
    return os.environ.get("PUBG_VOD_SINGLE_FALLBACK", "1") == "1"


def _pubg_singles_first_enabled() -> bool:
    from pubg_vod_singles_first import pubg_singles_first_enabled

    return pubg_singles_first_enabled()


def _prepare_pubg_row_for_send(row: dict, vod: Path, *, single: bool) -> dict | None:
    """Tight fight bounds + shape gate; None when clip is mostly running/menu."""
    from pubg_clip_shape_gate import validate_clip_fight_shape
    from pubg_montage_bounds import pubg_clip_has_gunfire

    prepared = dict(row)
    clip = _prepare_montage_clip(prepared, vod, part_max=999.0, game="pubg", single=single)
    if clip.get("shape_reject"):
        return None
    peak = float(prepared.get("peak_start", clip.get("peak_start", 0)) or 0)
    start = float(clip.get("start", 0))
    dur = float(clip.get("input_duration", 0))
    report = clip.get("segment_report") if isinstance(clip.get("segment_report"), dict) else {}
    if report:
        ok, reason = validate_clip_fight_shape(start, dur, peak, report)
        if not ok:
            log.warning("pubg send shape reject peak=%.1f: %s", peak, reason)
            return None
    gun_ok, gun_reason = pubg_clip_has_gunfire(vod, start, dur, peak, single=single)
    if not gun_ok:
        log.warning("pubg send gun reject peak=%.1f: %s", peak, gun_reason)
        return None
    sid = segment_id(vod_youtube_id(vod), start)
    prepared["segment_id"] = sid
    prepared["start"] = start
    prepared["peak_start"] = peak
    prepared["clip"] = clip
    return prepared


def _prepare_montage_clip(
    row: dict,
    vod: Path,
    *,
    part_max: float,
    game: str = "",
    single: bool = False,
) -> dict:
    """Peak-center montage parts on the fight sustain — not a fixed 14s window.

    Default: variable length from gunfire bins (SHOOTER_VOD_VARIABLE_LENGTH=1).
    Falls back to fixed core+pad when analysis unavailable or env disabled.
    """
    clip = dict(row.get("clip") or {})
    if clip.get("bounds_locked"):
        return clip
    start_hint = float(row.get("start", clip.get("start", 0)) or 0)
    peak = float(row.get("peak_start", clip.get("peak_start", start_hint)) or start_hint)
    core = float(os.environ.get("SHOOTER_VOD_MONTAGE_GATE_CORE_SEC", "10"))
    pad = float(os.environ.get("SHOOTER_VOD_MONTAGE_CORE_PAD_SEC", "2"))
    fixed_want = min(
        part_max,
        float(os.environ.get("SHOOTER_VOD_MONTAGE_PART_SEC", str(core + pad * 2))),
        max(12.0, core + pad * 2),
    )

    start = max(0.0, peak - fixed_want * 0.5)
    dur = fixed_want
    fight_end = start + dur

    file_dur = _ffprobe_duration(vod)
    segment_report: dict = {}
    use_pubg_segmenter = (
        game == "pubg" and os.environ.get("PUBG_FIGHT_SEGMENTER", "1") == "1"
    )
    if not use_pubg_segmenter:
        try:
            from shooter_fight_segment import (
                detect_shooter_fight_bounds,
                variable_length_enabled,
            )

            if variable_length_enabled():
                f_start, f_end, f_dur = detect_shooter_fight_bounds(
                    vod,
                    peak,
                    part_cap=part_max,
                )
                if f_dur >= max(10.0, fixed_want * 0.85):
                    start = f_start
                    dur = min(part_max, f_dur)
                    fight_end = min(f_end, start + dur)
                    dur = max(10.0, fight_end - start)
        except Exception:
            pass
    if use_pubg_segmenter:
        try:
            from pubg_fight_segment import resolve_pubg_fight_bounds

            start, dur, segment_report = resolve_pubg_fight_bounds(
                vod,
                peak,
                file_duration=file_dur,
            )
            cap = _pubg_duration_cap(float(dur), single=single)
            dur = min(float(dur), cap, part_max if not single else cap)
            try:
                from pubg_montage_bounds import tighten_pubg_clip_bounds

                peak_val = float(row.get("peak_start", peak) or peak)
                start, dur = tighten_pubg_clip_bounds(
                    start, dur, segment_report, peak=peak_val, single=single
                )
                dur = min(float(dur), _pubg_duration_cap(float(dur), single=single))
                from pubg_clip_shape_gate import validate_clip_fight_shape

                ok_shape, shape_reason = validate_clip_fight_shape(
                    start, dur, peak_val, segment_report
                )
                if not ok_shape:
                    log.warning(
                        "pubg clip shape reject peak=%.1f: %s — drop part",
                        peak_val,
                        shape_reason,
                    )
                    clip["shape_reject"] = shape_reason
            except Exception:
                pass
            if segment_report:
                clip["segment_report"] = segment_report
        except Exception as exc:
            log.warning("pubg fight segmenter fallback peak=%.1f: %s", peak, exc)
    if file_dur > 1.0 and start + dur > file_dur:
        start = max(0.0, file_dur - dur)
        dur = max(8.0, file_dur - start)
    clip.update(
        {
            "start": start,
            "peak_start": peak,
            "fight_end": round(start + dur, 2),
            "input_duration": round(dur, 2),
            "output_duration": round(dur, 2),
        }
    )
    if segment_report:
        clip["segment_report"] = segment_report
    return clip


def _validate_montage_final(
    game: str,
    vod: Path,
    accepted_rows: list[dict],
    *,
    report: dict | None = None,
) -> tuple[bool, str]:
    """Extra quality pass after parts passed presend — strict PUBG only."""
    if game != "pubg" or not pubg_quality_strict():
        return True, "skip"
    if _pubg_score_mode_ready():
        threshold = float(os.environ.get("PUBG_QUALITY_SCORE_MIN", "0.48"))
        for row in accepted_rows:
            quality = row.get("quality_report") or {}
            score = float(quality.get("quality_score", 0.0))
            if quality.get("hard_reject") or score < threshold:
                if report is not None:
                    report.setdefault("rejected_sids", []).append(
                        str(row.get("segment_id") or "")
                    )
                return False, f"final_quality={score:.3f}:min{threshold:.2f}"
        return True, "montage_final_score_ok"
    profile = _profile(game)
    for row in accepted_rows:
        clip = row.get("clip") if isinstance(row.get("clip"), dict) else {}
        start = float(row.get("start", clip.get("start", 0)) or 0)
        dur = float(clip.get("output_duration") or clip.get("input_duration") or 14)
        peak = float(row.get("peak_start", start))
        core = float(os.environ.get("SHOOTER_VOD_MONTAGE_GATE_CORE_SEC", "10"))
        gate_start = max(0.0, peak - core * 0.5)
        gate_dur = min(dur, core)
        ok_metro, metro_reason = True, "skip"
        if os.environ.get("PUBG_METRO_GATE", "0") == "1":
            from pubg_metro_royale_gate import segment_looks_metro_royale

            ok_metro, metro_reason = segment_looks_metro_royale(vod, gate_start, gate_dur)
        if not ok_metro:
            if report is not None:
                report.setdefault("rejected_sids", []).append(str(row.get("segment_id") or ""))
            return False, f"final_metro_{metro_reason}"
        ok, reason, _ = pubg_passes_combat_gate(vod, gate_start, gate_dur, profile)
        if not ok:
            if report is not None:
                report.setdefault("rejected_sids", []).append(str(row.get("segment_id") or ""))
            return False, f"final_combat_{reason}"
        from shooter_author_kill_gate import author_kill_window_ok

        kill_ok, kill_reason, _ = author_kill_window_ok(
            vod, gate_start, gate_dur, profile=game, shoot_metrics={}
        )
        if not kill_ok:
            if report is not None:
                report.setdefault("rejected_sids", []).append(str(row.get("segment_id") or ""))
            return False, f"final_kill_{kill_reason}"
    return True, "montage_final_ok"


def _send_montage(
    game: str,
    token: str,
    chat_id: str,
    vod: Path,
    rows: list[dict],
    sig: str,
    *,
    report: dict | None = None,
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
    soft_min = _montage_soft_min_clips(game)
    try_parts = _montage_try_parts(max_clips, soft_min)
    # Generous per-fight ceiling (2-part budget) — full sustain; trim montage if 3 won't fit.
    part_ceiling = min(part_max, _montage_part_budget(soft_min, final_max))
    log.info(
        "montage start game=%s try_up_to=%s soft_min=%s ceiling=%.1fs/part final_max=%.0fs rows=%s",
        game,
        try_parts,
        soft_min,
        part_ceiling,
        final_max,
        len(rows),
    )
    # If few peaks, retry with tighter spacing before giving up (still need distinct fights).
    picked = _pick_montage_rows(
        rows, min_clips=min_clips, max_clips=max_clips, gap_sec=gap_sec, game=game, vod=vod
    )
    if len(picked) < min_clips:
        tight = max(18.0, gap_sec * 0.45)
        picked = _pick_montage_rows(
            rows, min_clips=min_clips, max_clips=max_clips, gap_sec=tight, game=game, vod=vod
        )
        if len(picked) >= min_clips:
            log.info("montage tight-gap ok game=%s gap=%.0f→%.0f peaks=%s", game, gap_sec, tight, len(picked))
            gap_sec = tight
    if len(picked) < soft_min:
        log.warning(
            "montage insufficient peaks game=%s have=%s need=%s soft=%s rows=%s",
            game,
            len(picked),
            min_clips,
            soft_min,
            len(rows),
        )
        return 0
    if len(picked) < min_clips:
        log.warning(
            "montage soft peaks game=%s have=%s wanted=%s — try partial",
            game,
            len(picked),
            min_clips,
        )

    # Fast discovery (PANNs) + quality: CLIP ranks only this shortlist under a budget.
    prev_clip_disabled = os.environ.get("HIGHLIGHT_CLIP_DISABLED")
    os.environ["HIGHLIGHT_CLIP_DISABLED"] = "0"
    try:
        from highlight_scorer import rank_shortlist_with_clip

        picked = rank_shortlist_with_clip(vod, picked, _profile(game))
    except Exception as exc:
        if game == "pubg" and pubg_quality_strict():
            log.warning("montage CLIP rank required but failed: %s", exc)
            return 0
        log.warning("montage CLIP rank skipped: %s", exc)
    finally:
        if prev_clip_disabled is None:
            os.environ.pop("HIGHLIGHT_CLIP_DISABLED", None)
        else:
            os.environ["HIGHLIGHT_CLIP_DISABLED"] = prev_clip_disabled

    seg_root = _paths(game)["segments"]
    seg_root.mkdir(parents=True, exist_ok=True)
    max_attempts = max(1, int(os.environ.get("SHOOTER_VOD_MONTAGE_SHORTLIST_TRIES", "6")))
    rejected_sids: set[str] = set()
    remaining = list(picked)
    if len(rows) > len(picked):
        seen = {str(r.get("segment_id") or "") for r in remaining}
        for row in rows:
            sid = str(row.get("segment_id") or "")
            if sid and sid not in seen:
                remaining.append(row)
                seen.add(sid)

    for attempt in range(max_attempts):
        # Soft/partial ship: do not bail just because we have < ideal ×3 peaks.
        if len(remaining) < soft_min:
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
                clip = _prepare_montage_clip(
                    row,
                    vod,
                    part_max=part_ceiling,
                    game=game,
                )
                if clip.get("shape_reject"):
                    log.warning(
                        "montage part shape reject idx=%s sid=%s reason=%s",
                        idx,
                        sid,
                        clip.get("shape_reject"),
                    )
                    rejected_sids.add(sid)
                    continue
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
                    if report is not None:
                        report.setdefault("rejected_sids", []).append(sid)
                    part.unlink(missing_ok=True)
                    rejected_sids.add(sid)
                    continue
                work_row["quality_report"] = dict(_report or {})
                work_row["score"] = max(
                    float(work_row.get("score", 0.0)),
                    float(work_row["quality_report"].get("quality_score", 0.0)),
                )
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
                if (
                    os.environ.get("SHOOTER_VOD_MONTAGE_EARLY_SHIP", "0") == "1"
                    and len(segment_paths) >= soft_min
                ):
                    break

            need_parts = min_clips if (game == "pubg" and pubg_quality_strict()) else soft_min
            if len(segment_paths) < need_parts:
                log.warning(
                    "montage after-presend insufficient game=%s parts=%s soft_need=%s attempt=%s",
                    game,
                    len(segment_paths),
                    need_parts,
                    attempt + 1,
                )
                remaining = [r for r in remaining if str(r.get("segment_id") or "") not in rejected_sids]
                if not remaining:
                    remaining = [
                        r
                        for r in rows
                        if str(r.get("segment_id") or "") not in rejected_sids
                    ]
                    remaining = _pick_montage_rows(
                        remaining,
                        min_clips=soft_min,
                        max_clips=max_clips,
                        gap_sec=gap_sec,
                        game=game,
                        vod=vod,
                    )
                continue

            if game == "pubg" and pubg_quality_strict() and len(segment_paths) < min_clips:
                log.warning(
                    "montage strict need ideal parts game=%s have=%s need=%s",
                    game,
                    len(segment_paths),
                    min_clips,
                )
                remaining = [r for r in remaining if str(r.get("segment_id") or "") not in rejected_sids]
                continue

            if len(segment_paths) < min_clips:
                log.warning(
                    "montage shipping partial game=%s parts=%s wanted=%s attempt=%s",
                    game,
                    len(segment_paths),
                    min_clips,
                    attempt + 1,
                )

            ordered = sorted(
                zip(accepted_rows, segment_paths, durations),
                key=lambda t: float(t[0].get("peak_start", t[0].get("start", 0))),
            )
            accepted_rows = [t[0] for t in ordered]
            segment_paths = [t[1] for t in ordered]
            durations = [t[2] for t in ordered]

            while len(segment_paths) > soft_min:
                est = sum(durations) - _montage_xfade_sec() * (len(segment_paths) - 1)
                if est <= final_max:
                    break
                # Drop weakest fight — keep the more interesting peaks in the montage.
                scores = [float(r.get("score", 0) or 0) for r in accepted_rows]
                drop_idx = scores.index(min(scores))
                log.info(
                    "montage trim over budget game=%s est=%.1fs max=%.0f drop_idx=%s score=%.3f",
                    game,
                    est,
                    final_max,
                    drop_idx,
                    scores[drop_idx],
                )
                segment_paths.pop(drop_idx)
                durations.pop(drop_idx)
                accepted_rows.pop(drop_idx)

            montage_id = f"{vod_youtube_id(vod)}_mtg_{int(time.time())}"
            out = seg_root / f"montage_{montage_id}.mp4"
            if len(segment_paths) == 1:
                if game == "pubg":
                    log.warning("montage forbid single-part ship game=%s need>=2", game)
                    continue
                shutil.copy2(segment_paths[0], out)
            else:
                run_command(build_xfade_command(segment_paths, durations, out))
            final_dur = _ffprobe_duration(out)
            min_final = float(os.environ.get("SHOOTER_VOD_MONTAGE_MIN_FINAL_SEC", "18"))
            if game == "pubg" and pubg_quality_strict():
                pubg_min = float(os.environ.get("PUBG_VOD_MONTAGE_MIN_FINAL_SEC", "35"))
                if len(segment_paths) >= 2:
                    # Use rendered part lengths — part_ceiling×N heuristic exceeded final_max (55s).
                    min_final = max(pubg_min, sum(durations) * 0.78)
                else:
                    min_final = max(min_final, pubg_min)
            elif game == "pubg" and len(segment_paths) >= 2:
                min_final = max(
                    float(os.environ.get("PUBG_VOD_MONTAGE_MIN_FINAL_SEC", "35")),
                    sum(durations) * 0.85,
                )
            if len(segment_paths) == 1:
                min_final = float(os.environ.get("SHOOTER_VOD_MONTAGE_MIN_PARTIAL_SEC", "10"))
            # Never require longer than the montage cap (3×part heuristic could exceed final_max).
            min_final = min(min_final, final_max - 0.25)
            if final_dur + 0.35 < min_final:
                log.warning("montage too short game=%s dur=%.1f need>=%.0f", game, final_dur, min_final)
                out.unlink(missing_ok=True)
                continue
            final_report: dict = {}
            ok_final, final_reason = _validate_montage_final(
                game,
                vod,
                accepted_rows,
                report=final_report,
            )
            if not ok_final:
                log.warning("montage final reject game=%s reason=%s", game, final_reason)
                final_rejected = {
                    str(sid)
                    for sid in final_report.get("rejected_sids", [])
                    if str(sid)
                }
                # A bad accepted part used to be validated six times in the
                # same call. Drop only the row that failed and try a replacement.
                rejected_sids.update(final_rejected)
                if report is not None:
                    report.setdefault("rejected_sids", []).extend(sorted(final_rejected))
                remaining = [
                    row
                    for row in remaining
                    if str(row.get("segment_id") or "") not in rejected_sids
                ]
                out.unlink(missing_ok=True)
                continue

            peaks = ",".join(
                str(peak_label_sec(float(r.get("peak_start", r["start"]))))
                for r in accepted_rows
            )
            caption = (
                f"{game.upper()} склейка ×{len(accepted_rows)} · {final_dur:.0f}s\n"
                f"{vod_youtube_id(vod)} peaks {peaks}\n"
                f"Оцените каждый фрагмент 👍/👎"
            )
            primary_sid = accepted_rows[0]["segment_id"]
            part_markup = keyboard_for_parts(game, accepted_rows)
            sent_ok = send_video(
                token,
                chat_id,
                out,
                caption,
                seg_id=primary_sid,
                record_learning=False,
                reply_markup=part_markup,
                cycle_game=game,
            )
            if not sent_ok:
                time.sleep(2.0)
                sent_ok = send_video(
                    token,
                    chat_id,
                    out,
                    caption,
                    seg_id=primary_sid,
                    record_learning=False,
                    reply_markup=part_markup,
                    cycle_game=game,
                )
            if not sent_ok:
                size = out.stat().st_size if out.is_file() else 0
                log.error("montage telegram send failed game=%s file=%s bytes=%s", game, out.name, size)
                return 0

            seg_root = _paths(game)["segments"]
            seg_root.mkdir(parents=True, exist_ok=True)
            for row, part_path in zip(accepted_rows, segment_paths):
                sid = row["segment_id"]
                part_dur = _ffprobe_duration(part_path)
                hq_path = seg_root / f"seg_{sid}.mp4"
                shutil.copy2(part_path, hq_path)
                upsert_segment(
                    game,
                    {
                        "segment_id": sid,
                        "path": str(hq_path),
                        "vod": str(vod),
                        "vod_id": vod_youtube_id(vod),
                        "start": row["start"],
                        "duration": part_dur,
                        "peak_start": row.get("peak_start", row["start"]),
                        "score": row.get("score", 0),
                        "quality_metrics": row.get("quality_report", {}),
                        "segment_report": (row.get("clip") or {}).get("segment_report", {}),
                        "sig": sig,
                        "montage_id": montage_id,
                        "montage_combined_path": str(out),
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


def _game_lead_sec(game: str) -> float:
    g = game.strip().lower()
    if g == "genshin":
        return float(os.environ.get("GENSHIN_VOD_LEAD_SEC", os.environ.get("MLBB_VOD_LEAD_SEC", "2")))
    if g == "wot":
        return float(os.environ.get("WOT_VOD_LEAD_SEC", os.environ.get("MLBB_VOD_LEAD_SEC", "4")))
    if g == "pubg":
        return float(os.environ.get("PUBG_VOD_LEAD_SEC", os.environ.get("MLBB_VOD_LEAD_SEC", "4")))
    if g == "standoff":
        return float(os.environ.get("STANDOFF_VOD_LEAD_SEC", os.environ.get("MLBB_VOD_LEAD_SEC", "4")))
    return float(os.environ.get("MLBB_VOD_LEAD_SEC", "4"))


def _send_batch(
    game: str,
    token: str,
    chat_id: str,
    vod: Path,
    to_send: list[dict],
    sig: str,
    *,
    skip_montage: bool = False,
    reply_markup: dict | None = None,
    singles_final: bool = False,
) -> int:
    soft_min = _montage_soft_min_clips(game)
    if not skip_montage and _montage_enabled(game) and len(to_send) >= soft_min:
        n = _send_montage(game, token, chat_id, vod, to_send, sig)
        if n > 0:
            return n
    pubg_single = (
        game == "pubg"
        and len(to_send) == 1
        and (_pubg_single_fallback_enabled() or skip_montage or _pubg_singles_first_enabled())
    )
    if pubg_single:
        prepared = _prepare_pubg_row_for_send(to_send[0], vod, single=True)
        if prepared is None:
            log.warning(
                "pubg single fallback rejected peak=%.1f",
                float(to_send[0].get("peak_start", 0) or 0),
            )
            return 0
        to_send = [prepared]
    elif _montage_only(game):
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
        prev_clip_disabled = os.environ.get("HIGHLIGHT_CLIP_DISABLED")
        os.environ["HIGHLIGHT_CLIP_DISABLED"] = "0"
        try:
            from highlight_scorer import rank_shortlist_with_clip

            to_send_ranked = rank_shortlist_with_clip(
                vod, to_send[:6], _profile(game), max_n=min(6, len(to_send))
            )
        except Exception as exc:
            log.warning("single CLIP rank skipped: %s", exc)
            to_send_ranked = list(to_send)
        finally:
            if prev_clip_disabled is None:
                os.environ.pop("HIGHLIGHT_CLIP_DISABLED", None)
            else:
                os.environ["HIGHLIGHT_CLIP_DISABLED"] = prev_clip_disabled
    for row in to_send_ranked[:1]:
        sid = row["segment_id"]
        clip = dict(row.get("clip") or {})
        peak = float(row.get("peak_start", row.get("start", clip.get("start", 0))) or 0)
        # Owner rule: Genshin clip = full boss fight (HP bar start → fight end).
        if game == "genshin" and os.environ.get("GENSHIN_BOSS_FULL_FIGHT", "1") == "1":
            try:
                from genshin_boss_segment import apply_genshin_full_fight_clip

                clip = apply_genshin_full_fight_clip(vod, clip, peak_sec=peak)
                row = {
                    **row,
                    "clip": clip,
                    "start": float(clip["start"]),
                    "peak_start": peak,
                    "segment_id": segment_id(vod_youtube_id(vod), float(clip["start"])),
                }
                sid = row["segment_id"]
                log.info(
                    "genshin full-fight clip vod=%s peak=%.1f start=%.1f dur=%.1fs end=%.1f",
                    vod.name,
                    peak,
                    float(clip["start"]),
                    float(clip["input_duration"]),
                    float(clip.get("fight_end") or 0),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("genshin full-fight expand failed: %s — keep peak window", exc)
        out = seg_root / f"seg_{sid}.mp4"
        if not render_single_segment(vod, clip, out):
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
        presend_ok, presend_reason, presend_report = _validate_shooter_presend(
            game, vod, row, out, single=pubg_single
        )
        if not presend_ok:
            log.warning("presend REJECT %s: %s", sid, presend_reason)
            continue
        peak = int(row.get("peak_start", row["start"]))
        out_dur = _ffprobe_duration(out)
        caption = (
            f"{game.upper()} Metro Royale #{sid}\n"
            f"{vod_youtube_id(vod)} @ {int(row['start'])}s (пик {peak}s, {out_dur:.0f}s)\n"
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
            reply_markup=reply_markup or keyboard(game, sid),
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
                    "singles_final": bool(singles_final),
                },
            )
            mark_feed_sent(game, [sid])
            sent += 1
    st = stats(game)
    if sent:
        send_message(token, chat_id, f"✅ {game.upper()} sent={sent} | 👍{st['feedback_yes']} 👎{st['feedback_no']}")
    return sent


def _inbox_order_key(
    mp4: Path,
    registry: list[dict],
    *,
    game: str = "pubg",
    state: dict | None = None,
) -> tuple:
    """Pool-ready VODs first (skip re-probe); else long combat; zombie blocked last."""
    from pubg_metro_royale_gate import title_metro_hint
    from youtube_game_prefs import russian_score

    singles_pin = 1
    if game == "pubg" and state is not None:
        from pubg_vod_singles_first import inbox_active_vod_priority

        singles_pin = inbox_active_vod_priority(state, mp4)

    entry = next((r for r in registry if r.get("path") == str(mp4)), None)
    scanned = float((entry or {}).get("last_scan_at") or 0)
    title = str((entry or {}).get("title") or "")
    owner_prio = 0 if vod_has_owner_montage_anchors(game, mp4) else 1
    metro_prio = 0 if title_metro_hint(title) else 1
    ru = russian_score({"title": title, "uploader": str((entry or {}).get("uploader") or "")})
    ru_prio = 0 if ru >= 0.10 else (1 if ru >= 0.05 else 2)
    reject = str((entry or {}).get("reject_reason") or "")
    fast_fail = 1 if reject.startswith("fast_panns_0") else 0
    # Prefer longer usable VODs — short junk burns stall budget without combat.
    try:
        size_prio = -int(mp4.stat().st_size)
    except OSError:
        size_prio = 0
    # Duration beats size when self-heal accidentally recycled short high-bitrate clips.
    dur = float((entry or {}).get("duration") or (entry or {}).get("dur") or 0)
    if dur <= 0:
        dur = _ffprobe_duration(mp4)
    # Tier: 90min+ streams → mid combat → barely-legal → under min.
    long_floor = float(os.environ.get("SHOOTER_VOD_LONG_REDISCOVER_MIN_SEC", "1800"))
    if dur >= long_floor:
        dur_tier = 0
    elif dur >= max(_vod_min_sec(), 600.0):
        dur_tier = 1
    elif dur >= _vod_min_sec():
        dur_tier = 2
    else:
        dur_tier = 3
    # Registry rows with blocked=True but never stamped last_scan_at were retried
    # every run ahead of real long VODs (FpMs/wEmX stuck at rank 30+).
    blocked = bool((entry or {}).get("last_scan_blocked"))
    sent_ok = int((entry or {}).get("last_scan_sent") or 0) > 0
    zombie_blocked = 1 if blocked and scanned <= 0 and not sent_ok else 0
    peaks_n = len((entry or {}).get("last_pool_peaks") or [])
    pool_ready = 0 if peaks_n >= _montage_soft_min_clips(game) else 1
    # Ready: more cached peaks first (almost-montage), then shorter VOD for faster retry.
    dur_sort = int(dur) if pool_ready == 0 else -int(dur)
    return (
        singles_pin,
        pool_ready,
        zombie_blocked,
        1 if scanned else 0,
        -peaks_n if pool_ready == 0 else 0,
        dur_tier if pool_ready else 0,
        dur_sort,
        owner_prio,
        fast_fail,
        metro_prio,
        ru_prio,
        size_prio,
        scanned,
        mp4.stat().st_mtime,
    )


def _scan_vod(
    game: str,
    token: str,
    chat_id: str,
    vod: Path,
    env: dict[str, str],
    *,
    soften_level: int = 0,
    entry: dict | None = None,
    state: dict | None = None,
) -> int:
    profile = _profile(game)
    sig = file_sha256(vod)
    labeled = labeled_ids(game)
    sent_set = load_feed_sent(game)
    vid = vod_youtube_id(vod)
    lead = _game_lead_sec(game)
    seg_gap = segment_gap_sec(game, soften_level=soften_level)
    index_segments = load_index(game).get("segments", [])
    used_peaks = used_peaks_for_vod(game, vid, sent_set, index_segments)
    blocked_ids = labeled | sent_set
    montage = _montage_enabled(game)
    pubg_singles = game == "pubg" and _pubg_singles_first_enabled()
    if pubg_singles:
        montage = False
    min_clips = max(1, int(os.environ.get("SHOOTER_VOD_MONTAGE_MIN_CLIPS", "3"))) if montage else 1
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
    # only do it when forced, short enough, or genshin/wot need fresh boss/brawl peaks.
    if entry and pool_cache_valid(entry) and os.environ.get("SHOOTER_VOD_FORCE_REDISCOVER", "0") != "1":
        if str(entry.get("reject_reason") or "") != "peaks_blocked_rediscover":
            pool = minimal_pool_from_entry(entry)
            log.info("reuse cached peak pool vod=%s peaks=%s", vod.name, len(pool))
        else:
            pool = []
            force_rediscover = True
            log.info("invalidate blocked cache — rediscover vod=%s", vod.name)
    elif long_vod and not force_rediscover and game not in ("genshin", "wot"):
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
            # Genshin/WoT: one blocked seed must not kill a long VOD — force denser
            # rediscover next tick instead of exhausting (owner SLA / full boss fights).
            if game in ("genshin", "wot") and vod_dur >= 400:
                if entry is not None:
                    entry["last_pool_peaks"] = []
                    entry.pop("pool_cache_at", None)
                    record_vod_scan(
                        entry, sent=0, pool_peaks=pool_peaks, blocked=False, pool=[]
                    )
                    entry["reject_reason"] = "peaks_blocked_rediscover"
                    entry["exhausted"] = False
                log.warning(
                    "keep vod for denser rediscover game=%s vod=%s used=%s",
                    game,
                    vod.name,
                    used_peaks[:8],
                )
                return 0
            if entry is not None:
                record_vod_scan(entry, sent=0, pool_peaks=pool_peaks, blocked=blocked, pool=pool)
            return 0
        rows.sort(key=lambda r: float(r.get("score", 0)), reverse=True)
        if pubg_singles:
            from pubg_vod_singles_first import (
                clear_active_vod,
                pick_next_single_row,
                set_active_vod,
                singles_keyboard,
            )

            row, is_final = pick_next_single_row(
                rows,
                blocked_ids=blocked_ids,
                rejected_peaks=[],
                gap_sec=cand_gap,
                used_peaks=used_peaks,
                peak_too_close=_peak_too_close,
            )
            if row is None:
                if state is not None:
                    clear_active_vod(state, reason="pubg_singles_exhausted")
                if entry is not None:
                    record_vod_scan(entry, sent=0, pool_peaks=pool_peaks, blocked=True, pool=pool)
                    entry["exhausted"] = True
                    entry["reject_reason"] = "pubg_singles_exhausted"
                return 0
            if state is not None:
                set_active_vod(state, vid)
            markup = singles_keyboard(
                game,
                str(row["segment_id"]),
                vid,
                show_assemble=is_final,
            )
            n = _send_batch(
                game,
                token,
                chat_id,
                vod,
                [row],
                sig,
                skip_montage=True,
                reply_markup=markup,
                singles_final=is_final,
            )
            if n > 0:
                if entry is not None:
                    record_vod_scan(entry, sent=n, pool_peaks=pool_peaks, blocked=False, pool=pool)
                    if is_final:
                        entry["exhausted"] = True
                        entry["reject_reason"] = "pubg_singles_complete"
                        if state is not None:
                            clear_active_vod(state, reason="pubg_singles_complete")
                return n
            skip_peaks.add(round(float(row.get("peak_start", row["start"])), 1))
            peak_tries += 1
            continue
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
        inbox_trust = os.environ.get("PUBG_METRO_INBOX_TRUST", "1") == "1"
        if inbox_trust and str(vod).find("/inbox/") >= 0:
            ok_metro, metro_reason = True, "inbox_trusted"
        else:
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
        # VOD passed metro — do not re-reject montage parts with stricter segment probes.
        os.environ["PUBG_METRO_SEGMENT_TRUST_VOD"] = "1"

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
            dense_on_miss = _dense_on_fast_probe_miss(game)
            if dense_on_miss:
                # Sparse probes are only a cost optimization, not evidence that
                # the whole VOD has no fights. Always let the timeline-wide
                # dense montage scan make the final decision.
                log.info(
                    "fast-probe miss vod=%s reason=%s — continue with dense montage",
                    vod.name,
                    fast_reason,
                )
                seed_peaks = []
                ok_fast = True
            # Owner anchors alone must NOT keep a dead VOD alive forever —
            # that spun every idle tick with zero Telegram and paid CPU.
            # Still try dense montage once when anchors exist; otherwise exhaust.
            has_anchors = vod_has_owner_montage_anchors(game, vod)
            if not ok_fast and not has_anchors:
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
            if not ok_fast:
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
                from shooter_vod_fast_scan import (
                    candidate_pool_target,
                    discover_montage_gun_peaks,
                )

                min_clips, _max_c, gap_sec, part_max, _final = _montage_limits()
                cache_min = (
                    1
                    if game == "pubg" and _pubg_singles_first_enabled()
                    else min_clips
                )
                scan_funnel: ScanFunnel | None = None
                cached_peaks = peak_values_from_entry(entry)
                pool_target = candidate_pool_target(min_clips)
                rejected_peaks = _dense_rejected_peaks(entry)
                pool_hit = None
                try:
                    from vod_ranked_pool_cache import get_ranked_pool

                    pool_hit = get_ranked_pool(vod)
                    if pool_hit and not cached_peaks:
                        cached_peaks = [float(p) for p in pool_hit.get("ranked_peaks") or []]
                except Exception:
                    pass
                if _dense_pool_cache_usable(entry, cached_peaks, cache_min):
                    dense_peaks = cached_peaks
                    dense_reason = f"cached_pool_{len(cached_peaks)}"
                    scan_funnel = ScanFunnel()
                    scan_funnel.picked = len(cached_peaks)
                    scan_funnel.feature_cache_hit = True
                    if pool_hit:
                        scan_funnel.ranked_pool_cache_hit = True
                    log.info(
                        "fast-montage reuse cached peaks vod=%s n=%s",
                        vod.name,
                        len(cached_peaks),
                    )
                else:
                    probe_pass = _dense_probe_pass_index(entry)
                    scan_funnel = ScanFunnel()
                    dense_peaks, dense_reason = discover_montage_gun_peaks(
                        vod,
                        _profile(game),
                        min_clips=min_clips,
                        gap_sec=gap_sec,
                        probe_pass=probe_pass,
                        funnel=scan_funnel,
                    )
                    if entry is not None:
                        entry["dense_pool_version"] = DENSE_POOL_VERSION
                    log.info(
                        "fast-montage refreshed pool vod=%s candidates=%s target=%s pass=%s",
                        vod.name,
                        len(dense_peaks),
                        pool_target,
                        probe_pass,
                    )
                    scan_funnel.mark("discovery_done")
                # Owner-good fight times first (gold labels) — PANNs alone often
                # picks cruise SFX that fail impact/flash gates.
                try:
                    owner_peaks = owner_good_fight_peaks(game, vod)
                except Exception:
                    owner_peaks = []
                if owner_peaks:
                    merged: list[float] = []
                    for t in list(owner_peaks) + list(dense_peaks or []):
                        ft = float(t)
                        if any(abs(ft - x) < max(12.0, gap_sec * 0.4) for x in merged):
                            continue
                        merged.append(ft)
                    dense_peaks = merged
                    dense_reason = f"owner+{dense_reason}"
                    log.info(
                        "fast-montage owner peaks game=%s vod=%s n=%s first=%s",
                        game,
                        vod.name,
                        len(owner_peaks),
                        owner_peaks[:6],
                    )
                try:
                    from vod_event_dedup import dedup_by_audio_signature, merge_nearby_peaks

                    dense_peaks = merge_nearby_peaks(dense_peaks or [])
                    dense_peaks = dedup_by_audio_signature(vod, dense_peaks)
                except Exception:
                    pass
                style_sims: dict[float, float] = {}
                peak_meta: dict[float, dict] = {}
                if game == "pubg" and dense_peaks:
                    try:
                        from pubg_fast_peak_rank import rank_peaks_fast

                        dense_peaks, fast_reason, peak_meta = rank_peaks_fast(
                            vod,
                            dense_peaks,
                            _profile(game),
                            part_sec=part_max,
                        )
                        scan_funnel.note_stage("fast_rank", len(dense_peaks))
                        dense_reason = f"{dense_reason} {fast_reason}"
                    except Exception as exc:
                        log.warning("fast peak rank fallback vod=%s: %s", vod.name, exc)
                    try:
                        from vod_scan_cascade import apply_cascade_to_pool

                        dense_peaks = apply_cascade_to_pool(dense_peaks, "fast_ranker")
                        scan_funnel.fast_ranker_pass = len(dense_peaks)
                    except Exception:
                        pass
                    from pubg_killfeed_ocr import rank_peaks_by_killfeed

                    dense_peaks, kf_reason = rank_peaks_by_killfeed(
                        vod,
                        dense_peaks,
                        _profile(game),
                        part_sec=part_max,
                        meta=peak_meta,
                    )
                    dense_reason = f"{dense_reason} {kf_reason}"
                    try:
                        from vod_scan_cascade import apply_cascade_to_pool

                        dense_peaks = apply_cascade_to_pool(dense_peaks, "kill")
                        scan_funnel.kill_pass = len(dense_peaks)
                    except Exception:
                        pass
                    try:
                        from pubg_moment_ranker import rank_peaks_with_model

                        dense_peaks, ranker_reason = rank_peaks_with_model(
                            vod,
                            dense_peaks,
                            part_sec=min(14.0, part_max),
                        )
                        dense_reason = f"{dense_reason} {ranker_reason}"
                    except Exception as exc:
                        log.warning("pubg ranker fallback vod=%s: %s", vod.name, exc)
                    try:
                        from pubg_owner_style import rank_peaks_by_style

                        dense_peaks, style_reason, style_sims = rank_peaks_by_style(
                            vod,
                            dense_peaks,
                            part_sec=part_max,
                            meta=peak_meta,
                        )
                        dense_reason = f"{dense_reason} {style_reason}"
                    except Exception as exc:
                        log.warning("style rank fallback vod=%s: %s", vod.name, exc)
                try:
                    from vod_ranked_pool_cache import put_ranked_pool

                    put_ranked_pool(
                        vod,
                        ranked_peaks=dense_peaks or [],
                        reason=dense_reason,
                        funnel=scan_funnel.to_dict() if scan_funnel else None,
                    )
                except Exception:
                    pass
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
                soft_min = _montage_soft_min_clips(game)
                max_montages = montages_per_vod(game)
                total_sent = 0
                presend_reject = False
                pubg_avoid_peaks: list[float] = []
                if game == "pubg":
                    try:
                        from pubg_owner_style import style_avoid_peaks

                        pubg_avoid_peaks = style_avoid_peaks(vod)
                    except Exception:
                        pubg_avoid_peaks = []

                def _build_rows(
                    peak_gap: float,
                    *,
                    used: list[float],
                    blocked: set[str],
                ) -> list[dict]:
                    out_rows: list[dict] = []
                    pubg_bounds = game == "pubg" and _pubg_fight_segmenter_enabled()
                    for idx, peak in enumerate(dense_peaks):
                        if any(abs(float(peak) - bad) <= 4.0 for bad in rejected_peaks):
                            continue
                        if pubg_avoid_peaks and any(
                            abs(float(peak) - float(bad)) <= 25.0 for bad in pubg_avoid_peaks
                        ):
                            continue
                        report = None
                        if pubg_bounds:
                            try:
                                from pubg_montage_bounds import (
                                    fight_bounds,
                                    peak_blocked_by_used_fights,
                                    peak_fight_report,
                                    tighten_pubg_clip_bounds,
                                )

                                if peak_blocked_by_used_fights(
                                    vod, float(peak), used, peak_gap_sec=peak_gap
                                ):
                                    continue
                                report = peak_fight_report(vod, float(peak))
                                window_start, window_end = fight_bounds(vod, float(peak))
                                start, clip_dur = tighten_pubg_clip_bounds(
                                    window_start,
                                    window_end - window_start,
                                    report,
                                    peak=float(peak),
                                )
                                from pubg_clip_shape_gate import validate_clip_fight_shape

                                ok_shape, shape_reason = validate_clip_fight_shape(
                                    start, clip_dur, float(peak), report
                                )
                                if not ok_shape:
                                    continue
                            except Exception:
                                if _peak_too_close(float(peak), used, peak_gap):
                                    continue
                                start = max(0.0, float(peak) - part_sec * 0.5)
                                clip_dur = part_sec
                        elif _peak_too_close(float(peak), used, peak_gap):
                            continue
                        else:
                            start = max(0.0, float(peak) - part_sec * 0.5)
                            clip_dur = part_sec
                        sid = segment_id(vid, start)
                        if sid in blocked:
                            continue
                        style_match = style_sims.get(float(peak))
                        score = max(0.2, 0.95 - idx * 0.03)
                        if style_match is not None:
                            score = max(score, 0.30 + float(style_match) * 0.65)
                        row = {
                            "segment_id": sid,
                            "start": start,
                            "peak_start": float(peak),
                            "score": score,
                            "clip": {
                                "start": start,
                                "peak_start": float(peak),
                                "input_duration": clip_dur,
                                "output_duration": clip_dur,
                            },
                        }
                        if report is not None:
                            row["clip"]["segment_report"] = report
                        if style_match is not None:
                            row["style_sim"] = float(style_match)
                        out_rows.append(row)
                    return out_rows

                def _shortlist_rows(used: list[float], blocked: set[str]) -> list[dict]:
                    rows = _build_rows(gap_sec * 0.9, used=used, blocked=blocked)
                    if len(rows) < soft_min:
                        tight = max(18.0, gap_sec * 0.45)
                        rows = _build_rows(tight, used=used, blocked=blocked)
                        if len(rows) >= soft_min:
                            log.info(
                                "fast-montage tight unused-gap vod=%s gap=%.0f→%.0f rows=%s",
                                vod.name,
                                gap_sec,
                                tight,
                                len(rows),
                            )
                    if len(rows) < soft_min:
                        ultra = max(12.0, gap_sec * 0.28)
                        rows = _build_rows(ultra, used=used, blocked=blocked)
                        if len(rows) >= soft_min:
                            log.info(
                                "fast-montage ultra unused-gap vod=%s gap→%.0f rows=%s",
                                vod.name,
                                ultra,
                                len(rows),
                            )
                    if len(dense_peaks or []) >= soft_min:
                        expanded = _build_rows(max(6.0, gap_sec * 0.12), used=used, blocked=blocked)
                        if len(expanded) > len(rows):
                            log.info(
                                "fast-montage expand pool vod=%s rows=%s→%s peaks=%s",
                                vod.name,
                                len(rows),
                                len(expanded),
                                len(dense_peaks),
                            )
                            rows = expanded
                    return rows

                if game == "pubg" and _pubg_singles_first_enabled():
                    from pubg_vod_singles_first import singles_first_send_cycle

                    sent_set = load_feed_sent(game)
                    used_peaks = _used_peak_times(game, vid, sent_set)
                    blocked_ids = labeled_ids(game) | sent_set
                    all_rows = _build_rows(max(12.0, gap_sec * 0.9), used=used_peaks, blocked=blocked_ids)
                    n_sf = singles_first_send_cycle(
                        game=game,
                        token=token,
                        chat_id=chat_id,
                        vod=vod,
                        vid=vid,
                        state=state,
                        entry=entry,
                        rows=all_rows,
                        gap_sec=gap_sec,
                        rejected_peaks=rejected_peaks,
                        sig=file_sha256(vod),
                        mark_exhausted_fn=_mark_vod_exhausted,
                        save_state_fn=_save_state,
                        record_scan_fn=record_vod_scan,
                        scan_funnel=scan_funnel,
                    )
                    _save_state(game, state)
                    if clear_fast_seeds:
                        clear_fast_seeds()
                    return n_sf

                for montage_idx in range(max_montages):
                    sent_set = load_feed_sent(game)
                    used_peaks = _used_peak_times(game, vid, sent_set)
                    blocked_ids = labeled_ids(game) | sent_set
                    rows = _shortlist_rows(used_peaks, blocked_ids)
                    if len(rows) < soft_min:
                        break
                    if len(rows) < min_clips:
                        log.warning(
                            "fast-montage soft-shortlist vod=%s rows=%s soft_min=%s wanted=%s idx=%s",
                            vod.name,
                            len(rows),
                            soft_min,
                            min_clips,
                            montage_idx,
                        )
                    send_report: dict = {}
                    n_fast = _send_montage(
                        game,
                        token,
                        chat_id,
                        vod,
                        rows,
                        file_sha256(vod),
                        report=send_report,
                    )
                    rejected_sids = {
                        str(sid)
                        for sid in send_report.get("rejected_sids", [])
                        if str(sid)
                    }
                    if rejected_sids:
                        rejected_now = [
                            float(row["peak_start"])
                            for row in rows
                            if str(row.get("segment_id") or "") in rejected_sids
                        ]
                        _remember_dense_rejections(entry, rejected_now)
                        for peak in rejected_now:
                            if not any(abs(peak - old) <= 4.0 for old in rejected_peaks):
                                rejected_peaks.append(peak)
                    if n_fast > 0:
                        total_sent += n_fast
                        log.info(
                            "fast-montage SENT game=%s vod=%s n=%s idx=%s/%s peaks=%s",
                            game,
                            vod.name,
                            n_fast,
                            montage_idx + 1,
                            max_montages,
                            dense_peaks[:6],
                        )
                        if montage_idx + 1 >= max_montages:
                            break
                        continue
                    presend_reject = True
                    log.warning(
                        "fast-montage rejected by gates vod=%s peaks=%s idx=%s — try next slice",
                        vod.name,
                        len(rows),
                        montage_idx,
                    )
                    continue

                if total_sent > 0:
                    if entry is not None:
                        if scan_funnel is not None:
                            scan_funnel.sent = total_sent
                            scan_funnel.presend_pass = total_sent
                            scan_funnel.mark("sent")
                        record_vod_scan(
                            entry,
                            sent=total_sent,
                            pool_peaks=dense_peaks,
                            blocked=False,
                            funnel=scan_funnel.to_dict() if scan_funnel else None,
                        )
                        entry["dense_pool_version"] = DENSE_POOL_VERSION
                        entry.pop("reject_reason", None)
                        if total_sent < max_montages:
                            _bump_dense_probe_visit(entry)
                    _save_state(game, state)
                    if clear_fast_seeds:
                        clear_fast_seeds()
                    return total_sent

                if presend_reject:
                    sent_set = load_feed_sent(game)
                    used_peaks = _used_peak_times(game, vid, sent_set)
                    remaining_unused = [
                        p
                        for p in dense_peaks
                        if not _peak_too_close(float(p), used_peaks, max(12.0, gap_sec * 0.28))
                    ]
                    if len(remaining_unused) < soft_min:
                        _mark_vod_exhausted(
                            state,
                            vod,
                            reason="fast_montage_presend_reject",
                            delete_file=False,
                        )
                    elif entry is not None:
                        if scan_funnel is not None:
                            scan_funnel.presend_fail += 1
                            scan_funnel.note_reject("fast_montage_presend_reject_retry")
                            scan_funnel.mark("presend_fail")
                        record_vod_scan(
                            entry,
                            sent=0,
                            pool_peaks=dense_peaks,
                            blocked=False,
                            funnel=scan_funnel.to_dict() if scan_funnel else None,
                        )
                        entry["dense_pool_version"] = DENSE_POOL_VERSION
                        entry["reject_reason"] = "fast_montage_presend_reject_retry"
                        _bump_dense_probe_visit(entry)
                    _save_state(game, state)
                    if clear_fast_seeds:
                        clear_fast_seeds()
                    return 0

                sent_set = load_feed_sent(game)
                used_peaks = _used_peak_times(game, vid, sent_set)
                rows = _shortlist_rows(used_peaks, labeled_ids(game) | sent_set)
                vod_dur_fast = _ffprobe_duration(vod)
                long_vod = vod_dur_fast >= float(
                    os.environ.get("SHOOTER_VOD_LONG_REDISCOVER_MIN_SEC", "1800")
                )
                log.warning(
                    "fast-montage insufficient unused peaks vod=%s have=%s need=%s soft=%s used=%s%s",
                    vod.name,
                    len(rows),
                    min_clips,
                    soft_min,
                    len(used_peaks),
                    " — keep long vod" if long_vod else " — exhaust for discovery",
                )
                if game == "pubg" and len(rows) == 1 and _pubg_single_fallback_enabled():
                    n_single = _send_batch(
                        game, token, chat_id, vod, rows, file_sha256(vod)
                    )
                    if n_single > 0:
                        if scan_funnel is not None:
                            scan_funnel.sent = n_single
                            scan_funnel.presend_pass = n_single
                            scan_funnel.mark("sent")
                        record_vod_scan(
                            entry,
                            sent=n_single,
                            pool_peaks=dense_peaks,
                            blocked=False,
                            funnel=scan_funnel.to_dict() if scan_funnel else None,
                        )
                        _save_state(game, state)
                        if clear_fast_seeds:
                            clear_fast_seeds()
                        return n_single
                if long_vod:
                    # 90min+ streams still have fights outside the used gap window;
                    # exhausting them left the feed thrashing 3–5min junk for hours.
                    if entry is not None:
                        entry["exhausted"] = False
                        entry["reject_reason"] = (
                            f"fast_montage_need_{soft_min}_have_{len(rows)}_long_keep"
                        )
                        record_vod_scan(
                            entry, sent=0, pool_peaks=dense_peaks, blocked=True
                        )
                        _bump_dense_probe_visit(entry)
                else:
                    _mark_vod_exhausted(
                        state,
                        vod,
                        reason=f"fast_montage_need_{soft_min}_have_{len(rows)}",
                        delete_file=False,
                    )
                    if entry is not None:
                        record_vod_scan(entry, sent=0, pool_peaks=dense_peaks, blocked=True)
                _save_state(game, state)
                if clear_fast_seeds:
                    clear_fast_seeds()
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

    # Hard anti-hang: montage-only shooters skip CLIP/highlight unless fallback allowed.
    allow_fallback = os.environ.get("SHOOTER_VOD_ALLOW_HIGHLIGHT_FALLBACK", "0") == "1"
    if (
        game in ("pubg", "standoff", "wot")
        and _montage_only(game)
        and os.environ.get("SHOOTER_VOD_FAST_PROBE", "1") == "1"
        and os.environ.get("SHOOTER_VOD_FAST_MONTAGE", "1") == "1"
        and not allow_fallback
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

    if pubg_quality_strict() and game == "pubg":
        try:
            sent = _scan_vod(game, token, chat_id, vod, env, soften_level=0, entry=entry, state=state)
        finally:
            if clear_fast_seeds is not None:
                clear_fast_seeds()
        new_streak = gate.record_vod_outcome(state, vod_id=vid, sent=sent)
        state["last_adaptive_level"] = 0
        _save_state(game, state)
        if sent == 0 and os.environ.get("SHOOTER_VOD_EXHAUST_NOTIFY", os.environ.get("MLBB_VOD_EXHAUST_NOTIFY", "1")) == "1":
            if new_streak % 3 == 0:
                entry = _vod_registry_entry(state, vod)
                send_message(
                    token,
                    chat_id,
                    telegram_exhaust_notice(
                        game,
                        vid,
                        level=0,
                        streak=new_streak,
                        detail=scan_zero_detail(entry),
                    ),
                )
        return sent

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
            sent = _scan_vod(game, token, chat_id, vod, env, soften_level=level, entry=entry, state=state)
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


def _recycle_parked_vod(game: str, state: dict, inbox: Path) -> Path | None:
    """
    When YouTube discovery is 403/empty, pull ONE parked VOD back — with memory.

    Without recycle_count / cooldown this became park→dense-PANNs→exhaust→park
    forever (busy-idle on paid CPU). Never recycle VODs that already failed
    gate/shortlist unless unused peaks remain and attempts remain.
    """
    if os.environ.get("SHOOTER_VOD_RECYCLE_PARKED", "1") != "1":
        return None
    max_recycles = max(1, int(os.environ.get("SHOOTER_VOD_RECYCLE_MAX_PER_VOD", "2")))
    cooldown = float(os.environ.get("SHOOTER_VOD_RECYCLE_COOLDOWN_SEC", "900"))
    dead_reasons = {
        "no_combat_peaks",
        "all_peaks_blocked",
    }
    # With CLIP final-rank enabled, previous montage_fast_path rejects are worth one more try.
    if os.environ.get("SHOOTER_VOD_MONTAGE_CLIP_RANK", "1") != "1":
        dead_reasons.update({"fast_montage_presend_reject", "montage_fast_path_no_send"})
    parked = inbox.parent / "parked"
    if not parked.is_dir():
        return None
    min_sec = _vod_min_sec()
    candidates: list[tuple[float, Path, dict]] = []
    registry = state.setdefault("vods", [])
    now = time.time()
    for mp4 in list(parked.glob("yt_*.mp4")) + list(parked.glob("tw_*.mp4")):
        dur = _ffprobe_duration(mp4)
        if dur < min_sec or not _shooter_vod_length_ok(mp4, dur):
            continue
        vid = vod_youtube_id(mp4)
        entry = next((r for r in registry if str(r.get("id") or "") == vid), None) or {}
        recycles = int(entry.get("recycle_count") or 0)
        # Long VODs (≥15min) get one extra recycle attempt after path upgrades.
        allow = max_recycles + (1 if dur >= 900 else 0)
        if recycles >= allow:
            continue
        last_rec = float(entry.get("last_recycle_at") or 0)
        if last_rec > 0 and (now - last_rec) < cooldown:
            continue
        reason = str(entry.get("reject_reason") or "")
        reason_base = reason.split("=", 1)[0]
        reason_low = reason.lower()
        if any(k in reason_low for k in ("classic_outdoor", "metro_vod_reject", "not_metro", "classic_map")):
            continue
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

    from vod_feed_recover import auto_heal_stalled_feed

    state = _load_state(game)
    heal = auto_heal_stalled_feed(game)
    if heal.get("healed"):
        log.warning("auto-heal game=%s %s", game, heal)
        state = _load_state(game)
    registry = state.setdefault("vods", [])
    used = set(state.get("used_youtube_ids", []))
    inbox = _paths(game)["inbox"]
    inbox.mkdir(parents=True, exist_ok=True)
    bg_dl = ShooterVodBgDownloader(
        game,
        env,
        discover_fn=_discover_candidates,
        download_fn=_download_vod,
    )
    if inbox_files := sorted(
        _inbox_mp4_files(inbox),
        key=lambda p: _inbox_order_key(p, registry, game=game, state=state),
    ):
        if bg_dl.enabled() and inbox_files:
            bg_dl.start_if_idle(used)
    purged = _purge_junk_inbox_vods(game, inbox)
    if purged:
        log.info("purged short inbox vods game=%s count=%s", game, purged)

    inbox_files = sorted(
        _inbox_mp4_files(inbox),
        key=lambda p: _inbox_order_key(p, registry, game=game, state=state),
    )
    if game == "pubg" and _pubg_singles_first_enabled():
        from pubg_vod_singles_first import get_active_vod_id, pin_inbox_to_active_vod

        pinned = pin_inbox_to_active_vod(state, inbox_files, registry)
        if len(pinned) < len(inbox_files):
            log.info(
                "pubg singles inbox pin active=%s files=%s→%s",
                get_active_vod_id(state),
                len(inbox_files),
                len(pinned),
            )
        inbox_files = pinned
    max_vods = max(1, int(os.environ.get("SHOOTER_VOD_MAX_VODS_PER_RUN", "3")))
    if game == "pubg" and _pubg_singles_first_enabled():
        max_vods = max(1, int(os.environ.get("PUBG_SINGLES_MAX_VODS_PER_RUN", "4")))
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
        skip_cd = should_skip_vod_rescan(entry, game=game)
        if (
            skip_cd
            and game == "pubg"
            and _pubg_singles_first_enabled()
        ):
            from pubg_vod_singles_first import get_active_vod_id

            vid = vod_youtube_id(mp4)
            if get_active_vod_id(state) == vid:
                skip_cd = False
            elif entry and len(entry.get("last_pool_peaks") or []) >= 1:
                reason = str(entry.get("reject_reason") or "")
                if reason.startswith("pubg_singles") or reason.startswith("early_payoff"):
                    skip_cd = False
        if skip_cd:
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
            if os.environ.get("PUBG_METRO_INBOX_TRUST", "1") == "1":
                # Discovery already filtered by Metro title — skip redundant visual re-reject.
                ok_metro, metro_reason = True, "inbox_trusted"
            else:
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
        if game == "pubg" and _pubg_singles_first_enabled():
            from pubg_vod_singles_first import clear_active_vod, get_active_vod_id

            if get_active_vod_id(state) == vod_youtube_id(mp4):
                clear_active_vod(state, reason="zero_send_try_next_vod")
        log.info("zero-send continue next inbox vod game=%s tried=%s", game, tried)

    # All inbox files on rescan cooldown — skip re-scan this tick but still discover
    # fresh VODs (otherwise 100+ junk inbox blocks YouTube/Twitch forever).
    if tried == 0 and inbox_files:
        cooldown_only = True
        for mp4 in inbox_files:
            entries = _vod_registry_entries(state, mp4)
            if any(r.get("exhausted") for r in entries):
                continue
            entry = entries[0] if entries else None
            if not should_skip_vod_rescan(entry, game=game):
                cooldown_only = False
                break
        if cooldown_only and os.environ.get("SHOOTER_VOD_DISCOVERY_WHEN_INBOX_COOLDOWN", "1") == "1":
            log.info(
                "inbox rescan cooldown only — skip rescan, run discovery game=%s files=%s",
                game,
                len(inbox_files),
            )
        elif cooldown_only:
            log.info(
                "inbox rescan cooldown only — yield game=%s files=%s",
                game,
                len(inbox_files),
            )
            print(f"pipeline done sent=0 vods=0 game={game} inbox_cooldown=1")
            return 0

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
            for mp4 in list(_inbox_mp4_files(inbox)):
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
    vod, bg_pick = (None, None)
    if bg_dl.enabled():
        vod, bg_pick = bg_dl.pop_ready()
    if vod is None:
        vod = _download_vod(game, pick, env)
    else:
        log.info("using bg-downloaded vod=%s game=%s", vod.name, game)
        pick = bg_pick or pick
        _upsert_vod_registry(
            state,
            vid=str(pick.get("id") or vod_youtube_id(vod)),
            path=str(vod),
            title=str(pick.get("title") or ""),
            exhausted=False,
        )
        used.add(str(pick.get("id") or vod_youtube_id(vod)))
        state["used_youtube_ids"] = sorted(used)
        _save_state(game, state)
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
    file_env = load_env(ENV_PATH)
    os.environ.update({k: str(v) for k, v in file_env.items()})
    if os.environ.get("VOD_CONFIG_VALIDATE", "1") == "1":
        try:
            from vod_config import print_effective_config, validate_startup

            validate_startup()
            print_effective_config()
        except SystemExit as exc:
            log.error("config validation failed: %s", exc)
            return 1
    os.environ.setdefault("HIGHLIGHT_HEATMAP", "0")
    os.environ.setdefault("SHOOTER_VOD_FEED", "1")
    os.environ.setdefault("SHOOTER_VOD_FAST_PROBE", "1")
    os.environ.setdefault("SHOOTER_VOD_FAST_MONTAGE", "1")
    os.environ.setdefault("SHOOTER_VOD_MAX_VODS_PER_RUN", "3")
    os.environ.setdefault("SHOOTER_VOD_SKIP_DISCOVERY_WHEN_INBOX_DEAD", "1")
    # Never inherit a 600s floor — that blocked every 4–9 min Metro VOD.
    os.environ.setdefault(
        "SHOOTER_VOD_MIN_SEC",
        file_env.get("MLBB_VOD_MIN_SEC") or os.environ.get("MLBB_VOD_MIN_SEC") or "180",
    )
    os.environ.setdefault("SHOOTER_VOD_MAX_SEC", "14400")
    os.environ.setdefault("HIGHLIGHT_ALLOW_NO_CLIP", "1")
    # Disable CLIP during fast probe unless montage rank needs it (strict PUBG).
    if pubg_quality_strict() or os.environ.get("SHOOTER_VOD_MONTAGE_CLIP_RANK", "1") == "1":
        if os.environ.get("HIGHLIGHT_CLIP_DISABLED", "0") != "1":
            os.environ.setdefault("HIGHLIGHT_CLIP_DISABLED", "0")
    else:
        os.environ["HIGHLIGHT_CLIP_DISABLED"] = os.environ.get("HIGHLIGHT_CLIP_DISABLED", "1") or "1"
        if os.environ.get("HIGHLIGHT_CLIP_DISABLED") != "1":
            os.environ["HIGHLIGHT_CLIP_DISABLED"] = "1"
    os.environ.setdefault("SHOOTER_VOD_PREFER_RUSSIAN", "1")
    os.environ.setdefault("SHOOTER_VOD_SKIP_INTELLICLIP", "1")
    os.environ.setdefault("SHOOTER_VOD_MAX_PANN_PROBE", "24")
    os.environ.setdefault("HIGHLIGHT_MAX_STAGE1", "32")
    if game == "pubg":
        try:
            from pubg_owner_calibration import apply_owner_send_policy

            apply_owner_send_policy()
        except ImportError:
            pass
    if os.environ.get("SHOOTER_VOD_OWNER_EXEMPLARS", "1") == "1":
        os.environ["HIGHLIGHT_USE_OWNER_ANCHORS"] = "1"
    else:
        os.environ.setdefault("HIGHLIGHT_USE_OWNER_ANCHORS", "0")
    lock = _feed_lock(game)
    if lock is None:
        return 0
    env = {**os.environ, **file_env}
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
        "SHOOTER_VOD_MIN_SEC",
        "SHOOTER_VOD_MAX_SEC",
        "VOD_PUBG_ONLY",
        "VOD_PUBG_QUALITY_STRICT",
        "SHOOTER_VOD_MONTAGE_SHIP_PARTIAL",
        "PUBG_VOD_MONTAGE_SOFT_MIN_CLIPS",
        "SHOOTER_VOD_MONTAGES_PER_VOD",
        "SHOOTER_VOD_ALLOW_HIGHLIGHT_FALLBACK",
        "PUBG_METRO_SEGMENT_RELAX",
        "PUBG_METRO_TITLE_TRUST",
        "SHOOTER_VOD_MONTAGE_SHOOTING_ONLY",
        "SHOOTER_REQUIRE_AUTHOR_KILL",
        "DAILY_PUBG_QUOTA",
        "DAILY_MLBB_QUOTA",
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
        Path(f"/tmp/{game}_vod_segment_feed.pid").unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
