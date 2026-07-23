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


def _reliable_mode(game: str = "") -> bool:
    """Ship videos, not Telegram error spam. Default ON for PUBG/Standoff."""
    raw = os.environ.get("SHOOTER_VOD_RELIABLE")
    if raw is None or str(raw).strip() == "":
        return (game or "").strip().lower() in {"pubg", "standoff", ""}
    return str(raw).strip() not in {"0", "false", "False", "no"}


def _apply_reliable_runtime(game: str) -> None:
    """Mute owner noise + prefer singles + hard exhaust. Idempotent per process."""
    if not _reliable_mode(game):
        return
    defaults = {
        "SHOOTER_VOD_ADAPTIVE_NOTIFY": "0",
        "SHOOTER_VOD_EXHAUST_NOTIFY": "0",
        "SHOOTER_VOD_FAST_SKIP_NOTIFY": "0",
        "SHOOTER_VOD_DISCOVERY_MISS_NOTIFY": "0",
        "SHOOTER_VOD_DOWNLOAD_NOTIFY": "0",
        "SHOOTER_VOD_METRO_REJECT_NOTIFY": "0",
        "SHOOTER_VOD_MONTAGE": "0",
        "SHOOTER_VOD_SEND_ONE": "1",
        "SHOOTER_VOD_MAX_ZERO_ATTEMPTS": "1",
        "SHOOTER_VOD_PRESEND_EXHAUST_AFTER": "1",
        # Soften must be able to lower the live PANNs bar (was stuck at import 0.25).
        "HIGHLIGHT_PANN_FIXED": "0",
        # Ship clips on first pass — do not wait for a zero-streak to soften.
        "HIGHLIGHT_PANN_GUN_MIN": "0.10",
        "HIGHLIGHT_PANN_INFERENCE_FLOOR": "0.08",
        "HIGHLIGHT_PANN_PREFILTER_MIN": "0.06",
        "PUBG_COMBAT_PANN_MIN": "0.10",
        "PUBG_COMBAT_FRAMES_REQUIRED": "2",
        "PUBG_PANNS_TRUST_MIN": "0.28",
        "VIRAL_SEGMENT_HOOK_MIN": "0.06",
        "VIRAL_COMBAT_HOOK_MIN": "0.04",
        "SHOOTER_VOD_FAST_PANN_MIN": "0.06",
        "SHOOTER_VOD_FAST_PROBE_MAX": "10",
        "SHOOTER_VOD_MIN_CLIP_SCORE": "0.02",
        "SMART_PUBG_MIN_GUNFIRE_DENSITY": "0.036",
        # Never download multi-hour streams (flat search often hides duration).
        "SHOOTER_VOD_MAX_SEC": "1200",
        "SHOOTER_VOD_MIN_SEC": "300",
        "SHOOTER_VOD_PREFER_MIN_SEC": "420",
        "SHOOTER_VOD_PREFER_MAX_SEC": "1080",
    }
    if game == "pubg":
        defaults["PUBG_VOD_MONTAGE"] = "0"
    force_keys = {
        "SHOOTER_VOD_ADAPTIVE_NOTIFY",
        "SHOOTER_VOD_EXHAUST_NOTIFY",
        "SHOOTER_VOD_DISCOVERY_MISS_NOTIFY",
        "SHOOTER_VOD_METRO_REJECT_NOTIFY",
        "SHOOTER_VOD_MONTAGE",
        "PUBG_VOD_MONTAGE",
        "SHOOTER_VOD_MAX_ZERO_ATTEMPTS",
        "HIGHLIGHT_PANN_FIXED",
        "HIGHLIGHT_PANN_GUN_MIN",
        "HIGHLIGHT_PANN_INFERENCE_FLOOR",
        "PUBG_COMBAT_PANN_MIN",
        "PUBG_COMBAT_FRAMES_REQUIRED",
        "SHOOTER_VOD_FAST_PANN_MIN",
        "SHOOTER_VOD_MAX_SEC",
        "SHOOTER_VOD_MIN_SEC",
        "PUBG_PANNS_TRUST_MIN",
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)
        # Reliable mode forces these even if env had spam/montage/strict bars on.
        if key in force_keys:
            os.environ[key] = value


