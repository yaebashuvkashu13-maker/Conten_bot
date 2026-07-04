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
from pubg_fight_segment import apply_fight_bounds_to_clip
from strict_montage_direct import discover_strict_candidates, file_sha256
from vod_peak_gap import peak_too_close, segment_gap_sec, used_peak_times_shooter
from vod_scan_state import (
    exclude_intervals_env,
    fight_intervals_from_entry,
    max_peak_tries,
    peak_values_from_entry,
    peaks_from_pool,
    peaks_near_sent_reason,
    pool_cache_valid,
    pool_peaks_fully_blocked,
    record_fight_interval,
    record_vod_scan,
    scan_zero_detail,
    shooter_interval_blocked,
    shooter_peak_fight_blocked,
    should_mark_vod_exhausted,
    should_skip_vod_rescan,
    used_intervals_for_shooter_vod,
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
            if dur <= 0 or not _vod_length_ok(Path("x.mp4"), dur):
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


def _validate_shooter_candidate_pre_render(game: str, vod: Path, row: dict) -> tuple[bool, str, dict]:
    """Cheap pre-render validation to avoid wasting ffmpeg on obvious rejects."""
    clip_meta = row.get("clip") or {}
    if clip_meta.get("owner_label_cut"):
        if game == "pubg":
            from pubg_metro_royale_gate import segment_looks_metro_royale

            start = float(row.get("peak_start", row.get("start", 0)))
            dur = float(clip_meta.get("input_duration") or clip_meta.get("fight_dur") or 45.0)
            ok_metro, metro_reason = segment_looks_metro_royale(vod, start, dur)
            if not ok_metro:
                return False, metro_reason, {"metro": metro_reason}
        return True, "owner_label_cut", {"owner_label_cut": True}
    profile = _profile(game)
    start = float(row.get("peak_start", row.get("start", 0)))
    dur = float(row.get("duration") or 15.0)
    if dur <= 0:
        dur = 15.0
    if game == "pubg":
        from pubg_metro_royale_gate import segment_looks_metro_royale

        ok_metro, metro_reason = segment_looks_metro_royale(vod, start, dur)
        if not ok_metro:
            return False, metro_reason, {"metro": metro_reason}
    if game in EXTENDED_GAMES:
        from strict_segment_gate import passes_strict_gate

        ok, reason, metrics = passes_strict_gate(vod, start, dur, profile)
        return ok, reason, metrics
    scan_fast = os.environ.get("SHOOTER_VOD_COMBAT_FAST", "1") == "1"
    ok, reason, metrics = pubg_passes_combat_gate(vod, start, dur, profile, scan_fast=scan_fast)
    if not ok:
        return False, reason, metrics
    return True, "shooter_combat_ok", metrics


def _validate_shooter_presend(game: str, vod: Path, row: dict, rendered: Path) -> tuple[bool, str, dict]:
    profile = _profile(game)
    clip_meta = row.get("clip") or {}
    is_owner_cut = bool(clip_meta.get("owner_label_cut"))
    start = float(row.get("peak_start", row.get("start", 0)))
    dur = _ffprobe_duration(rendered)
    if dur <= 0:
        dur = float(row.get("duration", 15))
    if game == "pubg":
        from pubg_metro_royale_gate import segment_looks_metro_royale

        ok_metro, metro_reason = segment_looks_metro_royale(vod, start, dur)
        if not ok_metro:
            return False, metro_reason, {"metro": metro_reason}
    if is_owner_cut:
        if dur < 1.0:
            return False, "rendered_too_short", {"duration": dur}
        from shooter_vod_presend_audit import audit_shooter_presend

        audit_ok, audit_reason, audit_report = audit_shooter_presend(
            game,
            rendered,
            source_vod=vod,
            source_start=start,
            profile=profile,
            owner_label_cut=True,
        )
        if not audit_ok:
            return False, audit_reason, audit_report
        return True, "owner_label_cut", audit_report
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
    try:
        from standoff_vod_bootstrap import standoff_bootstrap_loose

        bootstrap = standoff_bootstrap_loose(game)
    except ImportError:
        bootstrap = False
    if bootstrap:
        clip = row.get("clip") or {}
        hm = clip.get("highlight_metrics") or {}
        gate_reason = str(clip.get("gate_reason") or hm.get("pass_reason") or "")
        if gate_reason.startswith("combat_fast") or float(hm.get("panns_gun_max") or 0) >= 0.08:
            ok, reason, metrics = pubg_passes_combat_gate(
                rendered, 0.0, dur, profile, scan_fast=True
            )
            if ok:
                return True, f"bootstrap_combat_fast:{reason}", metrics
    ok, reason, metrics = pubg_passes_combat_gate(
        vod,
        start,
        dur,
        profile,
        scan_fast=os.environ.get("PUBG_PRESEND_COMBAT_FAST", "0") == "1",
    )
    if not ok:
        return False, reason, metrics
    from shooter_vod_presend_audit import audit_shooter_presend

    audit_ok, audit_reason, audit_report = audit_shooter_presend(
        game,
        rendered,
        source_vod=vod,
        source_start=start,
        profile=profile,
    )
    if not audit_ok:
        return False, audit_reason, audit_report
    return True, "shooter_combat_ok", {**(metrics or {}), **audit_report}


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
        pre_ok, pre_reason, _pre_report = _validate_shooter_candidate_pre_render(game, vod, row)
        if not pre_ok:
            log.warning("presend PRECHECK REJECT %s: %s", sid, pre_reason)
            continue
        if not render_single_segment(vod, row["clip"], out):
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
    """Unscanned VODs first; owner-calibrated + Metro RU before others."""
    from pubg_metro_royale_gate import title_metro_hint
    from vod_owner_learning import owner_labels_for_vod_scan
    from youtube_game_prefs import russian_score

    profile = game if game in ("pubg", "standoff") else "pubg"
    entry = next((r for r in registry if r.get("path") == str(mp4)), None)
    scanned = float((entry or {}).get("last_scan_at") or 0)
    title = str((entry or {}).get("title") or "")
    metro_prio = 0 if title_metro_hint(title) else 1
    ru = russian_score({"title": title, "uploader": str((entry or {}).get("uploader") or "")})
    ru_prio = 0 if ru >= 0.10 else (1 if ru >= 0.05 else 2)
    fast_fail = 1 if str((entry or {}).get("reject_reason") or "").startswith("fast_panns_0") else 0
    try:
        owner_good = sum(
            1 for row in owner_labels_for_vod_scan(mp4, profile) if row.get("label") == "good"
        )
    except Exception:
        owner_good = 0
    owner_prio = 0 if owner_good else 1
    return (1 if scanned else 0, fast_fail, owner_prio, metro_prio, ru_prio, scanned, mp4.stat().st_mtime)


def _shooter_scan_max_sec() -> float:
    return max(300.0, float(os.environ.get("SHOOTER_VOD_SCAN_MAX_SEC", "1200")))


def _owner_peak_already_sent(peak: float, used_peaks: list[float], *, tol_sec: float = 4.0) -> bool:
    """Owner calibration marks each timestamp as its own fight — block only exact/near duplicates."""
    return any(abs(peak - p) <= tol_sec for p in used_peaks)


def _reject_vod_length(vod: Path, entry: dict | None, *, mark_exhausted: bool = True) -> str | None:
    """Return reject reason when VOD is outside 3–20 min window."""
    dur = _ffprobe_duration(vod)
    if _vod_length_ok(vod, dur):
        return None
    reason = f"vod_length={dur:.0f}s"
    if entry is not None:
        entry["reject_reason"] = reason
        if mark_exhausted:
            entry["exhausted"] = True
    return reason


def _streak_skip_reason(entry: dict | None) -> bool:
    """Fast rejects / infra failures — do not inflate adaptive soften streak."""
    reason = str((entry or {}).get("reject_reason") or "")
    return reason.startswith(
        (
            "fast_panns",
            "fast_probe",
            "metro_vod",
            "metro_title",
            "metro_soften",
            "score_timeout",
            "scan_timeout",
            "peaks_near_sent",
        )
    )


def _owner_label_pool_clips(
    vod: Path,
    game: str,
    vid: str,
    *,
    blocked_ids: set[str],
    used_peaks: list[float],
    seg_gap: float,
) -> list[dict]:
    """Exact owner calibration timestamps — cut at user labels, not shifted detector peaks."""
    if game != "pubg" or os.environ.get("SHOOTER_VOD_OWNER_LABEL_CUTS", "0") != "1":
        return []
    try:
        from pubg_owner_calibration import has_owner_labels, labels_for_video
    except ImportError:
        return []
    if not has_owner_labels(vod):
        return []
    lead = float(os.environ.get("MLBB_VOD_LEAD_SEC", "4"))
    out: list[dict] = []
    for row in sorted(labels_for_video(vod), key=lambda r: float(r.get("time_sec", 0))):
        if row.get("label") != "good":
            continue
        peak = float(row["time_sec"])
        if _owner_peak_already_sent(peak, used_peaks):
            continue
        start = max(0.0, peak - lead)
        if segment_id(vid, start) in blocked_ids:
            continue
        out.append(
            {
                "start": peak,
                "peak_start": peak,
                "score": 1.0,
                "hook_score": 0.0,
                "gate_reason": "owner_label_cut",
                "owner_label_cut": True,
            }
        )
    return out


def _merge_owner_label_pool(pool: list[dict], owner_pool: list[dict]) -> list[dict]:
    if not owner_pool:
        return pool
    anchor_peaks = [round(float(c["start"]), 0) for c in owner_pool]
    merged = list(owner_pool)
    for clip in pool:
        peak = round(float(clip.get("start", 0)), 0)
        if any(abs(peak - ap) < 8 for ap in anchor_peaks):
            continue
        merged.append(clip)
    return merged


def _scan_vod(
    game: str,
    token: str,
    chat_id: str,
    vod: Path,
    env: dict[str, str],
    *,
    soften_level: int = 0,
    entry: dict | None = None,
    scan_deadline: float | None = None,
) -> int:
    profile = _profile(game)
    sig = file_sha256(vod)
    labeled = labeled_ids(game)
    sent_set = load_feed_sent(game)
    vid = vod_youtube_id(vod)
    lead = float(os.environ.get("MLBB_VOD_LEAD_SEC", "4"))
    seg_gap = segment_gap_sec(game, soften_level=soften_level)
    index_segments = load_index(game).get("segments", [])
    blocked_ids = labeled | sent_set
    used_peaks = used_peaks_for_vod(game, vid, blocked_ids, index_segments)
    sent_intervals = used_intervals_for_shooter_vod(
        vid, blocked_ids, index_segments, vod_path=vod
    )
    presend_intervals = fight_intervals_from_entry(entry)
    if entry is not None and (sent_intervals or not used_peaks):
        entry["fight_intervals"] = []
        presend_intervals = []
    reserved_intervals = list(sent_intervals) + list(presend_intervals)

    exclude_env = exclude_intervals_env(sent_intervals)
    if exclude_env:
        os.environ["HIGHLIGHT_EXCLUDE_INTERVALS"] = exclude_env
        max_hi = max(hi for _, hi in sent_intervals)
        os.environ["SHOOTER_VOD_MIN_PROBE_START"] = str(max(0.0, max_hi + 4.0))
        if entry is not None and pool_cache_valid(entry):
            entry.pop("last_pool_peaks", None)
            entry.pop("last_pool_at", None)
        log.info(
            "exclude sent fight windows vod=%s intervals=%s min_probe=%.0f",
            vod.name,
            exclude_env[:80],
            float(os.environ["SHOOTER_VOD_MIN_PROBE_START"]),
        )
    else:
        os.environ.pop("HIGHLIGHT_EXCLUDE_INTERVALS", None)
        os.environ.pop("SHOOTER_VOD_MIN_PROBE_START", None)

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

    if scan_deadline and time.time() >= scan_deadline:
        log.warning("vod scan timeout before highlight vod=%s", vod.name)
        if entry is not None:
            entry["reject_reason"] = "scan_timeout"
            record_vod_scan(entry, sent=0, pool_peaks=[], blocked=True)
        return 0

    from highlight_scorer import clear_panns_score_cache

    owner_pool = _owner_label_pool_clips(
        vod,
        game,
        vid,
        blocked_ids=blocked_ids,
        used_peaks=used_peaks,
        seg_gap=seg_gap,
    )
    skip_heavy = (
        bool(owner_pool)
        and game == "pubg"
        and os.environ.get("SHOOTER_VOD_OWNER_SKIP_HEAVY_SCAN", "0") == "1"
    )
    if skip_heavy:
        pool = list(owner_pool)
        log.info(
            "owner label fast path %s: %s anchors (skip heavy highlight scan)",
            vod.name,
            len(owner_pool),
        )
    else:
        clear_panns_score_cache()
        os.environ["HIGHLIGHT_BUILD_POOL"] = "1"
        try:
            pool = discover_strict_candidates(vod, profile, sig, blocked_ids)
        finally:
            os.environ.pop("HIGHLIGHT_BUILD_POOL", None)
        if owner_pool:
            pool = _merge_owner_label_pool(pool, owner_pool)
            log.info(
                "owner label cuts %s: %s anchors first (detector pool=%s)",
                vod.name,
                len(owner_pool),
                len(pool),
            )
    pool_peaks = peaks_from_pool(pool)
    if reserved_intervals and pool:
        from vod_scan_state import shooter_min_clip_sep_sec

        min_peak = max(hi for _, hi in reserved_intervals) + shooter_min_clip_sep_sec() * 0.85
        before = len(pool)
        pool = [c for c in pool if float(c.get("start", 0)) >= min_peak]
        if len(pool) < before:
            log.info(
                "pool sent-tail filter vod=%s %s->%s min_peak=%.0f sent_end=%.0f",
                vod.name,
                before,
                len(pool),
                min_peak,
                max(hi for _, hi in reserved_intervals),
            )
        pool_peaks = peaks_from_pool(pool)
    if entry is not None:
        try:
            from highlight_scorer import last_vod_diag

            diag = last_vod_diag(vod)
            if diag.get("pann_prefilter"):
                entry["last_pann_prefilter"] = int(diag["pann_prefilter"])
        except Exception:
            pass
    if not pool:
        log.info("no candidates %s", vod.name)
        if entry is not None:
            try:
                from highlight_scorer import last_vod_diag

                diag = last_vod_diag(vod)
                if diag:
                    entry["last_fail_reasons"] = diag
                    if diag.get("pann_prefilter"):
                        entry["last_pann_prefilter"] = int(diag["pann_prefilter"])
                    if diag.get("timeouts"):
                        entry["reject_reason"] = f"score_timeout:{diag.get('timeouts')}"
            except Exception:
                pass
            record_vod_scan(entry, sent=0, pool_peaks=[], blocked=False)
        return 0

    probe_limit = int(os.environ.get("MLBB_VOD_PROBE_LIMIT", "24"))
    skip_peaks: set[float] = set()
    peak_tries = 0
    gate = _adaptive_gate(game)
    max_tries = max_peak_tries(soften_level, game=game, soft_max_fn=gate.soft_max_peak_tries)
    min_clip = float(os.environ.get("SHOOTER_VOD_MIN_CLIP_SCORE", "0.03"))
    owner_exemplars = os.environ.get("SHOOTER_VOD_OWNER_EXEMPLARS", "1") == "1"
    try:
        from standoff_vod_bootstrap import standoff_bootstrap_loose

        bootstrap_loose = standoff_bootstrap_loose(game)
    except ImportError:
        bootstrap_loose = False

    while peak_tries < max_tries:
        if scan_deadline and time.time() >= scan_deadline:
            log.warning("vod scan timeout vod=%s tries=%s", vod.name, peak_tries)
            if entry is not None:
                entry["reject_reason"] = "scan_timeout"
                record_vod_scan(entry, sent=0, pool_peaks=pool_peaks, blocked=False)
            return 0
        rows: list[dict] = []
        for clip in pool[:probe_limit]:
            peak = float(clip.get("start", 0))
            is_owner_cut = bool(clip.get("owner_label_cut"))
            if any(abs(peak - s) <= 4.0 for s in skip_peaks):
                continue
            if is_owner_cut:
                if _owner_peak_already_sent(peak, used_peaks):
                    continue
            else:
                if not bootstrap_loose:
                    if shooter_peak_fight_blocked(peak, used_peaks, game=game, soften_gap=seg_gap):
                        continue
                    if _peak_too_close(peak, used_peaks, seg_gap):
                        continue
                elif _owner_peak_already_sent(peak, used_peaks, tol_sec=6.0):
                    continue
            hm = clip.get("highlight_metrics") or {}
            clip_score = float(hm.get("clip_score") or clip.get("score") or 0.0)
            panns_max = float(hm.get("panns_gun_max") or 0.0)
            gate_reason = str(clip.get("gate_reason") or hm.get("pass_reason") or "")
            combat_trust = gate_reason.startswith("combat_fast")
            owner_anchor = is_owner_cut
            if not owner_anchor and game == "pubg":
                try:
                    from pubg_owner_calibration import has_owner_labels, segment_overlaps_owner_label

                    owner_anchor = has_owner_labels(vod) and segment_overlaps_owner_label(
                        vod, peak, 12.0, label="good", pad_sec=10.0
                    )
                except ImportError:
                    owner_anchor = False
            if (
                owner_exemplars
                and clip_score < min_clip
                and not combat_trust
                and not owner_anchor
                and not bootstrap_loose
            ):
                continue
            start = max(0.0, float(peak) - lead)
            sid = segment_id(vid, start)
            if sid in blocked_ids:
                continue
            clip_row = apply_fight_bounds_to_clip(
                {
                    **clip,
                    "start": start,
                    "peak_start": peak,
                    "owner_label_cut": bool(clip.get("owner_label_cut")),
                },
                vod,
            )
            clip_start = float(clip_row["start"])
            clip_end = clip_start + float(
                clip_row.get("input_duration") or clip_row.get("fight_dur") or 45.0
            )
            if not is_owner_cut and not bootstrap_loose and shooter_interval_blocked(
                clip_start, clip_end, reserved_intervals
            ):
                continue
            rows.append(
                {
                    "segment_id": sid,
                    "start": float(clip_row["start"]),
                    "peak_start": float(clip_row.get("peak_start", peak)),
                    "score": float(clip.get("score", 0)),
                    "clip": clip_row,
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
                "all peaks blocked vod=%s pool=%s used_peaks=%s gap=%.0fs soften=%s blocked=%s",
                vod.name,
                len(pool),
                used_peaks,
                seg_gap,
                soften_level,
                blocked,
            )
            if entry is not None:
                entry["last_sent_peaks"] = list(used_peaks)
                if used_peaks or blocked:
                    reject_reason = f"peaks_near_sent pool={len(pool)} sent={used_peaks}"
                elif int(entry.get("presend_reject_streak") or 0) > 0:
                    reject_reason = "presend_reject"
                else:
                    reject_reason = f"pool_filtered pool={len(pool)}"
                record_vod_scan(
                    entry,
                    sent=0,
                    pool_peaks=pool_peaks,
                    blocked=blocked,
                    pool=pool,
                    reject_reason=reject_reason,
                )
            return 0
        rows.sort(key=lambda r: float(r.get("score", 0)), reverse=True)
        n = _send_batch(game, token, chat_id, vod, rows[:1], sig)
        if n > 0:
            if entry is not None:
                entry["presend_reject_streak"] = 0
                sent_row = rows[0]
                cs = float(sent_row["start"])
                ce = cs + float(
                    sent_row.get("clip", {}).get("input_duration")
                    or sent_row.get("clip", {}).get("fight_dur")
                    or 45.0
                )
                record_fight_interval(entry, cs, ce)
                record_vod_scan(entry, sent=n, pool_peaks=pool_peaks, blocked=False, pool=pool)
            return n
        skip_peaks.add(round(float(rows[0].get("peak_start", rows[0]["start"])), 1))
        peak_tries += 1
        sent_row = rows[0]
        clip_start = float(sent_row["start"])
        clip_end = clip_start + float(
            sent_row.get("clip", {}).get("input_duration")
            or sent_row.get("clip", {}).get("fight_dur")
            or 45.0
        )
        is_owner_reject = bool(sent_row.get("clip", {}).get("owner_label_cut"))
        bootstrap_skip_interval = game == "standoff" and os.environ.get("STANDOFF_VOD_BOOTSTRAP", "1") == "1"
        if not is_owner_reject and not bootstrap_skip_interval:
            record_fight_interval(entry, clip_start, clip_end)
            reserved_intervals.append((clip_start, clip_end))
        if entry is not None:
            entry["presend_reject_streak"] = int(entry.get("presend_reject_streak") or 0) + 1
        log.warning(
            "presend rejected peak — try next (%s/%s) vod=%s game=%s",
            peak_tries,
            max_tries,
            vod.name,
            game,
        )
    if entry is not None:
        if int(entry.get("presend_reject_streak") or 0) >= max(
            1,
            int(
                os.environ.get(
                    "SHOOTER_VOD_PRESEND_EXHAUST_AFTER",
                    os.environ.get("MLBB_VOD_PRESEND_EXHAUST_AFTER", "2"),
                )
            ),
        ):
            entry.setdefault("reject_reason", "presend_exhausted")
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

    if game == "standoff":
        from standoff_vod_bootstrap import apply_standoff_bootstrap_env, standoff_bootstrap_active

        if apply_standoff_bootstrap_env() and standoff_bootstrap_active():
            streak_in = max(streak_in, int(os.environ.get("STANDOFF_BOOTSTRAP_MIN_STREAK", "3")))

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
    scan_deadline = time.time() + _shooter_scan_max_sec()

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
            sent = _scan_vod(
                game,
                token,
                chat_id,
                vod,
                env,
                soften_level=level,
                entry=entry,
                scan_deadline=scan_deadline,
            )
            if game == "pubg":
                os.environ.pop("PUBG_METRO_SEGMENT_TRUST_VOD", None)
    finally:
        if clear_fast_seeds is not None:
            clear_fast_seeds()

    entry = _vod_registry_entry(state, vod) or entry
    new_streak = gate.record_vod_outcome(
        state,
        vod_id=vid,
        sent=sent,
        streak_skip=_streak_skip_reason(entry),
    )
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

    for mp4 in sorted(inbox.glob("yt_*.mp4"), key=lambda p: _inbox_order_key(p, registry, game=game)):
        entry = next((r for r in registry if r.get("path") == str(mp4)), None)
        if entry and entry.get("exhausted"):
            continue
        if entry and peaks_near_sent_reason(entry):
            partial_at = float(entry.get("partial_scan_at") or entry.get("last_scan_at") or 0)
            if partial_at > 0 and (time.time() - partial_at) < 1800:
                log.info("skip partial vod (try others first) vod=%s", mp4.name)
                continue
        if should_skip_vod_rescan(entry, game=game):
            log.info("skip scan cooldown vod=%s", mp4.name)
            continue
        length_reason = _reject_vod_length(mp4, entry)
        if length_reason:
            log.warning("skip inbox vod=%s reason=%s", mp4.name, length_reason)
            _save_state(game, state)
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
        if n > 0:
            _save_state(game, state)
            print(f"pipeline done sent={n} vods=1 game={game}")
            return 0
        entry = _vod_registry_entry(state, mp4) or entry
        if entry and peaks_near_sent_reason(entry):
            entry["partial_scan_at"] = time.time()
            log.info("partial vod — try next inbox vod=%s sent_peaks=%s", mp4.name, entry.get("last_sent_peaks"))
            _save_state(game, state)
            continue
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
        print(f"pipeline done sent=0 vods=1 game={game}")
        return 0

    if os.environ.get("SHOOTER_VOD_SKIP_DISCOVERY", "0") == "1":
        log.info("skip discovery — inbox exhausted game=%s", game)
        print(f"pipeline done sent=0 vods=0 game={game} skip_discovery=1")
        return 0

    candidates = _discover_candidates(game, env, used)
    if not candidates:
        send_message(token, chat_id, f"⚠️ Не нашёл новый {game.upper()} стрим. Повторю позже.")
        print(f"pipeline done sent=0 vods=0 game={game}")
        return 0

    pick = None
    if game in ("pubg", "standoff"):
        from youtube_shooter_vod_prefs import pick_discovery_candidate

        pick = pick_discovery_candidate(game, candidates)
    if pick is None:
        pick = candidates[0]
    send_message(token, chat_id, f"📥 Качаю {game.upper()} VOD с YouTube…")
    vod = _download_vod(game, pick, env)
    if not vod:
        print(f"pipeline done sent=0 vods=0 game={game}")
        return 0

    length_reason = _reject_vod_length(vod, None, mark_exhausted=False)
    if length_reason:
        log.warning(
            "length reject vod=%s title=%s reason=%s",
            pick.get("id"),
            pick.get("title", ""),
            length_reason,
        )
        if os.environ.get("SHOOTER_VOD_LENGTH_REJECT_NOTIFY", "1") == "1":
            send_message(
                token,
                chat_id,
                f"⏭ Пропускаю VOD — неподходящая длина ({length_reason}): "
                f"{pick.get('title', pick.get('id'))[:80]}",
            )
        registry.append(
            {
                "id": pick["id"],
                "path": str(vod),
                "title": pick.get("title", ""),
                "exhausted": True,
                "reject_reason": length_reason,
            }
        )
        used.add(pick["id"])
        state["vods"] = registry
        state["used_youtube_ids"] = sorted(used)
        _save_state(game, state)
        print(f"pipeline done sent=0 vods=1 game={game} length_reject=1")
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
            _save_state(game, state)
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
    print(f"pipeline done sent={n} vods=1 game={game}")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    game = _game()
    if game == "standoff":
        from standoff_vod_bootstrap import apply_standoff_bootstrap_env

        apply_standoff_bootstrap_env()
    os.environ.setdefault("HIGHLIGHT_HEATMAP", "0")
    os.environ.setdefault("SHOOTER_VOD_FEED", "1")
    os.environ.setdefault("SHOOTER_VOD_FAST_PROBE", "1")
    os.environ.setdefault("SHOOTER_VOD_FULL_PASS", "1")
    os.environ.setdefault("SHOOTER_VOD_STAGE1_FAST", "0")
    os.environ.setdefault("SHOOTER_VOD_PREFER_RUSSIAN", "1")
    os.environ.setdefault("SHOOTER_VOD_SKIP_INTELLICLIP", "1")
    os.environ.setdefault("SHOOTER_VOD_MAX_PANN_PROBE", "24")
    os.environ.setdefault("HIGHLIGHT_MAX_STAGE1", "48")
    os.environ.setdefault("HIGHLIGHT_USE_OWNER_ANCHORS", "0")
    os.environ.setdefault("SHOOTER_VOD_OWNER_LABEL_CUTS", "0")
    os.environ.setdefault("SHOOTER_VOD_OWNER_SKIP_HEAVY_SCAN", "0")
    os.environ.setdefault("SHOOTER_VOD_OWNER_ANCHOR_PEAK", "0")
    if os.environ.get("SHOOTER_VOD_OWNER_EXEMPLARS", "1") == "1":
        os.environ.setdefault("HIGHLIGHT_CLIP_DISABLED", "0")
    lock = _feed_lock(game)
    if lock is None:
        return 0
    env = {**os.environ, **load_env(ENV_PATH)}
    for key in (
        "SHOOTER_VOD_FEED",
        "SHOOTER_VOD_FAST_PROBE",
        "SHOOTER_VOD_FULL_PASS",
        "SHOOTER_VOD_STAGE1_FAST",
        "SHOOTER_VOD_PREFER_RUSSIAN",
        "SHOOTER_VOD_SKIP_INTELLICLIP",
        "SHOOTER_VOD_MAX_PANN_PROBE",
        "HIGHLIGHT_MAX_STAGE1",
        "SHOOTER_VOD_OWNER_EXEMPLARS",
        "SHOOTER_VOD_OWNER_LABEL_CUTS",
        "SHOOTER_VOD_OWNER_SKIP_HEAVY_SCAN",
        "SHOOTER_VOD_OWNER_ANCHOR_PEAK",
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
