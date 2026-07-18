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
    record_zero_send_streak,
    scan_zero_detail,
    should_force_exhaust_after_retries,
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


def _shooter_vod_min_sec() -> float:
    """PUBG clutch compilations are often 2–4 min — don't force MLBB's 3 min floor."""
    return float(os.environ.get("SHOOTER_VOD_MIN_SEC", "120"))


def _shooter_vod_max_sec() -> float:
    """Allow longer Metro raids than MLBB's 20 min default (still below multi-hour streams).

    Do not fall back to MLBB_VOD_MAX_SEC — that 20m cap starves PUBG discovery
    (typical solo-vs-squad Metro VODs are 20–35 minutes).
    """
    return float(os.environ.get("SHOOTER_VOD_MAX_SEC", "2400"))


def _shooter_length_ok(dur: float) -> bool:
    return _shooter_vod_min_sec() <= float(dur) <= _shooter_vod_max_sec()


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
    """Return the registry row for this VOD.

    Prefer an exact path match. A stale id-only duplicate (exhausted, path="")
    must not steal scan/exhaust updates from the live inbox file row — that bug
    caused infinite rescans of the same PUBG VOD while the exhausted sibling
    was the only one marked spent.
    """
    vod_path = str(vod)
    vid = vod_youtube_id(vod)
    path_hit = None
    id_live = None
    id_any = None
    for entry in state.get("vods") or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("path") or "") == vod_path:
            path_hit = entry
            break
        if vid and str(entry.get("id") or "") == vid:
            if id_any is None:
                id_any = entry
            if str(entry.get("path") or "").strip() and id_live is None:
                id_live = entry
    return path_hit or id_live or id_any


def _vod_registry_entries_for_id(state: dict, youtube_id: str) -> list[dict]:
    vid = (youtube_id or "").strip()
    if not vid:
        return []
    return [
        e
        for e in (state.get("vods") or [])
        if isinstance(e, dict) and str(e.get("id") or "") == vid
    ]


def _reject_reason_permanently_spent(reason: str, entry: dict | None = None) -> bool:
    r = (reason or "").strip()
    if r in {"all_peaks_blocked", "peaks_spent"}:
        return True
    if entry and entry.get("last_scan_blocked"):
        return True
    if r == "no_combat_peaks":
        peaks = (entry or {}).get("last_pool_peaks")
        # Had peaks that are all spent/blocked — not a soft empty-pool miss.
        if peaks is not None and len(peaks) > 0:
            return True
    return False


def _youtube_id_permanently_spent(state: dict, youtube_id: str) -> bool:
    """True when any registry row says this YouTube id has nothing left to mine."""
    for entry in _vod_registry_entries_for_id(state, youtube_id):
        if not entry.get("exhausted"):
            continue
        if entry.get("file_deleted"):
            return True
        if _reject_reason_permanently_spent(str(entry.get("reject_reason") or ""), entry):
            return True
    return False


def _mark_siblings_exhausted(
    state: dict,
    *,
    youtube_id: str,
    reject_reason: str = "",
    primary: dict | None = None,
) -> list[dict]:
    """Mark every registry row for this youtube id exhausted; sync spent flags."""
    touched: list[dict] = []
    now = time.time()
    src = primary or {}
    reason = (reject_reason or str(src.get("reject_reason") or "")).strip()
    for entry in _vod_registry_entries_for_id(state, youtube_id):
        entry["exhausted"] = True
        entry.setdefault("exhausted_at", now)
        if reason:
            entry["reject_reason"] = reason
        if src.get("last_scan_blocked") or reason in {"all_peaks_blocked", "peaks_spent"}:
            entry["last_scan_blocked"] = True
        if src.get("last_pool_peaks") is not None and entry.get("last_pool_peaks") is None:
            entry["last_pool_peaks"] = src["last_pool_peaks"]
        touched.append(entry)
    return touched


def _vod_title(state: dict, vod: Path) -> str:
    entry = _vod_registry_entry(state, vod)
    return str((entry or {}).get("title") or "")


def _metro_reject_is_hard(reason: str) -> bool:
    """Classic outdoor / map UI rejects must never be softened away."""
    r = reason or ""
    return "classic_outdoor_sky" in r or "classic_map_ui" in r