def _hard_finish_vod(
    game: str,
    state: dict,
    vod: Path,
    *,
    vid: str,
    reason: str,
    delete_file: bool = True,
) -> None:
    """Never rescan a finished/dead VOD — stop encode/notify loops forever."""
    entry = _ensure_registry_entry(state, vod, vid=vid)
    entry["exhausted"] = True
    entry["reject_reason"] = reason
    entry["path"] = str(vod)
    _save_state(game, state)
    if delete_file and os.environ.get("SHOOTER_VOD_DELETE_EXHAUSTED", "1") == "1":
        try:
            if vod.exists():
                vod.unlink()
                log.info("deleted exhausted vod=%s reason=%s", vod.name, reason)
        except OSError as exc:
            log.warning("delete exhausted failed %s: %s", vod.name, exc)


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


def _shooter_vod_min_sec() -> float:
    return float(os.environ.get("SHOOTER_VOD_MIN_SEC", os.environ.get("MLBB_VOD_MIN_SEC", "180")))


def _shooter_vod_max_sec() -> float:
    # Hard cap — flat-playlist often omits duration and used to let multi-hour streams through.
    return float(os.environ.get("SHOOTER_VOD_MAX_SEC", os.environ.get("MLBB_VOD_MAX_SEC", "1200")))


def _shooter_duration_ok(dur: float) -> bool:
    if dur <= 0:
        return False
    return _shooter_vod_min_sec() <= float(dur) <= _shooter_vod_max_sec()


def _probe_youtube_duration(url: str, env: dict[str, str]) -> float:
    from youtube_download import run_ytdlp, ytdlp_cmd, ytdlp_extra_args

    cmd = ytdlp_cmd(env) + ["--print", "%(duration)s", "--no-playlist", url]
    cmd += ytdlp_extra_args(env)
    proc = run_ytdlp(cmd, env, timeout=60, label="probe-duration")
    if proc.returncode != 0:
        return 0.0
    try:
        return float((proc.stdout or "").strip().splitlines()[0])
    except (ValueError, IndexError):
        return 0.0


def _discover_candidates(game: str, env: dict[str, str], used: set[str]) -> list[dict]:
    from youtube_download import (
        _ytdlp_is_403,
        fallback_search_targets,
        run_ytdlp,
        ytdlp_cmd,
        ytdlp_extra_args,
    )

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
    limit = int(params.get("limit", 20) or 20)
    attempts = params.get("attempts") or [[u] for u in params.get("urls", [])]
    for targets in attempts:
        targets = list(targets or [])
        # Ensure ytsearch fallback exists even for older prefs without attempts.
        if targets and targets[0].startswith("http"):
            for alt in fallback_search_targets(targets[0], limit=limit):
                if alt not in targets:
                    targets.append(alt)
        got = False
        for url in targets:
            cmd = ytdlp_cmd(env) + [
                "--flat-playlist",
                "--playlist-end",
                str(limit),
                "--print",
                "%(id)s|%(title)s|%(duration)s|%(uploader)s",
                url,
            ]
            cmd += ytdlp_extra_args(env)
            proc = run_ytdlp(cmd, env, timeout=120, label=f"search-{game}")
            if proc.returncode != 0:
                err = (proc.stderr or "")[:200]
                if _ytdlp_is_403(proc):
                    log.warning("search 403 %s — trying fallback", url[:120])
                    time.sleep(float(env.get("YTDLP_403_RETRY_DELAY", "4")))
                else:
                    log.warning("search failed %s: %s", url, err)
                continue
            if url.startswith("ytsearch"):
                log.info("search ok via ytsearch game=%s", game)
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
                    raw = parts[2] if len(parts) > 2 else ""
                    dur = float(raw) if raw not in {"", "NA", "None"} else 0.0
                except ValueError:
                    dur = 0.0
                if dur > 0 and not _shooter_duration_ok(dur):
                    continue
                # Duration unknown (flat playlist) — keep candidate; probe before download.
                out.append(
                    {
                        "id": vid,
                        "title": title[:120],
                        "url": f"https://www.youtube.com/watch?v={vid}",
                        "duration": dur,
                        "uploader": parts[3][:60] if len(parts) > 3 else "",
                    }
                )
            got = True
            break
        time.sleep(float(params.get("delay", 6)) * (1.5 if not got else 1.0))
    return out


