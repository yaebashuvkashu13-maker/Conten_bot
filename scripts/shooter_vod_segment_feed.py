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
    _vod_length_ok,
    _vod_max_sec,
    _vod_min_sec,
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
from strict_montage_direct import discover_strict_candidates, file_sha256
from vod_peak_gap import peak_too_close, segment_gap_sec, used_peak_times_shooter
from vod_scan_state import (
    max_peak_tries,
    minimal_pool_from_entry,
    note_zero_send_session,
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


def _note_discovery_fail(game: str, state: dict, *, reason: str) -> None:
    """Pause discovery after repeated junk/zero downloads — stop Telegram spam."""
    streak = int(state.get("discovery_fail_streak") or 0) + 1
    state["discovery_fail_streak"] = streak
    state["discovery_last_fail_reason"] = reason[:120]
    need = max(2, int(os.environ.get("SHOOTER_VOD_DISCOVERY_PAUSE_AFTER", "3")))
    pause_sec = max(60.0, float(os.environ.get("SHOOTER_VOD_DISCOVERY_PAUSE_SEC", "900")))
    if streak >= need:
        state["discovery_pause_until"] = time.time() + pause_sec
        state["discovery_fail_streak"] = 0
        log.warning(
            "discovery pause game=%s after %s fails reason=%s pause=%.0fs",
            game,
            need,
            reason,
            pause_sec,
        )
    _save_state(game, state)


def _note_discovery_ok(game: str, state: dict) -> None:
    state["discovery_fail_streak"] = 0
    state.pop("discovery_pause_until", None)
    _save_state(game, state)


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
    cycle = int(state.get("discovery_cycle", 0))
    params = cycle_fn(cycle, game, env)
    state["discovery_cycle"] = cycle + 1
    _save_state(game, state)

    out: list[dict] = []
    for url in params.get("urls", []):
        cmd = ytdlp_cmd(env) + ["--flat-playlist", "--print", "%(id)s|%(title)s|%(duration)s|%(uploader)s", url]
        cmd += ytdlp_extra_args(env)
        proc = run_ytdlp(cmd, env, timeout=120, label=f"search-{game}")
        if proc.returncode != 0:
            log.warning("search failed %s: %s", url, (proc.stderr or "")[:200])
            continue
        for line in (proc.stdout or "").splitlines():
            parts = line.split("|", 3)
            if len(parts) < 2:
                continue
            vid, title = parts[0][:11], parts[1]
            if vid in used or len(vid) != 11:
                continue
            if not title_ok_fn(game, title):
                continue
            try:
                dur = float(parts[2]) if len(parts) > 2 else 0.0
            except ValueError:
                dur = 0.0
            # PUBG/Standoff: require known duration — unknown → shorts/junk bleed through.
            if game in ("pubg", "standoff") and dur <= 0:
                continue
            shooter_min = float(
                os.environ.get(
                    "SHOOTER_VOD_MIN_SEC",
                    os.environ.get("MLBB_VOD_MIN_SEC", "480" if game == "pubg" else "180"),
                )
            )
            if game in ("pubg", "standoff") and dur < shooter_min:
                continue
            if dur and not _vod_length_ok(Path("x.mp4"), dur):
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
    return out


def _download_vod(game: str, pick: dict, env: dict[str, str]) -> Path | None:
    from youtube_download import download_one

    inbox = _paths(game)["inbox"]
    inbox.mkdir(parents=True, exist_ok=True)
    try:
        path = download_one(str(pick["url"]), inbox, env)
        return path
    except Exception as exc:
        log.warning("download failed %s: %s", pick.get("id"), exc)
        return None


def _validate_shooter_presend(
    game: str,
    vod: Path,
    row: dict,
    rendered: Path | None = None,
) -> tuple[bool, str, dict]:
    profile = _profile(game)
    start = float(row.get("peak_start", row.get("start", 0)))
    dur = _ffprobe_duration(rendered) if rendered is not None and rendered.exists() else 0.0
    if dur <= 0:
        clip = row.get("clip") or {}
        dur = float(
            row.get("duration")
            or clip.get("input_duration")
            or clip.get("output_duration")
            or 15
        )
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
    ok, reason, metrics = pubg_passes_combat_gate(vod, start, dur, profile)
    if not ok:
        return False, reason, metrics
    return True, "shooter_combat_ok", metrics


def _used_peak_times(game: str, vod_id: str, sent_set: set[str]) -> list[float]:
    return used_peak_times_shooter(vod_id, sent_set, load_index(game).get("segments", []))


def _peak_too_close(peak: float, used_peaks: list[float], gap_sec: float) -> bool:
    return peak_too_close(peak, used_peaks, gap_sec)


def _send_batch(game: str, token: str, chat_id: str, vod: Path, to_send: list[dict], sig: str) -> int:
    ok_cycle, cycle_reason = can_send_for_game(game, 1)
    if not ok_cycle:
        log.info("cycle block game=%s reason=%s", game, cycle_reason)
        return 0

    seg_root = _paths(game)["segments"]
    seg_root.mkdir(parents=True, exist_ok=True)
    sent = 0
    for row in to_send[:1]:
        sid = row["segment_id"]
        out = seg_root / f"seg_{sid}.mp4"
        presend_ok, presend_reason, presend_report = _validate_shooter_presend(game, vod, row)
        if not presend_ok:
            log.warning("presend REJECT %s: %s", sid, presend_reason)
            continue
        row["presend_metrics"] = presend_report
        for key in (
            "panns_gunshot",
            "panns_machine_gun",
            "panns_explosion",
            "center_motion",
            "boss_bar",
            "hit_flash",
            "hit_flash_count",
            "visual_pass",
        ):
            if presend_report.get(key) not in (None, ""):
                row[key] = presend_report.get(key)
        try:
            from vod_quality_model import quality_gate

            quality_ok, quality_reason, quality_prob = quality_gate(game, row)
            row["quality_probability"] = quality_prob
            if not quality_ok:
                log.warning("quality REJECT %s game=%s reason=%s", sid, game, quality_reason)
                continue
        except Exception as exc:
            log.warning("quality REJECT %s game=%s error=%s", sid, game, exc)
            continue
        if not render_single_segment(vod, row["clip"], out):
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
            stored = {
                **row,
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
            }
            stored.pop("clip", None)
            upsert_segment(game, stored)
            mark_feed_sent(game, [sid])
            sent += 1
    st = stats(game)
    if sent:
        send_message(token, chat_id, f"✅ {game.upper()} sent={sent} | 👍{st['feedback_yes']} 👎{st['feedback_no']}")
    return sent


def _inbox_order_key(mp4: Path, registry: list[dict]) -> tuple:
    """Unscanned VODs first; Metro + Russian titles before others."""
    from pubg_metro_royale_gate import title_metro_hint
    from youtube_game_prefs import russian_score

    entry = next((r for r in registry if r.get("path") == str(mp4)), None)
    scanned = float((entry or {}).get("last_scan_at") or 0)
    title = str((entry or {}).get("title") or "")
    metro_prio = 0 if title_metro_hint(title) else 1
    ru = russian_score({"title": title, "uploader": str((entry or {}).get("uploader") or "")})
    ru_prio = 0 if ru >= 0.10 else (1 if ru >= 0.05 else 2)
    fast_fail = 1 if str((entry or {}).get("reject_reason") or "").startswith("fast_panns_0") else 0
    return (1 if scanned else 0, fast_fail, metro_prio, ru_prio, scanned, mp4.stat().st_mtime)


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

    if entry and entry.get("last_pool_peaks"):
        cached = peak_values_from_entry(entry)
        if pool_peaks_fully_blocked(
            cached,
            used_peaks=used_peaks,
            gap_sec=seg_gap,
            blocked_sids=blocked_ids,
            vod_id=vid,
            lead_sec=lead,
        ):
            log.info("skip highlight rescan — cached peaks blocked vod=%s peaks=%s", vod.name, cached[:4])
            record_vod_scan(entry, sent=0, pool_peaks=cached, blocked=True)
            return 0

    if entry and pool_cache_valid(entry):
        pool = minimal_pool_from_entry(entry)
        log.info("reuse cached peak pool vod=%s peaks=%s", vod.name, len(pool))
    else:
        from highlight_scorer import clear_panns_score_cache

        clear_panns_score_cache()
        os.environ["HIGHLIGHT_BUILD_POOL"] = "1"
        try:
            pool = discover_strict_candidates(vod, profile, sig, blocked_ids)
        finally:
            os.environ.pop("HIGHLIGHT_BUILD_POOL", None)
    pool_peaks = peaks_from_pool(pool)
    if not pool:
        log.info("no candidates %s", vod.name)
        if entry is not None:
            record_vod_scan(entry, sent=0, pool_peaks=[], blocked=False)
        return 0

    probe_limit = int(os.environ.get("MLBB_VOD_PROBE_LIMIT", "24"))
    skip_peaks: set[float] = set()
    peak_tries = 0
    gate = _adaptive_gate(game)
    max_tries = max_peak_tries(soften_level, game=game, soft_max_fn=gate.soft_max_peak_tries)
    min_clip = float(os.environ.get("SHOOTER_VOD_MIN_CLIP_SCORE", "0.02"))
    owner_exemplars = os.environ.get("SHOOTER_VOD_OWNER_EXEMPLARS", "1") == "1"

    while peak_tries < max_tries:
        rows: list[dict] = []
        skipped_score = 0
        for clip in pool[:probe_limit]:
            peak = float(clip.get("start", 0))
            if any(abs(peak - s) <= 4.0 for s in skip_peaks):
                continue
            if _peak_too_close(peak, used_peaks, seg_gap):
                continue
            hm = clip.get("highlight_metrics") or {}
            clip_score = float(
                hm.get("clip_score")
                or clip.get("clip_score")
                or clip.get("score")
                or 0.0
            )
            panns_gun = float(hm.get("panns_gun_max") or clip.get("panns_gun_max") or 0.0)
            # Soft bypass: strong gun window must not die on tiny clip-score margin.
            score_floor = min_clip
            if panns_gun >= float(os.environ.get("SHOOTER_VOD_CLIP_BYPASS_GUN", "0.45")):
                score_floor = min(score_floor, 0.01)
            if owner_exemplars and clip_score < score_floor:
                skipped_score += 1
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
                    "score": float(clip.get("score", clip_score)),
                    "hook_score": float(hm.get("hook_score") or 0),
                    "clip_score": clip_score,
                    "highlight_metrics": hm,
                    "clip": {**clip, "start": start, "peak_start": peak},
                }
            )
        if not rows:
            blocked = pool_peaks_fully_blocked(
                pool_peaks,
                used_peaks=used_peaks,
                gap_sec=seg_gap,
                blocked_sids=blocked_ids,
                vod_id=vid,
                lead_sec=lead,
            )
            log.warning(
                "all peaks blocked vod=%s pool=%s used_peaks=%s gap=%.0fs soften=%s "
                "blocked=%s skipped_score=%s",
                vod.name,
                len(pool),
                used_peaks,
                seg_gap,
                soften_level,
                blocked,
                skipped_score,
            )
            # Bad cache (scores=0) — force full rediscover next cycle.
            if skipped_score and entry is not None and not blocked:
                entry.pop("last_pool_peaks", None)
                entry.pop("last_pool_at", None)
            if entry is not None:
                record_vod_scan(entry, sent=0, pool_peaks=pool_peaks, blocked=blocked, pool=pool)
            return 0
        rows.sort(key=lambda r: float(r.get("score", 0)), reverse=True)
        n = _send_batch(game, token, chat_id, vod, rows[:1], sig)
        if n > 0:
            if entry is not None:
                record_vod_scan(entry, sent=n, pool_peaks=pool_peaks, blocked=False, pool=pool)
            return n
        skip_peaks.add(round(float(rows[0].get("peak_start", rows[0]["start"])), 1))
        peak_tries += 1
        log.warning(
            "presend rejected peak — try next (%s/%s) vod=%s game=%s",
            peak_tries,
            max_tries,
            vod.name,
            game,
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

    if game in ("pubg", "standoff") and os.environ.get("SHOOTER_VOD_FAST_PROBE", "1") == "1":
        from shooter_vod_fast_scan import (
            apply_fast_probe_seeds,
            clear_fast_probe_seeds,
            vod_fast_combat_check,
        )

        clear_fast_seeds = clear_fast_probe_seeds
        ok_fast, fast_reason, seed_peaks = vod_fast_combat_check(vod, _profile(game))
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
            if os.environ.get("SHOOTER_VOD_FAST_SKIP_NOTIFY", "0") == "1":
                send_message(token, chat_id, f"⏭ {game.upper()} {vid}: быстрый skip — {fast_reason}")
            return 0
        apply_fast_probe_seeds(seed_peaks)

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

    if game == "wot" and os.environ.get("WOT_VOD_FAST_PROBE", "1") == "1":
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
    entry = _vod_registry_entry(state, vod) or entry
    if entry is not None:
        if sent == 0:
            sessions = note_zero_send_session(entry)
            log.info("zero_send_sessions=%s vod=%s", sessions, vod.name)
            if not entry.get("last_pool_peaks"):
                entry.setdefault("reject_reason", "no_combat_peaks")
            elif not entry.get("reject_reason"):
                entry["reject_reason"] = "presend_rejected_all_peaks"
        else:
            entry["zero_send_sessions"] = 0
            entry.pop("reject_reason", None)
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

    for mp4 in sorted(inbox.glob("yt_*.mp4"), key=lambda p: _inbox_order_key(p, registry)):
        entry = next((r for r in registry if r.get("path") == str(mp4)), None)
        if entry and entry.get("exhausted"):
            continue
        if should_skip_vod_rescan(entry, game=game):
            log.info("skip scan cooldown vod=%s", mp4.name)
            continue
        if _ffprobe_duration(mp4) < _vod_min_sec():
            continue
        if entry is None:
            entry = {
                "id": vod_youtube_id(mp4),
                "path": str(mp4),
                "title": "",
                "exhausted": False,
            }
            registry.append(entry)
        if game == "pubg":
            streak_in = _adaptive_gate(game).streak_from_state(state)
            title = str(entry.get("title") or "")
            ok_metro, metro_reason = _pubg_metro_vod_ok(mp4, title=title, streak=streak_in)
            if not ok_metro:
                log.warning("metro skip inbox vod=%s reason=%s", mp4.name, metro_reason)
                entry["reject_reason"] = metro_reason
                if _pubg_metro_should_exhaust(title, streak_in):
                    entry["exhausted"] = True
                _save_state(game, state)
                continue
        n = _scan_vod_with_adaptive(game, token, chat_id, mp4, env, state)
        state["vods"] = registry
        if n == 0:
            entry = _vod_registry_entry(state, mp4) or entry
            if entry and not entry.get("exhausted") and should_mark_vod_exhausted(entry):
                if not entry.get("last_pool_peaks"):
                    entry.setdefault("reject_reason", "no_combat_peaks")
                else:
                    entry.setdefault("reject_reason", "all_peaks_blocked")
                entry["exhausted"] = True
                log.info(
                    "exhausted vod=%s reason=%s",
                    mp4.name,
                    entry.get("reject_reason"),
                )
        _save_state(game, state)
        print(f"pipeline done sent={n} vods=1 game={game}")
        return 0

    if os.environ.get("SHOOTER_VOD_SKIP_DISCOVERY", "0") == "1":
        log.info("skip discovery — inbox exhausted game=%s", game)
        print(f"pipeline done sent=0 vods=0 game={game} skip_discovery=1")
        return 0

    pause_until = float(state.get("discovery_pause_until") or 0)
    if pause_until > time.time():
        remain = int(pause_until - time.time())
        log.info("discovery pause game=%s remain=%ss", game, remain)
        print(f"pipeline done sent=0 vods=0 game={game} discovery_pause={remain}")
        return 0

    candidates = _discover_candidates(game, env, used)
    if not candidates:
        # Quiet "not found" spam — notify at most once per pause window.
        last_nf = float(state.get("last_not_found_notify_at") or 0)
        nf_gap = float(os.environ.get("SHOOTER_VOD_NOT_FOUND_NOTIFY_SEC", "900"))
        if time.time() - last_nf >= nf_gap:
            send_message(token, chat_id, f"⚠️ Не нашёл новый {game.upper()} стрим. Повторю позже.")
            state["last_not_found_notify_at"] = time.time()
            _save_state(game, state)
        print(f"pipeline done sent=0 vods=0 game={game}")
        return 0

    pick = None
    if game in ("pubg", "standoff"):
        from youtube_shooter_vod_prefs import pick_discovery_candidate

        pick = pick_discovery_candidate(game, candidates)
    if pick is None:
        pick = candidates[0]
    if os.environ.get("SHOOTER_VOD_DOWNLOAD_NOTIFY", "0") == "1":
        send_message(token, chat_id, f"📥 Качаю {game.upper()} VOD с YouTube…")
    else:
        log.info(
            "download start game=%s id=%s dur=%s title=%s",
            game,
            pick.get("id"),
            pick.get("duration"),
            str(pick.get("title") or "")[:80],
        )
    vod = _download_vod(game, pick, env)
    if not vod:
        _note_discovery_fail(game, state, reason="download_failed")
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
            if os.environ.get("SHOOTER_VOD_METRO_REJECT_NOTIFY", "1") == "1":
                send_message(
                    token,
                    chat_id,
                    f"⏭ Пропускаю VOD — не Metro Royale: {pick.get('title', pick.get('id'))[:80]}\n{metro_reason}",
                )
            exhausted = _pubg_metro_should_exhaust(str(pick.get("title") or ""), streak_dl)
            registry.append(
                {
                    "id": pick["id"],
                    "path": str(vod),
                    "title": pick.get("title", ""),
                    "exhausted": exhausted,
                    "reject_reason": metro_reason,
                }
            )
            used.add(pick["id"])
            state["vods"] = registry
            state["used_youtube_ids"] = sorted(used)
            _note_discovery_fail(game, state, reason=f"metro_reject:{metro_reason}")
            print(f"pipeline done sent=0 vods=1 game={game} metro_reject=1")
            return 0

    registry.append(
        {
            "id": pick["id"],
            "path": str(vod),
            "title": pick.get("title", ""),
            "exhausted": False,
        }
    )
    used.add(pick["id"])
    state["vods"] = registry
    state["used_youtube_ids"] = sorted(used)
    _save_state(game, state)

    n = _scan_vod_with_adaptive(game, token, chat_id, vod, env, state)
    if n > 0:
        _note_discovery_ok(game, state)
    else:
        entry = _vod_registry_entry(state, vod)
        reason = str((entry or {}).get("reject_reason") or "zero_send")
        _note_discovery_fail(game, state, reason=reason)
    print(f"pipeline done sent={n} vods=1 game={game}")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    game = _game()
    os.environ.setdefault("HIGHLIGHT_HEATMAP", "0")
    os.environ.setdefault("SHOOTER_VOD_FEED", "1")
    os.environ.setdefault("SHOOTER_VOD_FAST_PROBE", "1")
    os.environ.setdefault("SHOOTER_VOD_PREFER_RUSSIAN", "1")
    os.environ.setdefault("SHOOTER_VOD_SKIP_INTELLICLIP", "1")
    os.environ.setdefault("SHOOTER_VOD_MAX_PANN_PROBE", "24")
    os.environ.setdefault("HIGHLIGHT_MAX_STAGE1", "32")
    if os.environ.get("SHOOTER_VOD_OWNER_EXEMPLARS", "1") == "1":
        os.environ["HIGHLIGHT_USE_OWNER_ANCHORS"] = "1"
        os.environ.setdefault("HIGHLIGHT_CLIP_DISABLED", "0")
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