def _pubg_metro_should_exhaust(title: str, streak: int, reason: str = "") -> bool:
    """Permanently skip VOD when metro reject cannot be overridden."""
    from pubg_metro_royale_gate import title_is_training_junk, title_metro_hint
    from shooter_vod_adaptive_gate import soften_level

    if _metro_reject_is_hard(reason) or title_is_training_junk(title):
        return True
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
    from pubg_metro_royale_gate import title_is_training_junk, vod_looks_metro_royale
    from shooter_vod_adaptive_gate import soften_level

    if title_is_training_junk(title):
        return False, "metro_training_junk_title"
    ok, reason = vod_looks_metro_royale(vod, title=title or None)
    if ok:
        return True, reason
    # Soft L2+ may bypass ambiguous not_metro — never classic outdoor/map.
    if _metro_reject_is_hard(reason):
        return False, reason
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

    limit = int(params.get("limit") or 20)
    out: list[dict] = []
    seen: set[str] = set()
    skipped: dict[str, int] = {}
    for url in params.get("urls", []):
        # Tab delimiter: titles often contain "|" which broke field parsing and
        # dropped "Metro Royale" into the duration column.
        cmd = ytdlp_cmd(env) + [
            "--flat-playlist",
            "--playlist-end",
            str(limit),
            "--print",
            "%(id)s\t%(title)s\t%(duration)s\t%(uploader)s",
            url,
        ]
        cmd += ytdlp_extra_args(env)
        proc = run_ytdlp(cmd, env, timeout=120, label=f"search-{game}")
        if proc.returncode != 0:
            log.warning("search failed %s: %s", url, (proc.stderr or "")[:200])
            skipped["search_fail"] = skipped.get("search_fail", 0) + 1
            continue
        for line in (proc.stdout or "").splitlines():
            parts = line.split("\t") if "\t" in line else line.split("|", 3)
            if len(parts) < 2:
                skipped["parse"] = skipped.get("parse", 0) + 1
                continue
            vid, title = parts[0][:11], parts[1].replace("\n", " ").strip()
            uploader = (parts[3] if len(parts) > 3 else "")[:60]
            if len(vid) != 11:
                skipped["bad_id"] = skipped.get("bad_id", 0) + 1
                continue
            if vid in used or vid in seen:
                skipped["used_or_dup"] = skipped.get("used_or_dup", 0) + 1
                continue
            try:
                ok_title = title_ok_fn(game, title, uploader=uploader)  # type: ignore[call-arg]
            except TypeError:
                ok_title = title_ok_fn(game, title)
            if not ok_title:
                skipped["title"] = skipped.get("title", 0) + 1
                continue
            raw_dur = parts[2].strip() if len(parts) > 2 else ""
            try:
                dur = float(raw_dur) if raw_dur and raw_dur.upper() not in {"NA", "NONE", "NULL"} else 0.0
            except ValueError:
                dur = 0.0
            # Unknown duration (flat search) is kept; known out-of-window is dropped.
            if dur > 0:
                length_ok = (
                    _shooter_length_ok(dur)
                    if game in ("pubg", "standoff")
                    else _vod_length_ok(Path("x.mp4"), dur)
                )
                if not length_ok:
                    skipped["duration"] = skipped.get("duration", 0) + 1
                    continue
            seen.add(vid)
            out.append(
                {
                    "id": vid,
                    "title": title[:120],
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "duration": dur,
                    "uploader": uploader,
                }
            )
        time.sleep(float(params.get("delay", 6)))
    if game in ("pubg", "standoff"):
        from youtube_shooter_vod_prefs import rank_discovery_candidates

        out = rank_discovery_candidates(game, out)
    if skipped:
        log.info(
            "discovery game=%s mode=%s raw_kept=%s skipped=%s",
            game,
            params.get("filter_mode"),
            len(out),
            skipped,
        )
    return out


def _cleanup_exhausted_entry(game: str, entry: dict | None, state: dict) -> None:
    if not entry:
        return
    entry.setdefault("exhausted_at", time.time())
    vid = str(entry.get("id") or "")
    if vid:
        _mark_siblings_exhausted(
            state,
            youtube_id=vid,
            reject_reason=str(entry.get("reject_reason") or ""),
            primary=entry,
        )
    try:
        from vod_inbox_cleanup import cleanup_after_exhaust

        # Prefer the sibling that still points at a live inbox path.
        target = entry
        for sibling in _vod_registry_entries_for_id(state, vid):
            if str(sibling.get("path") or "").strip():
                if entry.get("reject_reason"):
                    sibling["reject_reason"] = entry["reject_reason"]
                if entry.get("last_scan_blocked"):
                    sibling["last_scan_blocked"] = True
                sibling["exhausted"] = True
                sibling.setdefault("exhausted_at", entry.get("exhausted_at") or time.time())
                target = sibling
                break
        cleanup_after_exhaust(game, target, state=state)
    except Exception:
        log.exception("inbox cleanup after exhaust failed game=%s id=%s", game, entry.get("id"))


