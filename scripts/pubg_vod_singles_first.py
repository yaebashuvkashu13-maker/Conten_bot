#!/usr/bin/env python3
"""PUBG singles-first VOD mode: send all fights one-by-one, assemble montage at end."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("pubg_vod_singles_first")


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


def singles_keyboard(
    game: str,
    segment_id: str,
    vod_id: str,
    *,
    show_assemble: bool,
) -> dict:
    from shooter_vod_segment_store import inline_keyboard_markup

    markup = inline_keyboard_markup(game, segment_id)
    if show_assemble:
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
    """After 👍/👎 on the last single — keep assemble row."""
    from shooter_vod_segment_store import labeled_keyboard_markup

    base = labeled_keyboard_markup(
        game,
        label,
        reason=reason,
        segment_id=segment_id if label == "good" else "",
    )
    prefix = _callback_prefix(game)
    base["inline_keyboard"].append(
        [
            {"text": "🔧 Собрать склейку", "callback_data": f"{prefix}_assemble:{vod_id}"},
            {"text": "⏭ Пропустить", "callback_data": f"{prefix}_assemble_skip:{vod_id}"},
        ]
    )
    return base


def assemble_skip_keyboard(game: str) -> dict:
    return {"inline_keyboard": [[{"text": "—", "callback_data": "mlbb_noop"}]]}


def resolve_vod_path(vod_id: str) -> Path | None:
    vid = vod_id.strip()
    if not vid:
        return None
    name = vid if vid.endswith(".mp4") else f"yt_{vid}.mp4"
    for base in (
        Path(os.environ.get("PUBG_VOD_INBOX", "/root/data/pubg/youtube_nightly/inbox")),
        Path("/root/data/pubg/youtube_nightly/inbox"),
        Path("/root/data/mlbb/youtube_nightly/inbox"),
    ):
        candidate = base / name
        if candidate.is_file():
            return candidate
    return None


def good_rows_for_vod(game: str, vod_id: str) -> list[dict]:
    from shooter_vod_segment_store import find_segment, load_labels

    vid = vod_id.strip()
    out: list[dict] = []
    seen: set[str] = set()
    for entry in load_labels(game).get("good", []):
        sid = str(entry.get("segment_id") or "").strip()
        if not sid or not sid.startswith(f"{vid}_"):
            continue
        if sid in seen:
            continue
        seen.add(sid)
        row = find_segment(game, sid) or dict(entry)
        row["segment_id"] = sid
        out.append(row)
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


def run_assemble_montage(
    game: str,
    vod_id: str,
    token: str,
    chat_id: str,
) -> tuple[bool, str]:
    """Build montage from owner 👍 singles for this VOD (re-trim each part)."""
    from shooter_vod_segment_feed import (
        _prepare_pubg_row_for_send,
        _send_montage,
        file_sha256,
    )

    vod = resolve_vod_path(vod_id)
    if vod is None:
        return False, "vod_not_found"

    good_rows = good_rows_for_vod(game, vod_id)
    if len(good_rows) < 2:
        return False, f"need_2_good_have_{len(good_rows)}"

    prepared: list[dict] = []
    for row in good_rows:
        clip = row.get("clip") if isinstance(row.get("clip"), dict) else {}
        work = {
            **row,
            "clip": dict(clip),
            "peak_start": float(row.get("peak_start", row.get("start", 0)) or 0),
            "start": float(row.get("start", clip.get("start", 0)) or 0),
        }
        ready = _prepare_pubg_row_for_send(work, vod, single=False)
        if ready is not None:
            prepared.append(ready)

    if len(prepared) < 2:
        return False, f"after_retrim_need_2_have_{len(prepared)}"

    n = _send_montage(game, token, chat_id, vod, prepared, file_sha256(vod))
    if n > 0:
        return True, f"montage_x{len(prepared)}"
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
    """Send one single from pre-built rows; exhaust VOD on last clip."""
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

    max_tries = max(1, int(os.environ.get("PUBG_SINGLES_PEAK_TRIES_PER_RUN", "4")))
    exhaust_streak = int(os.environ.get("PUBG_SINGLES_ZERO_SEND_EXHAUST", "6"))

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
            clear_active_vod(state, reason="pubg_singles_exhausted")
            mark_exhausted_fn(state, vod, reason="pubg_singles_exhausted", delete_file=False)
            save_state_fn(game, state)
            log.info("pubg singles-first exhaust vod=%s — no sendable peaks", vod.name)
            return 0

        set_active_vod(state, vid)

        markup = singles_keyboard(game, str(row["segment_id"]), vid, show_assemble=is_final)
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
            if entry is not None:
                entry["singles_zero_send_streak"] = 0
                if scan_funnel is not None:
                    scan_funnel.sent = n
                    scan_funnel.presend_pass = n
                    scan_funnel.mark("sent")
                record_scan_fn(
                    entry,
                    sent=n,
                    pool_peaks=[float(r.get("peak_start", 0)) for r in rows],
                    blocked=False,
                    funnel=scan_funnel.to_dict() if scan_funnel else None,
                )
                entry.pop("reject_reason", None)

            if is_final:
                clear_active_vod(state, reason="pubg_singles_complete")
                mark_exhausted_fn(state, vod, reason="pubg_singles_complete", delete_file=False)
                log.info(
                    "pubg singles-first FINAL vod=%s peak=%.1f sent=%s — assemble button",
                    vod.name,
                    peak,
                    n,
                )
            else:
                log.info(
                    "pubg singles-first vod=%s peak=%.1f sent=%s — more peaks remain",
                    vod.name,
                    peak,
                    n,
                )

            save_state_fn(game, state)
            return n

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
                sent=0,
                pool_peaks=[float(r.get("peak_start", 0)) for r in rows],
                blocked=False,
            )
            if row_next is None or streak >= exhaust_streak:
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
            "pubg singles retry peak vod=%s rejected=%.1f attempt=%s/%s next=%s",
            vod.name,
            peak,
            attempt + 1,
            max_tries,
            "yes" if row_next else "no",
        )

    save_state_fn(game, state)
    return 0
