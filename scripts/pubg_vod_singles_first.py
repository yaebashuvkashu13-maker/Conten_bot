#!/usr/bin/env python3
"""PUBG singles-first VOD mode: send all fights one-by-one, assemble montage at end.

Inspect-all policy (PUBG_FULL_PEAK_SCAN=1, default):
  Walk every ranked combat peak in the VOD through presend gates.
  Do NOT stop after a silent top-4/8 try budget.

Send policy (PUBG_FULL_PEAK_SCAN=1): ship every peak that passes quality gates
in the same cycle (flood OK). Junk still dies on menu/loot/gun/hook gates.
Cap with PUBG_SINGLES_MAX_SENDS_PER_CYCLE (0 = unlimited). Legacy FULL=0
keeps one good send per cycle.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("pubg_vod_singles_first")


def full_peak_scan_enabled() -> bool:
    return os.environ.get("PUBG_FULL_PEAK_SCAN", "1") == "1"


def singles_peak_try_budget(n_rows: int) -> int:
    """How many peaks to inspect this run.

    Under full peak scan: 0 / unset → every row in the pool.
    Explicit positive PUBG_SINGLES_PEAK_TRIES_PER_RUN still caps if set.
    Legacy (FULL_PEAK_SCAN=0): default 4.
    """
    n = max(0, int(n_rows))
    raw = os.environ.get("PUBG_SINGLES_PEAK_TRIES_PER_RUN")
    if full_peak_scan_enabled():
        if raw is None or str(raw).strip() == "":
            return max(1, n) if n else 1
        try:
            val = int(raw)
        except (TypeError, ValueError):
            return max(1, n) if n else 1
        if val <= 0:
            return max(1, n) if n else 1
        return max(1, min(val, n)) if n else max(1, val)
    try:
        val = int(raw if raw is not None else "4")
    except (TypeError, ValueError):
        val = 4
    if val <= 0:
        return max(1, n) if n else 1
    return max(1, val)


def singles_zero_send_exhaust_limit() -> int:
    """Consecutive presend rejects before giving up on a VOD.

    Under full peak scan default 20 = skip dead VODs so fresh inbox can ship.
    Set PUBG_SINGLES_ZERO_SEND_EXHAUST=0 to never abandon on streak alone.
    Legacy (full scan off) default 6.
    """
    if full_peak_scan_enabled():
        try:
            return max(0, int(os.environ.get("PUBG_SINGLES_ZERO_SEND_EXHAUST", "20")))
        except (TypeError, ValueError):
            return 20
    try:
        return max(1, int(os.environ.get("PUBG_SINGLES_ZERO_SEND_EXHAUST", "6")))
    except (TypeError, ValueError):
        return 6


def singles_max_sends_per_cycle() -> int:
    """How many quality singles to ship in one feed cycle.

    Full peak scan default 0 = unlimited (keep sending while gates pass).
    Legacy FULL=0 default 1 (stop after first good send).
    Explicit positive PUBG_SINGLES_MAX_SENDS_PER_CYCLE always caps.
    """
    raw = os.environ.get("PUBG_SINGLES_MAX_SENDS_PER_CYCLE")
    if full_peak_scan_enabled():
        if raw is None or str(raw).strip() == "":
            return 0
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 0
    if raw is None or str(raw).strip() == "":
        return 1
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1

ASSEMBLE_LOG = Path(os.environ.get("PUBG_ASSEMBLE_LOG", "/root/data/mlbb/assemble_montage.log"))
PENDING_ASSEMBLE_PATH = Path(
    os.environ.get("PUBG_PENDING_ASSEMBLE", "/root/data/pubg/pending_assemble.json")
)


def pubg_singles_first_enabled() -> bool:
    return os.environ.get("PUBG_VOD_SINGLES_FIRST", "1") == "1"


ACTIVE_VOD_KEY = "pubg_singles_active_vod"


def get_active_vod_id(state: dict) -> str:
    return str(state.get(ACTIVE_VOD_KEY) or "").strip()


def set_active_vod(state: dict, vod_id: str) -> None:
    vid = vod_id.strip()
    if not vid:
        return
    prev = get_active_vod_id(state)
    if prev and prev != vid:
        log.warning("pubg singles refuse pin steal vod=%s while active=%s", vid, prev)
        return
    state[ACTIVE_VOD_KEY] = vid
    if prev != vid:
        log.info("pubg singles pin vod=%s (was %s)", vid, prev or "none")


def clear_active_vod(state: dict, *, reason: str = "") -> None:
    prev = get_active_vod_id(state)
    if prev:
        state.pop(ACTIVE_VOD_KEY, None)
        log.info("pubg singles unpin vod=%s reason=%s", prev, reason or "done")


def _vod_registry_entries_for_id(registry: list[dict], vod_id: str) -> list[dict]:
    out: list[dict] = []
    for row in registry:
        rid = str(row.get("id") or "").strip()
        path = str(row.get("path") or "")
        stem = _vod_id_from_path(Path(path)) if path else ""
        if rid == vod_id or stem == vod_id:
            out.append(row)
    return out


def pin_inbox_to_active_vod(
    state: dict,
    inbox_files: list[Path],
    registry: list[dict],
) -> list[Path]:
    """While a VOD is in progress, scan/send only that file — no inbox interleaving."""
    if not pubg_singles_first_enabled():
        return inbox_files
    active = get_active_vod_id(state)
    if not active:
        return inbox_files
    matching = [p for p in inbox_files if _vod_id_from_path(p) == active]
    if not matching:
        clear_active_vod(state, reason="file_missing")
        return inbox_files
    entries = _vod_registry_entries_for_id(registry, active)
    if entries and any(r.get("exhausted") for r in entries):
        clear_active_vod(state, reason="exhausted")
        return inbox_files
    log.debug("pubg singles inbox pinned to %s (skip %s others)", active, len(inbox_files) - 1)
    return matching


def _vod_id_from_path(mp4: Path) -> str:
    from shooter_vod_segment_store import vod_youtube_id

    return vod_youtube_id(mp4)


def inbox_active_vod_priority(state: dict, mp4: Path) -> int:
    """Sort key fragment: active VOD always first when pinning not yet set."""
    if not pubg_singles_first_enabled():
        return 1
    active = get_active_vod_id(state)
    if not active:
        return 1
    return 0 if _vod_id_from_path(mp4) == active else 2


def _callback_prefix(game: str) -> str:
    return f"{game.strip().lower()}_vseg"


def vod_id_from_segment_id(segment_id: str) -> str:
    sid = segment_id.strip()
    if "_" in sid:
        return sid.rsplit("_", 1)[0]
    return sid


def good_count_for_vod(game: str, vod_id: str) -> int:
    return len(good_rows_for_vod(game, vod_id))


def assemble_eligible(game: str, vod_id: str) -> bool:
    """Montage only when owner marked ≥2 singles 👍 from the same VOD."""
    return good_count_for_vod(game, vod_id) >= 2


def should_show_assemble_button(game: str, vod_id: str, *, singles_final: bool) -> bool:
    if game.strip().lower() != "pubg" or not pubg_singles_first_enabled():
        return False
    if not singles_final:
        return False
    return assemble_eligible(game, vod_id)


def singles_keyboard(
    game: str,
    segment_id: str,
    vod_id: str,
    *,
    show_assemble: bool,
) -> dict:
    from shooter_vod_segment_store import inline_keyboard_markup

    markup = inline_keyboard_markup(game, segment_id)
    if show_assemble and assemble_eligible(game, vod_id):
        prefix = _callback_prefix(game)
        markup["inline_keyboard"].append(
            [
                {"text": "🔧 Собрать склейку", "callback_data": f"{prefix}_assemble:{vod_id}"},
                {"text": "⏭ Пропустить", "callback_data": f"{prefix}_assemble_skip:{vod_id}"},
            ]
        )
    return markup


def singles_final_labeled_keyboard(
    game: str,
    segment_id: str,
    vod_id: str,
    label: str,
    *,
    reason: str = "",
) -> dict:
    """After 👍/👎 on the last single — keep assemble row when ≥2 👍 on this VOD."""
    from shooter_vod_segment_store import labeled_keyboard_markup

    base = labeled_keyboard_markup(
        game,
        label,
        reason=reason,
        segment_id=segment_id if label == "good" else "",
    )
    if not assemble_eligible(game, vod_id):
        return base
    prefix = _callback_prefix(game)
    base["inline_keyboard"].append(
        [
            {"text": "🔧 Собрать склейку", "callback_data": f"{prefix}_assemble:{vod_id}"},
            {"text": "⏭ Пропустить", "callback_data": f"{prefix}_assemble_skip:{vod_id}"},
        ]
    )
    return base


def after_owner_label_keyboard(
    game: str,
    segment_id: str,
    label: str,
    *,
    reason: str = "",
) -> dict:
    """Keyboard after 👍/👎 — assemble when ≥2 👍 and this was the last single."""
    from shooter_vod_segment_store import (
        find_segment,
        labeled_keyboard_markup,
        montage_labeled_keyboard_markup,
        montage_parts_from_segment,
    )

    parts = montage_parts_from_segment(game, segment_id)
    if parts:
        return montage_labeled_keyboard_markup(game, parts, reason=reason)

    seg_row = find_segment(game, segment_id) or {}
    vod_id = str(seg_row.get("vod_id") or vod_id_from_segment_id(segment_id))
    if should_show_assemble_button(
        game,
        vod_id,
        singles_final=bool(seg_row.get("singles_final")),
    ):
        return singles_final_labeled_keyboard(
            game,
            segment_id,
            vod_id,
            label,
            reason=reason,
        )
    return labeled_keyboard_markup(
        game,
        label,
        reason=reason,
        segment_id=segment_id if label == "good" else "",
    )


def assemble_skip_keyboard(game: str) -> dict:
    return {"inline_keyboard": [[{"text": "—", "callback_data": "mlbb_noop"}]]}


def _assemble_log(msg: str) -> None:
    log.info("%s", msg)
    try:
        ASSEMBLE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ASSEMBLE_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def segment_belongs_to_vod(segment_id: str, vod_id: str) -> bool:
    sid = segment_id.strip()
    vid = vod_id.strip()
    if not sid or not vid:
        return False
    return sid.startswith((f"{vid}_", f"yt_{vid}_", f"owner_yt_{vid}_"))


def resolve_vod_path(vod_id: str) -> Path | None:
    vid = vod_id.strip()
    if not vid:
        return None
    name = vid if vid.endswith(".mp4") else f"yt_{vid}.mp4"
    inbox = Path(os.environ.get("PUBG_VOD_INBOX", "/root/data/pubg/youtube_nightly/inbox"))
    bases = [
        inbox,
        inbox.parent / "parked",
        Path("/root/data/pubg/youtube_nightly/inbox"),
        Path("/root/data/pubg/youtube_nightly/parked"),
        Path("/root/data/mlbb/youtube_nightly/inbox"),
        Path("/root/data/mlbb/youtube_nightly/parked"),
    ]
    seen: set[Path] = set()
    for base in bases:
        if base in seen:
            continue
        seen.add(base)
        candidate = base / name
        if candidate.is_file():
            return candidate
    return None


def load_pending_assemble() -> list[dict]:
    if not PENDING_ASSEMBLE_PATH.is_file():
        return []
    try:
        data = json.loads(PENDING_ASSEMBLE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def save_pending_assemble(jobs: list[dict]) -> None:
    PENDING_ASSEMBLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PENDING_ASSEMBLE_PATH.write_text(
        json.dumps(jobs, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def enqueue_assemble_job(game: str, vod_id: str, chat_id: str) -> dict:
    jobs = load_pending_assemble()
    for job in jobs:
        if (
            str(job.get("game") or "") == game
            and str(job.get("vod_id") or "") == vod_id
            and str(job.get("status") or "") in {"pending", "running"}
        ):
            return job
    job = {
        "id": f"{game}:{vod_id}:{int(time.time())}",
        "game": game,
        "vod_id": vod_id,
        "chat_id": str(chat_id),
        "status": "pending",
        "ts": time.time(),
    }
    jobs.append(job)
    save_pending_assemble(jobs[-20:])
    _assemble_log(f"enqueue assemble game={game} vod={vod_id} chat={chat_id}")
    return job


def update_assemble_job(job_id: str, **fields: object) -> None:
    jobs = load_pending_assemble()
    for job in jobs:
        if str(job.get("id") or "") == job_id:
            job.update(fields)
            job["updated_at"] = time.time()
            break
    save_pending_assemble(jobs)


def bot_token_from_env() -> str:
    token = (
        os.environ.get("TG_BOT_TOKEN", "").strip()
        or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    )
    if token:
        return token
    env_file = Path(os.environ.get("VIDEO_BOT_ENV", "/root/.video_bot.env"))
    if not env_file.is_file():
        return ""
    try:
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("TG_BOT_TOKEN="):
                return line.split("=", 1)[1].strip()
    except OSError:
        return ""
    return ""


def good_rows_for_vod(game: str, vod_id: str) -> list[dict]:
    from shooter_vod_segment_store import find_segment, load_labels

    vid = vod_id.strip()
    out: list[dict] = []
    seen: set[str] = set()
    for entry in load_labels(game).get("good", []):
        sid = str(entry.get("segment_id") or "").strip()
        if not sid or not segment_belongs_to_vod(sid, vid):
            continue
        if sid in seen:
            continue
        seen.add(sid)
        row = find_segment(game, sid) or dict(entry)
        row["segment_id"] = sid
        out.append(row)
    max_parts = max(2, int(os.environ.get("PUBG_ASSEMBLE_MAX_PARTS", "6")))
    out.sort(key=lambda r: str(r.get("at") or ""), reverse=True)
    out = out[:max_parts]
    out.sort(key=lambda r: float(r.get("peak_start", r.get("start", 0)) or 0))
    return out


def pick_next_single_row(
    rows: list[dict],
    *,
    blocked_ids: set[str],
    rejected_peaks: list[float],
    gap_sec: float,
    used_peaks: list[float],
    peak_too_close,
) -> tuple[dict | None, bool]:
    """Return (row, is_vod_final). is_vod_final=True when no more sendable peaks after this one."""
    candidates: list[dict] = []
    for row in rows:
        sid = str(row.get("segment_id") or "")
        if sid and sid in blocked_ids:
            continue
        peak = float(row.get("peak_start", row.get("start", 0)) or 0)
        if any(abs(peak - float(bad)) <= 4.0 for bad in rejected_peaks):
            continue
        if peak_too_close(peak, used_peaks, gap_sec):
            continue
        candidates.append(row)
    if not candidates:
        return None, True
    candidates.sort(key=lambda r: float(r.get("score", 0)), reverse=True)
    chosen = candidates[0]
    peak = float(chosen.get("peak_start", chosen.get("start", 0)) or 0)
    rest = [
        r
        for r in candidates[1:]
        if not peak_too_close(
            float(r.get("peak_start", r.get("start", 0)) or 0),
            [peak],
            gap_sec,
        )
    ]
    return chosen, len(rest) == 0


def prepare_pubg_assemble_row(
    vod: Path,
    peak: float,
    *,
    owner_row: dict | None = None,
) -> dict | None:
    """Re-trim owner 👍 peak on full VOD; re-gates only trim bounds, not owner approval."""
    from pubg_clip_shape_gate import validate_clip_fight_shape
    from pubg_fight_segment import resolve_pubg_fight_bounds
    from pubg_montage_bounds import pubg_clip_has_gunfire, tighten_pubg_assemble_bounds
    from pubg_quality_score import score_pubg_window
    from shooter_vod_segment_feed import _ffprobe_duration
    from shooter_vod_segment_store import segment_id, vod_youtube_id

    peak_val = float(peak)
    owner_approved = owner_row is not None
    file_dur = _ffprobe_duration(vod)
    owner_start = None
    owner_dur = None
    if owner_row is not None:
        try:
            if owner_row.get("start") is not None:
                owner_start = float(owner_row.get("start"))
            qm = owner_row.get("quality_metrics") or {}
            if owner_row.get("duration") is not None:
                owner_dur = float(owner_row.get("duration"))
            elif qm.get("duration") is not None:
                owner_dur = float(qm.get("duration"))
        except (TypeError, ValueError):
            owner_start = owner_dur = None
    start, dur, report = resolve_pubg_fight_bounds(vod, peak_val, file_duration=file_dur)
    start, dur = tighten_pubg_assemble_bounds(
        start,
        dur,
        report,
        peak=peak_val,
        file_dur=file_dur,
        owner_start=owner_start,
        owner_dur=owner_dur,
    )
    ok_shape, shape_reason = validate_clip_fight_shape(start, dur, peak_val, report)
    if not ok_shape and not owner_approved:
        log.warning("assemble shape reject peak=%.1f: %s", peak_val, shape_reason)
        return None
    if not ok_shape and owner_approved:
        log.info(
            "assemble owner-trust shape peak=%.1f: %s — keep re-trim",
            peak_val,
            shape_reason,
        )

    gun_ok, gun_reason = pubg_clip_has_gunfire(vod, start, dur, peak_val, single=True)
    if not gun_ok and not owner_approved:
        log.warning("assemble gun reject peak=%.1f: %s", peak_val, gun_reason)
        return None
    if not gun_ok and owner_approved:
        log.info(
            "assemble owner-trust gun peak=%.1f: %s — keep re-trim",
            peak_val,
            gun_reason,
        )

    presend_env = {}
    if owner_approved:
        presend_env["PUBG_OWNER_REDO"] = "1"
        presend_env["PUBG_EARLY_PAYOFF_REJECT_SINGLES"] = "0"
    if owner_approved:
        # Owner already 👍'd this fight. Do not re-run CLIP/PANNs (10+ min per
        # peak on CPU) — re-trim bounds + gunfire only, then ship.
        quality_report = dict((owner_row or {}).get("quality_metrics") or {})
        quality_report["owner_assemble_trusted"] = True
        quality_report.setdefault(
            "quality_score",
            float((owner_row or {}).get("score") or 0.5),
        )
        ok, presend_reason = True, "owner_assemble_skip_presend"
        log.info("assemble skip CLIP presend peak=%.1f — owner 👍", peak_val)
    else:
        prev: dict[str, str | None] = {k: os.environ.get(k) for k in presend_env}
        try:
            for key, value in presend_env.items():
                os.environ[key] = value
            ok, presend_reason, quality_report = score_pubg_window(
                vod,
                start,
                dur,
                use_cache=False,
                single=True,
            )
        finally:
            for key, old in prev.items():
                if old is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old

    if not ok and not owner_approved:
        log.warning("assemble presend reject peak=%.1f: %s", peak_val, presend_reason)
        return None
    if not ok and owner_approved:
        log.info(
            "assemble owner-trust presend peak=%.1f: %s — ship re-trim",
            peak_val,
            presend_reason,
        )
        quality_report = dict(quality_report or {})
        quality_report.setdefault("owner_assemble_trusted", True)

    vid = vod_youtube_id(vod)
    clip = {
        "start": start,
        "peak_start": peak_val,
        "input_duration": dur,
        "bounds_locked": True,
        "segment_report": report,
    }
    sid = str((owner_row or {}).get("segment_id") or segment_id(vid, start))
    return {
        "segment_id": sid,
        "vod_id": vid,
        "peak_start": peak_val,
        "start": start,
        "score": float(quality_report.get("quality_score", 0.0) or 0.0),
        "clip": clip,
        "quality_report": quality_report,
    }


def run_assemble_montage(
    game: str,
    vod_id: str,
    token: str,
    chat_id: str,
) -> tuple[bool, str]:
    """Build montage from owner 👍 singles — re-probe VOD, re-trim gunfire ±4s, presend each part."""
    from shooter_vod_segment_feed import _send_montage, file_sha256

    token = (token or "").strip() or bot_token_from_env()
    if not token:
        _assemble_log(f"assemble fail vod={vod_id} reason=missing_token")
        return False, "missing_token"

    vod = resolve_vod_path(vod_id)
    if vod is None:
        _assemble_log(f"assemble fail vod={vod_id} reason=vod_not_found")
        return False, "vod_not_found"

    good_rows = good_rows_for_vod(game, vod_id)
    _assemble_log(f"assemble start vod={vod_id} path={vod} likes={len(good_rows)}")
    if len(good_rows) < 2:
        return False, f"need_2_ok_have_{len(good_rows)}"

    prepared: list[dict] = []
    failed_peaks: list[float] = []
    for row in good_rows:
        peak = float(row.get("peak_start", row.get("start", 0)) or 0)
        ready = prepare_pubg_assemble_row(vod, peak, owner_row=row)
        if ready is not None:
            prepared.append(ready)
        else:
            failed_peaks.append(peak)

    if len(prepared) < 2:
        if failed_peaks:
            peaks_s = ", ".join(f"{p:.0f}s" for p in failed_peaks[:4])
            reason = f"не удалось обрезать 👍 пики ({peaks_s}) — VOD недоступен?"
            _assemble_log(f"assemble fail vod={vod_id} reason={reason}")
            return False, reason
        return False, f"need_2_ok_have_{len(good_rows)}"

    n = _send_montage(
        game, token, chat_id, vod, prepared, file_sha256(vod), owner_assemble=True
    )
    if n > 0:
        reason = f"montage_x{len(prepared)}"
        _assemble_log(f"assemble ok vod={vod_id} {reason}")
        return True, reason
    _assemble_log(f"assemble fail vod={vod_id} reason=montage_send_failed")
    return False, "montage_send_failed"


def singles_first_send_cycle(
    *,
    game: str,
    token: str,
    chat_id: str,
    vod: Path,
    vid: str,
    state: dict,
    entry: dict | None,
    rows: list[dict],
    gap_sec: float,
    rejected_peaks: list[float],
    sig: str,
    mark_exhausted_fn: Callable[..., None],
    save_state_fn: Callable[[str, dict], None],
    record_scan_fn: Callable[..., None],
    scan_funnel: Any | None = None,
) -> int:
    """Send quality singles from pre-built rows; exhaust VOD when pool is done.

    Under full peak scan: keep shipping every gate-pass until the pool is empty
    (or PUBG_SINGLES_MAX_SENDS_PER_CYCLE). Legacy: stop after first good send.
    """
    from shooter_vod_segment_feed import (
        _peak_too_close,
        _remember_dense_rejections,
        _send_batch,
        _used_peak_times,
        labeled_ids,
        load_feed_sent,
    )

    sent_set = load_feed_sent(game)
    used_peaks = _used_peak_times(game, vid, sent_set)
    blocked_ids = labeled_ids(game) | sent_set
    merged_rejected = list(rejected_peaks)
    if entry is not None:
        for value in entry.get("dense_rejected_peaks") or []:
            try:
                merged_rejected.append(float(value))
            except (TypeError, ValueError):
                continue

    max_tries = singles_peak_try_budget(len(rows))
    exhaust_streak = singles_zero_send_exhaust_limit()
    max_sends = singles_max_sends_per_cycle()
    total_sent = 0
    log.info(
        "pubg singles inspect-all vod=%s rows=%s tries=%s exhaust_streak=%s "
        "max_sends=%s full_scan=%s",
        vod.name,
        len(rows),
        max_tries,
        exhaust_streak,
        max_sends if max_sends > 0 else "unlimited",
        int(full_peak_scan_enabled()),
    )

    for attempt in range(max_tries):
        row, is_final = pick_next_single_row(
            rows,
            blocked_ids=blocked_ids,
            rejected_peaks=merged_rejected,
            gap_sec=gap_sec,
            used_peaks=used_peaks,
            peak_too_close=_peak_too_close,
        )
        if row is None:
            if total_sent > 0:
                clear_active_vod(state, reason="pubg_singles_complete")
                mark_exhausted_fn(state, vod, reason="pubg_singles_complete", delete_file=False)
                log.info(
                    "pubg singles-first pool done vod=%s total_sent=%s",
                    vod.name,
                    total_sent,
                )
                save_state_fn(game, state)
                return total_sent
            clear_active_vod(state, reason="pubg_singles_exhausted")
            mark_exhausted_fn(state, vod, reason="pubg_singles_exhausted", delete_file=False)
            save_state_fn(game, state)
            log.info("pubg singles-first exhaust vod=%s — no sendable peaks", vod.name)
            return 0

        set_active_vod(state, vid)

        markup = singles_keyboard(
            game,
            str(row["segment_id"]),
            vid,
            show_assemble=should_show_assemble_button(game, vid, singles_final=is_final),
        )
        peak = float(row.get("peak_start", row.get("start", 0)) or 0)
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
            total_sent += n
            sid = str(row.get("segment_id") or "")
            if sid:
                blocked_ids.add(sid)
                sent_set.add(sid)
            used_peaks.append(peak)
            # Refresh from disk so later picks skip anything already shipped.
            sent_set = load_feed_sent(game)
            used_peaks = _used_peak_times(game, vid, sent_set)
            blocked_ids = labeled_ids(game) | sent_set

            if entry is not None:
                entry["singles_zero_send_streak"] = 0
                if scan_funnel is not None:
                    scan_funnel.sent = total_sent
                    scan_funnel.presend_pass = max(
                        int(getattr(scan_funnel, "presend_pass", 0) or 0), total_sent
                    )
                    scan_funnel.mark("sent")
                record_scan_fn(
                    entry,
                    sent=total_sent,
                    pool_peaks=[float(r.get("peak_start", 0)) for r in rows],
                    blocked=False,
                    funnel=scan_funnel.to_dict() if scan_funnel else None,
                )
                entry.pop("reject_reason", None)

            if is_final:
                clear_active_vod(state, reason="pubg_singles_complete")
                mark_exhausted_fn(state, vod, reason="pubg_singles_complete", delete_file=False)
                log.info(
                    "pubg singles-first FINAL vod=%s peak=%.1f sent=%s total=%s — assemble button",
                    vod.name,
                    peak,
                    n,
                    total_sent,
                )
                save_state_fn(game, state)
                return total_sent

            hit_cap = max_sends > 0 and total_sent >= max_sends
            log.info(
                "pubg singles-first vod=%s peak=%.1f sent=%s total=%s — %s",
                vod.name,
                peak,
                n,
                total_sent,
                "cycle cap" if hit_cap else "continue quality flood",
            )
            save_state_fn(game, state)
            if hit_cap:
                return total_sent
            continue

        _remember_dense_rejections(entry, [peak])
        merged_rejected.append(peak)
        row_next, _ = pick_next_single_row(
            rows,
            blocked_ids=blocked_ids,
            rejected_peaks=merged_rejected,
            gap_sec=gap_sec,
            used_peaks=used_peaks,
            peak_too_close=_peak_too_close,
        )
        if entry is not None:
            streak = int(entry.get("singles_zero_send_streak") or 0) + 1
            entry["singles_zero_send_streak"] = streak
            entry["reject_reason"] = "pubg_singles_presend_reject"
            record_scan_fn(
                entry,
                sent=total_sent,
                pool_peaks=[float(r.get("peak_start", 0)) for r in rows],
                blocked=False,
            )
            # Full scan: streak limit 0 means keep going until the pool is empty.
            streak_give_up = exhaust_streak > 0 and streak >= exhaust_streak
            if row_next is None or streak_give_up:
                if total_sent > 0:
                    clear_active_vod(state, reason="pubg_singles_complete")
                    mark_exhausted_fn(
                        state, vod, reason="pubg_singles_complete", delete_file=False
                    )
                    log.info(
                        "pubg singles stop after quality sends vod=%s total=%s "
                        "remaining_reject_or_empty=1",
                        vod.name,
                        total_sent,
                    )
                    save_state_fn(game, state)
                    return total_sent
                reason = (
                    "pubg_singles_presend_exhausted"
                    if row_next is None
                    else f"pubg_singles_zero_send_streak_{streak}"
                )
                clear_active_vod(state, reason=reason)
                mark_exhausted_fn(state, vod, reason=reason, delete_file=False)
                log.warning(
                    "pubg singles give up vod=%s reason=%s rejected=%s",
                    vod.name,
                    reason,
                    len(merged_rejected),
                )
                save_state_fn(game, state)
                return 0
            save_state_fn(game, state)
        log.info(
            "pubg singles retry peak vod=%s rejected=%.1f attempt=%s/%s next=%s total_sent=%s",
            vod.name,
            peak,
            attempt + 1,
            max_tries,
            "yes" if row_next else "no",
            total_sent,
        )

    save_state_fn(game, state)
    return total_sent


def _cli_send_text(token: str, chat_id: str, text: str) -> None:
    import urllib.parse
    import urllib.request

    if not token or not chat_id:
        return
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data,
            timeout=20,
        )
    except Exception as exc:
        _assemble_log(f"assemble notify failed: {exc}")


def assemble_lock_path(vod_id: str) -> Path:
    safe = "".join(ch if ch.isalnum() else "_" for ch in vod_id.strip())[:32]
    return Path(f"/tmp/pubg_assemble_{safe}.lock")


def assemble_already_running(vod_id: str) -> bool:
    path = assemble_lock_path(vod_id)
    if not path.is_file():
        return False
    try:
        pid = int(path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return False
    if pid <= 1:
        path.unlink(missing_ok=True)
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        path.unlink(missing_ok=True)
        return False


def _acquire_assemble_lock(vod_id: str) -> bool:
    if assemble_already_running(vod_id):
        return False
    assemble_lock_path(vod_id).write_text(str(os.getpid()), encoding="utf-8")
    return True


def _release_assemble_lock(vod_id: str) -> None:
    path = assemble_lock_path(vod_id)
    try:
        if path.is_file() and path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            path.unlink(missing_ok=True)
    except OSError:
        pass


def spawn_assemble_subprocess(game: str, vod_id: str, chat_id: str, token: str = "") -> None:
    """Detached assemble so a bot restart cannot kill the job."""
    import subprocess
    import sys

    token = (token or "").strip() or bot_token_from_env()
    if assemble_already_running(vod_id):
        _assemble_log(f"assemble already running vod={vod_id} — skip spawn")
        return
    script = Path("/usr/local/bin/pubg_vod_singles_first.py")
    if not script.is_file():
        script = Path(__file__).resolve()
    ASSEMBLE_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_fh = ASSEMBLE_LOG.open("a", encoding="utf-8")
    env = os.environ.copy()
    if token:
        env["TG_BOT_TOKEN"] = token
    env["TG_CHAT_ID"] = str(chat_id)
    subprocess.Popen(
        [
            sys.executable,
            "-u",
            str(script),
            "--assemble",
            "--game",
            game,
            "--vod",
            vod_id,
            "--chat-id",
            str(chat_id),
        ],
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=env,
        close_fds=True,
    )
    _assemble_log(f"spawn assemble subprocess vod={vod_id} script={script}")


def retry_pending_assemble_jobs(token: str = "") -> int:
    """Re-launch pending/stale assemble jobs after bot restart."""
    now = time.time()
    launched = 0
    for job in load_pending_assemble():
        status = str(job.get("status") or "")
        age = now - float(job.get("updated_at") or job.get("ts") or 0)
        if status == "ok":
            continue
        if status == "running" and age < 1800:
            continue
        if status not in {"pending", "running"}:
            continue
        vod_id = str(job.get("vod_id") or "").strip()
        chat_id = str(job.get("chat_id") or "").strip()
        game = str(job.get("game") or "pubg")
        if not vod_id or not chat_id:
            continue
        update_assemble_job(str(job.get("id") or ""), status="pending")
        spawn_assemble_subprocess(game, vod_id, chat_id, token=token)
        launched += 1
    return launched


def main_cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="PUBG singles-first helpers")
    parser.add_argument("--assemble", action="store_true")
    parser.add_argument("--game", default="pubg")
    parser.add_argument("--vod", default="")
    parser.add_argument("--chat-id", default="")
    parser.add_argument("--retry-pending", action="store_true")
    args = parser.parse_args()
    token = bot_token_from_env()
    if args.retry_pending:
        n = retry_pending_assemble_jobs(token=token)
        print(f"retry_pending={n}")
        return 0
    if not args.assemble:
        parser.error("pass --assemble or --retry-pending")
    vod_id = args.vod.strip()
    chat_id = str(args.chat_id).strip()
    if not vod_id or not chat_id:
        parser.error("--vod and --chat-id required")
    job = enqueue_assemble_job(args.game, vod_id, chat_id)
    if not _acquire_assemble_lock(vod_id):
        _assemble_log(f"assemble lock busy vod={vod_id}")
        print("busy")
        return 0
    try:
        update_assemble_job(str(job.get("id") or ""), status="running")
        _cli_send_text(
            token,
            chat_id,
            f"🔧 {args.game.upper()} {vod_id}: собираю склейку из 👍…",
        )
        ok, reason = run_assemble_montage(args.game, vod_id, token, chat_id)
        update_assemble_job(
            str(job.get("id") or ""),
            status="ok" if ok else "error",
            reason=reason,
        )
        if ok:
            _cli_send_text(token, chat_id, f"✅ {args.game.upper()} {vod_id}: склейка отправлена ({reason})")
            print(f"ok {reason}")
            return 0
        _cli_send_text(token, chat_id, f"⚠️ {args.game.upper()} {vod_id}: склейка не собрана — {reason}")
        print(f"fail {reason}")
        return 1
    finally:
        _release_assemble_lock(vod_id)


if __name__ == "__main__":
    raise SystemExit(main_cli())