def _notify_discovery_miss(game: str, token: str, chat_id: str, state: dict) -> None:
    """Throttle repeated «не нашёл» Telegram spam (default 30 min)."""
    gap = max(60, int(os.environ.get("VOD_DISCOVERY_MISS_NOTIFY_SEC", "1800")))
    last = float(state.get("last_discovery_miss_notify_at") or 0)
    now = time.time()
    if now - last < gap:
        log.info("discovery miss notify suppressed game=%s age=%.0fs", game, now - last)
        return
    send_message(token, chat_id, f"⚠️ Не нашёл новый {game.upper()} стрим. Повторю позже.")
    state["last_discovery_miss_notify_at"] = now
    _save_state(game, state)


def _probe_youtube_meta(url: str, env: dict[str, str]) -> tuple[str, float]:
    """Resolve full title + duration before download (flat search truncates titles)."""
    from youtube_download import run_ytdlp, ytdlp_cmd, ytdlp_extra_args

    cmd = ytdlp_cmd(env) + [
        "--skip-download",
        "--no-playlist",
        "--print",
        "%(title)s|%(duration)s",
        url,
    ]
    cmd += ytdlp_extra_args(env)
    proc = run_ytdlp(cmd, env, timeout=90, label="probe-meta")
    if proc.returncode != 0:
        return "", 0.0
    raw = (proc.stdout or "").strip().splitlines()
    if not raw:
        return "", 0.0
    line = raw[-1].strip()
    if "|" not in line:
        return line[:200], 0.0
    title, dur_s = line.rsplit("|", 1)
    try:
        dur = float(dur_s.strip()) if dur_s.strip() and dur_s.strip().upper() not in {"NA", "NONE"} else 0.0
    except ValueError:
        dur = 0.0
    return title.strip()[:200], dur if dur > 0 else 0.0


def _preflight_vod_pick(game: str, pick: dict, env: dict[str, str]) -> tuple[bool, str]:
    """Reject loot/learning titles and multi-hour streams before yt-dlp download."""
    title = str(pick.get("title") or "")
    dur = float(pick.get("duration") or 0)
    # Always refresh metadata when duration unknown or title looks truncated/weak.
    need_meta = dur <= 0 or len(title) < 24 or title.endswith("…") or title.endswith("...")
    if need_meta or game in ("pubg", "standoff"):
        full_title, full_dur = _probe_youtube_meta(str(pick.get("url") or ""), env)
        if full_title:
            title = full_title
            pick["title"] = full_title
        if full_dur > 0:
            dur = full_dur
            pick["duration"] = full_dur

    if game in ("pubg", "standoff"):
        from youtube_shooter_vod_prefs import title_ok

        if title and not title_ok(game, title):
            return False, "bad_title"
    elif game in EXTENDED_GAMES:
        from youtube_extended_vod_prefs import title_ok as ext_title_ok

        if title and not ext_title_ok(game, title):
            return False, "bad_title"

    if dur > 0:
        length_ok = (
            _shooter_length_ok(dur)
            if game in ("pubg", "standoff")
            else _vod_length_ok(Path("x.mp4"), dur)
        )
        if not length_ok:
            return False, f"vod_length={dur:.0f}s"
    return True, ""


def _download_vod(game: str, pick: dict, env: dict[str, str]) -> Path | None:
    from youtube_download import download_one

    inbox = _paths(game)["inbox"]
    inbox.mkdir(parents=True, exist_ok=True)
    try:
        path = download_one(str(pick["url"]), inbox, env)
    except Exception as exc:
        log.warning("download failed %s: %s", pick.get("id"), exc)
        return None
    if path is None:
        return None
    file_dur = _ffprobe_duration(path)
    if file_dur > 0:
        length_ok = (
            _shooter_length_ok(file_dur)
            if game in ("pubg", "standoff")
            else _vod_length_ok(path, file_dur)
        )
        if not length_ok:
            log.warning("delete overlong download id=%s dur=%.0fs", pick.get("id"), file_dur)
            pick["reject_reason"] = f"vod_length={file_dur:.0f}s"
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return None
    return path