def _download_vod(game: str, pick: dict, env: dict[str, str]) -> Path | None:
    from youtube_download import download_one

    inbox = _paths(game)["inbox"]
    inbox.mkdir(parents=True, exist_ok=True)
    dur = float(pick.get("duration") or 0)
    if not _shooter_duration_ok(dur):
        dur = _probe_youtube_duration(str(pick.get("url") or ""), env)
        pick["duration"] = dur
    if not _shooter_duration_ok(dur):
        log.info(
            "skip download id=%s duration=%.0fs (need %.0f–%.0f)",
            pick.get("id"),
            dur,
            _shooter_vod_min_sec(),
            _shooter_vod_max_sec(),
        )
        return None
    # yt-dlp hard filter — belt and suspenders if metadata lies until fetch.
    dl_env = dict(env)
    dl_env["YTDLP_MATCH_FILTER"] = (
        f"duration > {_shooter_vod_min_sec()} & duration < {_shooter_vod_max_sec()}"
    )
    try:
        path = download_one(str(pick["url"]), inbox, dl_env)
        return path
    except Exception as exc:
        log.warning("download failed %s: %s", pick.get("id"), exc)
        return None


def _validate_shooter_presend(game: str, vod: Path, row: dict, rendered: Path) -> tuple[bool, str, dict]:
    profile = _profile(game)
    start = float(row.get("peak_start", row.get("start", 0)))
    dur = _ffprobe_duration(rendered)
    if dur <= 0:
        dur = float(row.get("duration", 15))
    return _validate_shooter_window(game, vod, start, dur, profile=profile)


def _validate_shooter_window(
    game: str,
    vod: Path,
    start: float,
    dur: float,
    *,
    profile: str | None = None,
) -> tuple[bool, str, dict]:
    """Cheap combat/metro check on source timestamps — run BEFORE ffmpeg encode."""
    profile = profile or _profile(game)
    vod_dur = _ffprobe_duration(vod)
    if vod_dur > 0 and start + max(dur, 1.0) > vod_dur + 0.5:
        return False, f"clip_past_eof start={start:.1f} dur={dur:.1f} vod={vod_dur:.1f}", {}
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
    from shooter_vod_montage import montage_enabled, pick_montage_rows

    if montage_enabled(game) and len(to_send) >= 2:
        picked = pick_montage_rows(to_send)
        if len(picked) >= 2:
            return _send_montage_batch(game, token, chat_id, vod, picked, sig)

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
        clip = row["clip"]
        try:
            from shooter_vod_montage import apply_run_trim_to_clip

            clip = apply_run_trim_to_clip(clip, vod, game=game)
        except Exception:
            pass
        peak = float(row.get("peak_start", row.get("start", 0)))
        plan_dur = float(clip.get("input_duration") or row.get("duration") or 15)
        pre_ok, pre_reason, _pre = _validate_shooter_window(game, vod, peak, plan_dur)
        if not pre_ok:
            log.warning("presend PRE-REJECT (skip encode) %s: %s", sid, pre_reason)
            continue
        if not render_single_segment(vod, clip, out):
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


def _send_montage_batch(
    game: str,
    token: str,
    chat_id: str,
    vod: Path,
    to_send: list[dict],
    sig: str,
) -> int:
    """One Telegram video = xfade of 2–4 fight windows from the same VOD."""
    import tempfile

    from shooter_vod_montage import (
        apply_run_trim_to_clip,
        build_montage_id,
        cleanup_temps,
        concat_rendered_parts,
    )
    from smart_video_editor import ffprobe_duration as _probe_part_dur

    ok_cycle, cycle_reason = can_send_for_game(game, 1)
    if not ok_cycle:
        log.info("cycle block game=%s reason=%s", game, cycle_reason)
        return 0

    vid = vod_youtube_id(vod)
    mid = build_montage_id(vid, to_send)
    seg_root = _paths(game)["segments"]
    seg_root.mkdir(parents=True, exist_ok=True)
    out = seg_root / f"seg_{mid}.mp4"
    temps: list[Path] = []
    try:
        gated_rows: list[dict] = []
        gated_parts: list[Path] = []
        gated_durs: list[float] = []
        for row in to_send:
            clip = apply_run_trim_to_clip(dict(row["clip"]), vod, game=game)
            peak = float(row.get("peak_start", row.get("start", 0)))
            plan_dur = float(clip.get("input_duration") or row.get("duration") or 15)
            pre_ok, pre_reason, _pre = _validate_shooter_window(game, vod, peak, plan_dur)
            if not pre_ok:
                log.warning(
                    "montage part PRE-REJECT (skip encode) %s: %s",
                    row.get("segment_id"),
                    pre_reason,
                )
                continue
            part = Path(tempfile.mkstemp(suffix=".part.mp4")[1])
            temps.append(part)
            if not render_single_segment(vod, clip, part):
                log.warning("montage part render fail %s", row.get("segment_id"))
                continue
            ok, reason, _rep = _validate_shooter_presend(game, vod, {**row, "clip": clip}, part)
            if not ok:
                log.warning("montage part REJECT %s: %s", row.get("segment_id"), reason)
                continue
            dur = float(clip.get("input_duration") or 0)
            if dur < 1:
                dur = float(_probe_part_dur(part) or 0)
            gated_rows.append({**row, "clip": clip, "fight_dur": dur})
            gated_parts.append(part)
            gated_durs.append(dur)
        if len(gated_rows) < 2:
            log.warning("montage aborted — fewer than 2 parts (%s); fallback single", len(gated_rows))
            if gated_rows:
                # Temporarily disable montage to avoid recursion.
                old = os.environ.get("SHOOTER_VOD_MONTAGE")
                old_g = os.environ.get(f"{game.upper()}_VOD_MONTAGE")
                os.environ["SHOOTER_VOD_MONTAGE"] = "0"
                if old_g is not None:
                    os.environ[f"{game.upper()}_VOD_MONTAGE"] = "0"
                try:
                    return _send_batch(game, token, chat_id, vod, gated_rows[:1], sig)
                finally:
                    if old is None:
                        os.environ.pop("SHOOTER_VOD_MONTAGE", None)
                    else:
                        os.environ["SHOOTER_VOD_MONTAGE"] = old
                    if old_g is None:
                        os.environ.pop(f"{game.upper()}_VOD_MONTAGE", None)
                    else:
                        os.environ[f"{game.upper()}_VOD_MONTAGE"] = old_g
            return 0

        if not concat_rendered_parts(gated_parts, gated_durs, out):
            log.warning("montage concat fail vod=%s", vid)
            return 0

        peaks = [int(float(r.get("peak_start", r["start"]))) for r in gated_rows]
        seg_dur = _ffprobe_duration(out)
        label = "Metro склейка" if game == "pubg" else "склейка"
        caption = (
            f"{game.upper()} {label} #{mid}\n"
            f"🎯 {len(gated_rows)} боя @ {peaks}\n"
            f"{vid} | {seg_dur:.0f}с\n"
            f"✓ montage (anti-run trim)\n"
            f"👍 Ок / 👎 Не ок"
        )
        if not send_video(
            token,
            chat_id,
            out,
            caption,
            seg_id=mid,
            record_learning=False,
            reply_markup=keyboard(game, mid),
            cycle_game=game,
        ):
            send_message(token, chat_id, f"{caption}\n(файл не отправился)")
            return 0
        upsert_segment(
            game,
            {
                "segment_id": mid,
                "path": str(out),
                "vod": str(vod),
                "vod_id": vid,
                "start": gated_rows[0]["start"],
                "duration": seg_dur,
                "peak_start": gated_rows[0].get("peak_start", gated_rows[0]["start"]),
                "score": max(float(r.get("score") or 0) for r in gated_rows),
                "montage_parts": [r["segment_id"] for r in gated_rows],
                "sig": sig,
                "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        part_ids = [r["segment_id"] for r in gated_rows] + [mid]
        mark_feed_sent(game, part_ids)
        log.info("montage sent game=%s id=%s parts=%s dur=%.0f", game, mid, len(gated_rows), seg_dur)
        st = stats(game)
        send_message(
            token,
            chat_id,
            f"✅ {game.upper()} montage parts={len(gated_rows)} | 👍{st['feedback_yes']} 👎{st['feedback_no']}",
        )
        return 1
    finally:
        cleanup_temps(temps)


def _inbox_order_key(mp4: Path, registry: list[dict]) -> tuple:
    """Prefer rich leftover pools, then unscanned; Metro + Russian titles before others."""
    from pubg_metro_royale_gate import title_metro_hint
    from shooter_vod_montage import vod_richness_rank
    from youtube_game_prefs import russian_score

    entry = next((r for r in registry if r.get("path") == str(mp4)), None) or {}
    scanned = float(entry.get("last_scan_at") or 0)
    title = str(entry.get("title") or "")
    metro_prio = 0 if title_metro_hint(title) else 1
    ru = russian_score({"title": title, "uploader": str(entry.get("uploader") or "")})
    ru_prio = 0 if ru >= 0.10 else (1 if ru >= 0.05 else 2)
    fast_fail = 1 if str(entry.get("reject_reason") or "").startswith("fast_panns_0") else 0
    rich = vod_richness_rank(entry)
    return (rich, 1 if scanned else 0, fast_fail, metro_prio, ru_prio, scanned, mp4.stat().st_mtime)


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
    from shooter_vod_montage import (
        apply_run_trim_to_clip,
        montage_collect_env,
        montage_enabled,
        montage_max_clips,
        pick_montage_rows,
    )

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
    montage_on = montage_enabled(game)

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

    with montage_collect_env(game):
        if entry and pool_cache_valid(entry):
            pool = minimal_pool_from_entry(entry)
            log.info("reuse cached peak pool vod=%s peaks=%s", vod.name, len(pool))
        else:
            pool = discover_strict_candidates(vod, profile, sig, blocked_ids)
    pool_peaks = peaks_from_pool(pool)
    if not pool:
        log.info("no candidates %s", vod.name)
        if entry is not None:
            record_vod_scan(entry, sent=0, pool_peaks=[], blocked=False)
        return 0

    probe_limit = int(os.environ.get("MLBB_VOD_PROBE_LIMIT", "24"))
    if montage_on:
        probe_limit = max(probe_limit, montage_max_clips() * 4)
    skip_peaks: set[float] = set()
    peak_tries = 0
    gate = _adaptive_gate(game)
    max_tries = max_peak_tries(soften_level, game=game, soft_max_fn=gate.soft_max_peak_tries)
    min_clip = float(os.environ.get("SHOOTER_VOD_MIN_CLIP_SCORE", "0.03"))
    owner_exemplars = os.environ.get("SHOOTER_VOD_OWNER_EXEMPLARS", "1") == "1"

    while peak_tries < max_tries:
        rows: list[dict] = []
        with montage_collect_env(game):
            for clip in pool[:probe_limit]:
                peak = float(clip.get("start", 0))
                if any(abs(peak - s) <= 4.0 for s in skip_peaks):
                    continue
                if _peak_too_close(peak, used_peaks, seg_gap):
                    continue
                hm = clip.get("highlight_metrics") or {}
                clip_score = float(hm.get("clip_score") or clip.get("score") or 0.0)
                if owner_exemplars and clip_score < min_clip and not montage_on:
                    continue
                if owner_exemplars and montage_on:
                    mont_min = float(os.environ.get("SHOOTER_VOD_MONTAGE_MIN_CLIP_SCORE", "0.02"))
                    if clip_score < mont_min:
                        continue
                start = max(0.0, peak - lead)
                sid = segment_id(vid, start)
                if sid in blocked_ids:
                    continue
                clip_out = {**clip, "start": start, "peak_start": peak}
                try:
                    clip_out = apply_run_trim_to_clip(clip_out, vod, game=game)
                except Exception:
                    pass
                rows.append(
                    {
                        "segment_id": sid,
                        "start": start,
                        "peak_start": peak,
                        "score": float(clip.get("score", 0)),
                        "clip_score": clip_score,
                        "fight_dur": float(clip_out.get("input_duration") or 0),
                        "clip": clip_out,
                    }
                )
                if montage_on and len(rows) >= max(montage_max_clips() * 2, 6):
                    break
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
                record_vod_scan(entry, sent=0, pool_peaks=pool_peaks, blocked=blocked, pool=pool)
            return 0
        rows.sort(key=lambda r: float(r.get("score", 0)), reverse=True)
        batch = rows
        if montage_on:
            picked = pick_montage_rows(rows)
            if picked:
                log.info(
                    "montage pick game=%s vod=%s n=%s peaks=%s",
                    game,
                    vod.name,
                    len(picked),
                    [int(float(r.get("peak_start", r["start"]))) for r in picked],
                )
                batch = picked
            else:
                batch = rows[:1]
        else:
            batch = rows[:1]
        n = _send_batch(game, token, chat_id, vod, batch, sig)
        if n > 0:
            if entry is not None:
                record_vod_scan(entry, sent=n, pool_peaks=pool_peaks, blocked=False, pool=pool)
            return n
        # Reject path: skip first attempted peak(s) and retry.
        for row in batch:
            skip_peaks.add(round(float(row.get("peak_start", row["start"])), 1))
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


def _ensure_registry_entry(
    state: dict,
    vod: Path,
    *,
    vid: str,
    title: str = "",
    entry: dict | None = None,
) -> dict:
    """Return registry row for vod, appending a new one when missing."""
    if entry is not None:
        return entry
    found = _vod_registry_entry(state, vod)
    if found is not None:
        return found
    row = {
        "id": vid,
        "path": str(vod),
        "title": title,
        "exhausted": False,
    }
    state.setdefault("vods", []).append(row)
    return row


def _mark_fast_skip_exhausted(
    state: dict,
    vod: Path,
    *,
    vid: str,
    title: str,
    fast_reason: str,
    entry: dict | None,
) -> dict:
    row = _ensure_registry_entry(state, vod, vid=vid, title=title, entry=entry)
    row["reject_reason"] = fast_reason
    row["exhausted"] = True
    record_vod_scan(row, sent=0, pool_peaks=[], blocked=False)
    return row


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
            _mark_fast_skip_exhausted(
                state, vod, vid=vid, title=title, fast_reason=fast_reason, entry=entry
            )
            _save_state(game, state)
            if _reliable_mode(game):
                _hard_finish_vod(
                    game,
                    state,
                    vod,
                    vid=vid,
                    reason=fast_reason,
                    delete_file=True,
                )
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
            _mark_fast_skip_exhausted(
                state, vod, vid=vid, title=title, fast_reason=fast_reason, entry=entry
            )
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
            _mark_fast_skip_exhausted(
                state, vod, vid=vid, title=title, fast_reason=fast_reason, entry=entry
            )
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
    _save_state(game, state)

    entry = _ensure_registry_entry(state, vod, vid=vid, title=title, entry=entry)
    if sent > 0:
        # One successful clip from a VOD is enough — do not rescan into notify/encode loops.
        _hard_finish_vod(game, state, vod, vid=vid, reason="sent_ok", delete_file=True)
    elif _reliable_mode(game):
        zeros = int(entry.get("zero_send_attempts") or 0) + 1
        entry["zero_send_attempts"] = zeros
        max_zero = max(1, int(os.environ.get("SHOOTER_VOD_MAX_ZERO_ATTEMPTS", "1")))
        if zeros >= max_zero or should_mark_vod_exhausted(entry):
            _hard_finish_vod(
                game,
                state,
                vod,
                vid=vid,
                reason=str(entry.get("reject_reason") or "zero_send_reliable"),
                delete_file=True,
            )
        else:
            _save_state(game, state)

    notify_exhaust = os.environ.get(
        "SHOOTER_VOD_EXHAUST_NOTIFY", os.environ.get("MLBB_VOD_EXHAUST_NOTIFY", "1")
    ) == "1"
    if sent == 0 and notify_exhaust and not _reliable_mode(game):
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
    _apply_reliable_runtime(game)
    log.info("shooter feed start game=%s rev=%s reliable=%s", game, VOD_PIPELINE_REV, int(_reliable_mode(game)))
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
        vid_inbox = vod_youtube_id(mp4)
        entry = next(
            (
                r
                for r in registry
                if r.get("path") == str(mp4) or r.get("id") == vid_inbox
            ),
            None,
        )
        if entry and entry.get("exhausted"):
            continue
        if should_skip_vod_rescan(entry, game=game):
            log.info("skip scan cooldown vod=%s", mp4.name)
            continue
        if _ffprobe_duration(mp4) < _shooter_vod_min_sec():
            continue
        inbox_dur = _ffprobe_duration(mp4)
        if not _shooter_duration_ok(inbox_dur):
            log.info("skip inbox vod=%s duration=%.0fs outside shooter window", mp4.name, inbox_dur)
            if _reliable_mode(game):
                _hard_finish_vod(
                    game,
                    state,
                    mp4,
                    vid=vid_inbox,
                    reason=f"duration_out={inbox_dur:.0f}",
                    delete_file=True,
                )
            continue
        if entry is None:
            entry = {
                "id": vid_inbox,
                "path": str(mp4),
                "title": "",
                "exhausted": False,
            }
            registry.append(entry)
        else:
            entry["path"] = str(mp4)
            entry["id"] = entry.get("id") or vid_inbox
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

    candidates = _discover_candidates(game, env, used)
    if not candidates:
        if os.environ.get("SHOOTER_VOD_DISCOVERY_MISS_NOTIFY", "0") == "1":
            send_message(token, chat_id, f"⚠️ Не нашёл новый {game.upper()} стрим. Повторю позже.")
        else:
            log.info("discovery miss game=%s (notify muted)", game)
        try:
            from daily_game_cycle import maybe_skip_on_discovery_miss

            if maybe_skip_on_discovery_miss(game):
                log.warning(
                    "daily cycle skip game=%s after discovery misses — advance to next game",
                    game,
                )
                if token and chat_id and os.environ.get("DAILY_GAME_SKIP_NOTIFY", "1") == "1":
                    send_message(
                        token,
                        chat_id,
                        f"⏭️ {game.upper()}: YouTube/поиск пустой несколько раз подряд — "
                        f"пропускаю квоту и перехожу к следующей игре.",
                    )
        except Exception as exc:
            log.warning("discovery-miss skip failed: %s", exc)
        print(f"pipeline done sent=0 vods=0 game={game}")
        return 0

    try:
        from daily_game_cycle import clear_discovery_miss

        clear_discovery_miss(game)
    except Exception:
        pass

    if game in ("pubg", "standoff"):
        from youtube_shooter_vod_prefs import rank_discovery_candidates

        ranked = rank_discovery_candidates(game, candidates)
        from youtube_shooter_vod_prefs import pick_discovery_candidate

        # Build an ordered try-list: preferred pick first, then other ranked.
        primary = pick_discovery_candidate(game, candidates)
        ordered: list[dict] = []
        if primary:
            ordered.append(primary)
        for cand in ranked:
            if primary and cand.get("id") == primary.get("id"):
                continue
            ordered.append(cand)
    else:
        ordered = list(candidates)

    vod = None
    pick: dict | None = None
    max_tries = max(1, int(os.environ.get("SHOOTER_VOD_DISCOVERY_TRY", "8")))
    for cand in ordered[:max_tries]:
        cid = str(cand.get("id") or "")
        if not cid or cid in used:
            continue
        if os.environ.get("SHOOTER_VOD_DOWNLOAD_NOTIFY", "0") == "1":
            send_message(token, chat_id, f"📥 Качаю {game.upper()} VOD с YouTube…")
        else:
            log.info("downloading %s vod id=%s (notify muted)", game, cid)
        path = _download_vod(game, cand, env)
        if path:
            vod = path
            pick = cand
            break
        # Duration/title rejects must not be retried forever.
        used.add(cid)
        registry.append(
            {
                "id": cid,
                "path": "",
                "title": cand.get("title", ""),
                "exhausted": True,
                "reject_reason": f"download_skip_dur={cand.get('duration') or 0}",
            }
        )
        state["vods"] = registry
        state["used_youtube_ids"] = sorted(used)
        _save_state(game, state)
        log.info("marked used after download skip id=%s dur=%s", cid, cand.get("duration"))

    if not vod or pick is None:
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
            exhausted = True if _reliable_mode(game) else _pubg_metro_should_exhaust(
                str(pick.get("title") or ""), streak_dl
            )
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
            if exhausted and os.environ.get("SHOOTER_VOD_DELETE_EXHAUSTED", "1") == "1":
                try:
                    if vod.exists():
                        vod.unlink()
                        log.info("deleted metro-reject vod=%s", vod.name)
                except OSError as exc:
                    log.warning("delete metro-reject failed %s: %s", vod.name, exc)
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
    _apply_reliable_runtime(game)
    os.environ.setdefault("HIGHLIGHT_HEATMAP", "0")
    os.environ.setdefault("SHOOTER_VOD_FEED", "1")
    os.environ.setdefault("SHOOTER_VOD_FAST_PROBE", "1")
    os.environ.setdefault("SHOOTER_VOD_PREFER_RUSSIAN", "1")
    os.environ.setdefault("SHOOTER_VOD_SKIP_INTELLICLIP", "1")
    os.environ.setdefault("SHOOTER_VOD_MAX_PANN_PROBE", "24")
    os.environ.setdefault("HIGHLIGHT_MAX_STAGE1", "32")
    os.environ.setdefault("SHOOTER_VOD_RELIABLE", "1")
    _apply_reliable_runtime(game)
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