def _validate_shooter_presend(game: str, vod: Path, row: dict, rendered: Path) -> tuple[bool, str, dict]:
    profile = _profile(game)
    start = float(row.get("peak_start", row.get("start", 0)))
    dur = _ffprobe_duration(rendered)
    if dur <= 0:
        dur = float(row.get("duration", 15))
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
        pool = discover_strict_candidates(vod, profile, sig, blocked_ids)
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
    min_clip = float(os.environ.get("SHOOTER_VOD_MIN_CLIP_SCORE", "0.03"))
    owner_exemplars = os.environ.get("SHOOTER_VOD_OWNER_EXEMPLARS", "1") == "1"

    while peak_tries < max_tries:
        rows: list[dict] = []
        for clip in pool[:probe_limit]:
            peak = float(clip.get("start", 0))
            if any(abs(peak - s) <= 4.0 for s in skip_peaks):
                continue
            if _peak_too_close(peak, used_peaks, seg_gap):
                continue
            hm = clip.get("highlight_metrics") or {}
            clip_score = float(hm.get("clip_score") or clip.get("score") or 0.0)
            if owner_exemplars and clip_score < min_clip:
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
                    "score": float(clip.get("score", 0)),
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
    if entry is None:
        entry = {
            "id": vid,
            "path": str(vod),
            "title": title,
            "exhausted": False,
        }
        state.setdefault("vods", []).append(entry)
    else:
        entry.setdefault("path", str(vod))
        entry.setdefault("id", vid)
        if title and not entry.get("title"):
            entry["title"] = title

    metro_trust_segments = False
    if game == "pubg":
        ok_metro, metro_reason = _pubg_metro_vod_ok(vod, title=title, streak=streak_in)
        if not ok_metro:
            log.warning("metro reject scan vod=%s reason=%s", vod.name, metro_reason)
            entry = _vod_registry_entry(state, vod)
            if entry:
                entry["reject_reason"] = metro_reason
                if _pubg_metro_should_exhaust(title, streak_in, metro_reason):
                    entry["exhausted"] = True
                    _cleanup_exhausted_entry(game, entry, state)
            _save_state(game, state)
            return 0
        # Soften override must not disable per-segment metro checks.
        metro_trust_segments = not str(metro_reason).startswith("metro_soften")

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
            _cleanup_exhausted_entry(game, entry, state)
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
            _cleanup_exhausted_entry(game, entry, state)
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
            _cleanup_exhausted_entry(game, entry, state)
            _save_state(game, state)
            return 0
        apply_wot_seeds(seed_peaks)

    try:
        ctx = gate.adaptive_env(game, streak_in) if game in EXTENDED_GAMES else gate.adaptive_env(streak_in)
        with ctx as level:
            active_level = level
            if game == "pubg" and metro_trust_segments:
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
    # Ensure registry row exists and reflects scan fields (empty pool must persist).
    entry = _vod_registry_entry(state, vod) or entry
    if entry is None:
        entry = {
            "id": vid,
            "path": str(vod),
            "title": title,
            "exhausted": False,
        }
        state.setdefault("vods", []).append(entry)
    if sent == 0:
        record_zero_send_streak(entry, sent=0)
        force = should_force_exhaust_after_retries(entry)
        if should_mark_vod_exhausted(entry) or force:
            if force:
                entry["last_scan_blocked"] = True
                entry.setdefault("reject_reason", "presend_retry_exhausted")
            elif entry.get("last_scan_blocked") and entry.get("last_pool_peaks"):
                entry["reject_reason"] = "all_peaks_blocked"
            elif not entry.get("last_pool_peaks"):
                entry.setdefault("reject_reason", "no_combat_peaks")
            elif entry.get("last_scan_blocked"):
                entry["reject_reason"] = "all_peaks_blocked"
            entry["exhausted"] = True
            _cleanup_exhausted_entry(game, entry, state)
            log.info("exhausted vod=%s reason=%s", vod.name, entry.get("reject_reason"))
    else:
        record_zero_send_streak(entry, sent=sent)
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
    try:
        from vod_search_pool import used_ids_for_game

        used = used_ids_for_game(game)
    except Exception:
        used = set(state.get("used_youtube_ids", []))
    # Keep state list in sync for permanent ids only.
    state["used_youtube_ids"] = sorted(used)
    inbox = _paths(game)["inbox"]
    inbox.mkdir(parents=True, exist_ok=True)

    for mp4 in sorted(inbox.glob("yt_*.mp4"), key=lambda p: _inbox_order_key(p, registry)):
        vid = vod_youtube_id(mp4)
        # Sibling id-only row may already be spent while a live path duplicate remains.
        if _youtube_id_permanently_spent(state, vid):
            entry = _vod_registry_entry(state, mp4)
            if entry is None:
                entry = {
                    "id": vid,
                    "path": str(mp4),
                    "title": "",
                    "exhausted": True,
                    "reject_reason": "all_peaks_blocked",
                    "last_scan_blocked": True,
                }
                registry.append(entry)
            else:
                entry["path"] = str(mp4)
                entry["exhausted"] = True
                if not entry.get("reject_reason") or entry.get("reject_reason") == "no_combat_peaks":
                    if entry.get("last_pool_peaks") or entry.get("last_scan_blocked"):
                        entry["reject_reason"] = "all_peaks_blocked"
                entry["last_scan_blocked"] = True
            _cleanup_exhausted_entry(game, entry, state)
            _save_state(game, state)
            log.info("skip permanently spent inbox vod=%s id=%s", mp4.name, vid)
            continue
        entry = next((r for r in registry if r.get("path") == str(mp4)), None)
        if entry and entry.get("exhausted"):
            _cleanup_exhausted_entry(game, entry, state)
            _save_state(game, state)
            continue
        if should_skip_vod_rescan(entry, game=game):
            log.info("skip scan cooldown vod=%s", mp4.name)
            continue
        dur = _ffprobe_duration(mp4)
        min_sec = _shooter_vod_min_sec() if game in ("pubg", "standoff") else _vod_min_sec()
        if dur < min_sec:
            if entry is None:
                entry = {
                    "id": vid,
                    "path": str(mp4),
                    "title": "",
                    "exhausted": True,
                    "reject_reason": f"vod_length={dur:.0f}s",
                }
                registry.append(entry)
            else:
                entry["exhausted"] = True
                entry.setdefault("reject_reason", f"vod_length={dur:.0f}s")
            _cleanup_exhausted_entry(game, entry, state)
            _save_state(game, state)
            log.info("exhaust short inbox vod=%s dur=%.0fs", mp4.name, dur)
            continue
        if entry is None:
            entry = {
                "id": vid,
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
                if _pubg_metro_should_exhaust(title, streak_in, metro_reason):
                    entry["exhausted"] = True
                    _cleanup_exhausted_entry(game, entry, state)
                _save_state(game, state)
                continue
        n = _scan_vod_with_adaptive(game, token, chat_id, mp4, env, state)
        state["vods"] = registry
        if n == 0:
            entry = _vod_registry_entry(state, mp4) or entry
            if entry is not None:
                record_zero_send_streak(entry, sent=0)
            force = should_force_exhaust_after_retries(entry)
            if entry and not entry.get("exhausted") and (should_mark_vod_exhausted(entry) or force):
                if force:
                    entry.setdefault("reject_reason", "presend_retry_exhausted")
                    entry["last_scan_blocked"] = True
                elif entry.get("last_scan_blocked") and entry.get("last_pool_peaks"):
                    entry["reject_reason"] = "all_peaks_blocked"
                elif not entry.get("last_pool_peaks"):
                    entry.setdefault("reject_reason", "no_combat_peaks")
                else:
                    entry.setdefault("reject_reason", "all_peaks_blocked")
                entry["exhausted"] = True
                _cleanup_exhausted_entry(game, entry, state)
                log.info(
                    "exhausted vod=%s reason=%s",
                    mp4.name,
                    entry.get("reject_reason"),
                )
        elif entry is not None:
            record_zero_send_streak(entry, sent=n)
        _save_state(game, state)
        print(f"pipeline done sent={n} vods=1 game={game}")
        return 0

    if os.environ.get("SHOOTER_VOD_SKIP_DISCOVERY", "0") == "1":
        log.info("skip discovery — inbox exhausted game=%s", game)
        print(f"pipeline done sent=0 vods=0 game={game} skip_discovery=1")
        return 0

    pick = None
    pool_on = os.environ.get("VOD_SEARCH_POOL_ENABLED", "1") == "1"
    if pool_on:
        from vod_search_pool import pop_candidate, pool_needs_refresh, refresh_game_pool

        if pool_needs_refresh(game, used):
            refresh_game_pool(game, env, used=used, force=True)
        pick = pop_candidate(game, used)
        if pick and game in ("pubg", "standoff"):
            from youtube_shooter_vod_prefs import pick_discovery_candidate

            ranked = pick_discovery_candidate(game, [pick])
            if ranked is not None:
                pick = ranked
    else:
        candidates = _discover_candidates(game, env, used)
        if candidates:
            if game in ("pubg", "standoff"):
                from youtube_shooter_vod_prefs import pick_discovery_candidate

                pick = pick_discovery_candidate(game, candidates)
            if pick is None:
                pick = candidates[0]
    if pick is None:
        _notify_discovery_miss(game, token, chat_id, state)
        print(f"pipeline done sent=0 vods=0 game={game}")
        return 0
    ok_dl, reject_pre = _preflight_vod_pick(game, pick, env)
    if not ok_dl:
        used.add(str(pick.get("id") or ""))
        state["used_youtube_ids"] = sorted(x for x in used if x)
        registry.append(
            {
                "id": pick.get("id"),
                "path": "",
                "title": pick.get("title", ""),
                "exhausted": True,
                "reject_reason": reject_pre,
            }
        )
        state["vods"] = registry
        _save_state(game, state)
        log.warning(
            "skip download id=%s reason=%s title=%s",
            pick.get("id"),
            reject_pre,
            (pick.get("title") or "")[:80],
        )
        print(f"pipeline done sent=0 vods=0 game={game} reject={reject_pre}")
        return 0

    send_message(token, chat_id, f"📥 Качаю {game.upper()} VOD с YouTube…")
    vod = _download_vod(game, pick, env)
    if not vod:
        # Avoid retrying the same bad pool/discovery hit forever.
        used.add(str(pick.get("id") or ""))
        state["used_youtube_ids"] = sorted(x for x in used if x)
        reject = str(pick.get("reject_reason") or "download_failed")
        registry.append(
            {
                "id": pick.get("id"),
                "path": "",
                "title": pick.get("title", ""),
                "exhausted": True,
                "reject_reason": reject,
            }
        )
        state["vods"] = registry
        _save_state(game, state)
        log.info("marked undownloadable id=%s reason=%s", pick.get("id"), reject)
        print(f"pipeline done sent=0 vods=0 game={game} reject={reject}")
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
            exhausted = _pubg_metro_should_exhaust(
                str(pick.get("title") or ""),
                streak_dl,
                metro_reason,
            )
            entry = {
                "id": pick["id"],
                "path": str(vod),
                "title": pick.get("title", ""),
                "exhausted": exhausted,
                "reject_reason": metro_reason,
            }
            registry.append(entry)
            if exhausted:
                _cleanup_exhausted_entry(game, entry, state)
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
    os.environ.setdefault("HIGHLIGHT_HEATMAP", "0")
    os.environ.setdefault("SHOOTER_VOD_FEED", "1")
    os.environ.setdefault("SHOOTER_VOD_FAST_PROBE", "1")
    os.environ.setdefault("SHOOTER_VOD_PREFER_RUSSIAN", "1")
    # Match MLBB discovery density: IntelliClip + denser long-VOD analysis.
    os.environ.setdefault("SHOOTER_VOD_SKIP_INTELLICLIP", "0")
    os.environ.setdefault("SHOOTER_VOD_MAX_PANN_PROBE", "32")
    os.environ.setdefault("HIGHLIGHT_MAX_STAGE1", "48")
    os.environ.setdefault("SHOOTER_VOD_ACTION_PEAK_LIMIT", "40")
    os.environ.setdefault("SMART_LONG_SAMPLE_FPS", "1.0")
    os.environ.setdefault("SMART_LONG_ANALYSIS_MAX_FPS", "1.0")
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
        "SHOOTER_VOD_ACTION_PEAK_LIMIT",
        "SMART_LONG_SAMPLE_FPS",
        "SMART_LONG_ANALYSIS_MAX_FPS",
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
