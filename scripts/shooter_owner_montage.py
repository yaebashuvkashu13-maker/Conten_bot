#!/usr/bin/env python3
"""Owner-good fight anchors as *hints* for shooter склейки — not the only source.

Keep owner 👍 / brawl times in mind: boost those peaks in the pool and soft-allow
noisy gates near them. Never replace normal rediscover / combat scan with
owner-only selection.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("shooter_owner_montage")

# Hardcoded "brawl" seeds — disabled. Owner rejected today's n97c/FpMs склейки
# (run/talk/loot). Hints come only from live calibration / 👍 feedback.
PUBG_BRAWL_ANCHORS_BY_VOD: dict[str, list[float]] = {}
# Peaks owner marked as trash (do not boost / soft-allow).
PUBG_OWNER_REJECTED_PEAKS: dict[str, list[float]] = {
    "n97cHIR9Qow": [1845.0, 2150.0, 2470.0, 2005.0],
    "FpMs48XOnq0": [230.0, 300.0, 475.0, 790.0, 930.0, 1070.0, 1175.0, 1420.0],
}
# Sniper / hold windows labeled good but unsuitable for combat склейка.
PUBG_SNIPER_SKIP: frozenset[float] = frozenset({2005.0})

# Reasons that must never be soft-forgiven (talk / loot / no gunfire).
NEVER_SOFT_ALLOW_REASONS: frozenset[str] = frozenset(
    {
        "streamer_talk",
        "talk_menu",
        "talk_low_gun",
        "loot_walk",
        "loot_rummage",
        "run_loot",
        "run_no_fight",
        "run_fake_gun",
        "no_shots",
        "silent_segment",
        "menu_ui",
        "music",
        "ambient",
        "quiet",
        "vehicle",
        "panns_speech_dominant",
        "panns_music_dominant",
        "panns_no_gunshot",
        "author_death",
        "author_death_ocr",
        "author_death_hud",
        "author_death_screen",
        "no_author_kill",
    }
)
# Borderline-only reasons that may soft-pass near owner-good IF gunfire evidence exists.
BORDERLINE_SOFT_REASONS: frozenset[str] = frozenset(
    {
        "weak_shots",
        "low_energy",
        "low_gunfire",
        "panns_uncertain",
        "sniper_hold_weak",
    }
)


def owner_anchor_montage_enabled() -> bool:
    return os.environ.get("SHOOTER_VOD_OWNER_ANCHOR_MONTAGE", "0") == "1"


def _video_id(vod: Path) -> str:
    stem = vod.stem
    if stem.startswith("yt_") and len(stem) > 3:
        return stem[3:]
    return stem


def _peaks_from_pubg_calibration(vod: Path) -> list[float]:
    try:
        from pubg_owner_calibration import labels_for_video
    except ImportError:
        return []
    out: list[float] = []
    for row in labels_for_video(vod):
        if str(row.get("label") or "") != "good":
            continue
        try:
            t = float(row["time_sec"])
        except (KeyError, TypeError, ValueError):
            continue
        if any(abs(t - s) <= 2.0 for s in PUBG_SNIPER_SKIP):
            continue
        out.append(t)
    return out


def _peaks_from_highlight_labels(vod: Path, profile: str) -> list[float]:
    try:
        from highlight_scorer import _owner_anchor_starts
    except ImportError:
        return []
    try:
        return [float(t) for t in _owner_anchor_starts(vod, profile)]
    except Exception as exc:  # noqa: BLE001 — best-effort seed
        log.debug("highlight owner anchors failed: %s", exc)
        return []


def _peaks_from_feedback_labels(game: str, vod: Path) -> list[float]:
    """👍 feedback on previously sent segments of this VOD."""
    from shooter_vod_segment_store import _paths

    path = _paths(game)["labels"]
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    vid = _video_id(vod)
    out: list[float] = []
    for row in data.get("good", []) + [
        r for r in data.get("feedback", []) if r.get("owner_label") in ("yes", "good")
    ]:
        sid = str(row.get("segment_id") or "")
        vod_field = str(row.get("vod") or "")
        row_vid = ""
        if vod_field:
            vp = Path(vod_field)
            row_vid = vp.stem[3:] if vp.stem.startswith("yt_") else vp.stem
        elif sid.startswith(f"{vid}_"):
            row_vid = vid
        if row_vid != vid:
            continue
        peak = row.get("peak_start", row.get("start"))
        if peak is None and sid.startswith(f"{vid}_"):
            tail = sid[len(vid) + 1 :]
            if tail.startswith("m") and "_" in tail:
                try:
                    out.extend(float(x) for x in tail[1:].split("_") if x.replace(".", "", 1).isdigit())
                except ValueError:
                    pass
                continue
            try:
                peak = float(tail.rsplit("_", 1)[-1])
            except ValueError:
                continue
        try:
            out.append(float(peak))
        except (TypeError, ValueError):
            continue
    return out


def _owner_bad_peaks(game: str, vod: Path) -> list[tuple[float, str]]:
    """Owner 👎 peaks with optional dislike reason (no_kill, loot_run, …)."""
    if game != "pubg":
        return []
    out: list[tuple[float, str]] = []
    try:
        from daily_game_cycle import profile_for_game
        from vod_owner_learning import owner_labels_for_vod_scan

        for row in owner_labels_for_vod_scan(vod, profile_for_game(game)):
            if str(row.get("label") or "") != "bad":
                continue
            try:
                t = float(row["time_sec"])
            except (KeyError, TypeError, ValueError):
                continue
            out.append((t, str(row.get("note") or "")))
    except Exception as exc:  # noqa: BLE001
        log.debug("owner bad peaks load failed: %s", exc)

    from shooter_vod_segment_store import _paths

    path = _paths(game)["labels"]
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        vid = _video_id(vod)
        for row in data.get("bad", []) + [
            r for r in data.get("feedback", []) if r.get("owner_label") in ("no", "bad")
        ]:
            sid = str(row.get("segment_id") or "")
            row_vid = ""
            vod_field = str(row.get("vod") or "")
            if vod_field:
                vp = Path(vod_field)
                row_vid = vp.stem[3:] if vp.stem.startswith("yt_") else vp.stem
            elif sid.startswith(f"{vid}_"):
                row_vid = vid
            if row_vid != vid:
                continue
            peak = row.get("peak_start", row.get("start"))
            if peak is None and "_" in sid:
                try:
                    peak = float(sid.rsplit("_", 1)[-1])
                except ValueError:
                    continue
            try:
                out.append((float(peak), str(row.get("reason") or "")))
            except (TypeError, ValueError):
                continue
    deduped: list[tuple[float, str]] = []
    for t, reason in sorted(out, key=lambda x: x[0]):
        if any(abs(t - p) <= 4.0 for p, _ in deduped):
            continue
        deduped.append((t, reason))
    return deduped


def _reject_radius_for_reason(reason: str, *, default: float = 20.0) -> float:
    """Wider block zone for recurring owner trash patterns."""
    r = reason.strip().lower()
    if r in ("no_kill", "no_combat", "loot_run", "not_metro", "classic"):
        return max(default, 30.0)
    if r in ("promo", "boring", "blurry", "not_gameplay"):
        return max(default, 22.0)
    return default


def _is_owner_rejected_peak(game: str, vod: Path, peak_sec: float, *, radius: float = 20.0) -> bool:
    if game != "pubg":
        return False
    try:
        from pubg_owner_style import style_avoid_peaks

        for t in style_avoid_peaks(vod):
            if abs(float(peak_sec) - float(t)) <= max(radius, 25.0):
                return True
    except ImportError:
        pass
    vid = _video_id(vod)
    for t in PUBG_OWNER_REJECTED_PEAKS.get(vid, []):
        if abs(float(peak_sec) - float(t)) <= radius:
            return True
    for t, reason in _owner_bad_peaks(game, vod):
        block_r = _reject_radius_for_reason(reason, default=radius)
        if abs(float(peak_sec) - float(t)) <= block_r:
            return True
    return False


def owner_labeled_good_times(game: str, vod: Path) -> list[float]:
    """Owner 👍 timestamps for this VOD — always available for learning/trust.

    Unlike owner_good_fight_peaks(), this is NOT gated by
    PUBG_OWNER_LABEL_SEED_SENDS. Ratings must affect nearby trust / ranking
    even when labels are forbidden from becoming the send queue.
    """
    if game != "pubg":
        return []
    out: list[float] = []
    try:
        from daily_game_cycle import profile_for_game
        from vod_owner_learning import owner_labels_for_vod_scan

        for row in owner_labels_for_vod_scan(vod, profile_for_game(game)):
            if str(row.get("label") or "") != "good":
                continue
            try:
                out.append(float(row["time_sec"]))
            except (KeyError, TypeError, ValueError):
                continue
    except Exception as exc:  # noqa: BLE001
        log.debug("owner good labels load failed: %s", exc)

    # Also read segment-store good bucket directly (source of Telegram 👍).
    try:
        from shooter_vod_segment_store import _paths

        path = _paths(game)["labels"]
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            vid = _video_id(vod)
            for row in data.get("good", []):
                sid = str(row.get("segment_id") or "")
                vod_field = str(row.get("vod") or "")
                row_vid = ""
                if vod_field:
                    vp = Path(vod_field)
                    row_vid = vp.stem[3:] if vp.stem.startswith("yt_") else vp.stem
                elif sid.startswith(f"{vid}_"):
                    row_vid = vid
                if row_vid != vid:
                    continue
                peak = row.get("peak_start", row.get("start"))
                if peak is None and "_" in sid:
                    try:
                        peak = float(sid.rsplit("_", 1)[-1])
                    except ValueError:
                        continue
                try:
                    out.append(float(peak))
                except (TypeError, ValueError):
                    continue
    except Exception as exc:  # noqa: BLE001
        log.debug("owner good segment labels load failed: %s", exc)

    deduped: list[float] = []
    for t in sorted(out):
        if any(abs(t - p) <= 4.0 for p in deduped):
            continue
        deduped.append(float(t))
    return deduped


def owner_good_fight_peaks(game: str, vod: Path) -> list[float]:
    """Deduped owner-good fight times for optional send-queue seeding.

    Owner timestamps calibrate style/combat-act floors. They must NOT be
    prepended into the live Telegram send order unless explicitly enabled.
    For trust / ranking near 👍, use owner_labeled_good_times() /
    peak_near_owner_good() instead — those stay on even when seeding is off.
    """
    if not owner_anchor_montage_enabled():
        return []
    # PUBG singles production: labels teach the bot; they do not pick clips.
    if game == "pubg" and os.environ.get("PUBG_OWNER_LABEL_SEED_SENDS", "0") != "1":
        return []
    profile = {
        "pubg": "pubg",
        "standoff": "standoff",
        "wot": "wot",
        "genshin": "genshin",
    }.get(game, game)
    peaks: list[float] = []
    vid = _video_id(vod)
    if game == "pubg":
        try:
            from pubg_owner_style import style_reference_peaks

            peaks.extend(style_reference_peaks(vod))
        except ImportError:
            pass
        peaks.extend(PUBG_BRAWL_ANCHORS_BY_VOD.get(vid, []))
        peaks.extend(_peaks_from_pubg_calibration(vod))
    peaks.extend(_peaks_from_highlight_labels(vod, profile))
    peaks.extend(_peaks_from_feedback_labels(game, vod))
    # Explicit Telegram 👍 times when seeding is enabled.
    peaks.extend(owner_labeled_good_times(game, vod))
    peaks.sort()
    deduped: list[float] = []
    # Owner-marked fight acts must stay in the pool — including early-VOD
    # openers (owner 6mWLqNBX1pE @0:06). The old t<45 skip dropped real fights.
    min_t = float(os.environ.get("SHOOTER_OWNER_GOOD_MIN_PEAK_SEC", "0"))
    for t in peaks:
        if t < min_t:
            continue
        if game == "pubg" and any(abs(t - s) <= 2.0 for s in PUBG_SNIPER_SKIP):
            continue
        if _is_owner_rejected_peak(game, vod, t):
            continue
        if any(abs(t - p) <= 8.0 for p in deduped):
            continue
        deduped.append(float(t))
    return deduped


def vod_has_owner_montage_anchors(game: str, vod: Path, *, min_clips: int = 3) -> bool:
    return len(owner_good_fight_peaks(game, vod)) >= min_clips


def owner_good_pool(
    game: str,
    vod: Path,
    *,
    lead_sec: float = 6.0,
    part_sec: float = 18.0,
) -> list[dict]:
    """Hint rows from owner-good peaks (modest score — not exclusive top picks)."""
    peaks = owner_good_fight_peaks(game, vod)
    if not peaks:
        return []
    hint_score = float(os.environ.get("SHOOTER_VOD_OWNER_ANCHOR_HINT_SCORE", "0.55"))
    pool: list[dict] = []
    for peak in peaks:
        pool.append(
            {
                "start": float(peak),
                "peak_start": float(peak),
                "score": hint_score,
                "input_duration": part_sec,
                "output_duration": part_sec,
                "highlight_metrics": {"clip_score": hint_score, "owner_anchor": True},
                "owner_anchor": True,
                "gate_reason": "owner_good_hint",
            }
        )
    log.info(
        "owner-anchor hints game=%s vod=%s peaks=%s (merged into normal pool)",
        game,
        vod.name,
        [int(p) for p in peaks],
    )
    return pool


def merge_owner_hints_into_pool(pool: list[dict], owner_hints: list[dict]) -> list[dict]:
    """Boost / inject owner peaks into the normal candidate pool (dedupe by ~10s)."""
    if not owner_hints:
        return pool
    boost = float(os.environ.get("SHOOTER_VOD_OWNER_ANCHOR_SCORE_BOOST", "0.12"))
    merged: list[dict] = [dict(c) for c in pool]
    for hint in owner_hints:
        peak = float(hint.get("start", hint.get("peak_start", 0)))
        matched = False
        for clip in merged:
            cpeak = float(clip.get("start", clip.get("peak_start", 0)))
            if abs(cpeak - peak) <= 10.0:
                clip["score"] = float(clip.get("score", 0)) + boost
                hm = dict(clip.get("highlight_metrics") or {})
                hm["clip_score"] = float(hm.get("clip_score") or clip.get("score") or 0) + boost
                hm["owner_anchor_hint"] = True
                clip["highlight_metrics"] = hm
                clip["owner_anchor"] = True
                matched = True
                break
        if not matched:
            merged.append(dict(hint))
    merged.sort(
        key=lambda c: (1 if c.get("owner_anchor") else 0, float(c.get("score", 0))),
        reverse=True,
    )
    return merged


def peak_near_owner_good(
    game: str,
    vod: Path,
    peak_sec: float,
    *,
    radius_sec: float | None = None,
) -> bool:
    radius = float(
        radius_sec
        if radius_sec is not None
        else os.environ.get("SHOOTER_VOD_OWNER_ANCHOR_RADIUS_SEC", "45")
    )
    # Always consult stored 👍 labels — independent of send-queue seeding.
    labeled = owner_labeled_good_times(game, vod)
    for t in labeled:
        if abs(float(peak_sec) - t) <= radius:
            return True
    # Fallback: seeded/calibration peaks when explicitly enabled.
    for t in owner_good_fight_peaks(game, vod):
        if abs(float(peak_sec) - t) <= radius:
            return True
    return False


def owner_neighborhood_probe_peaks(
    game: str,
    vod: Path,
    *,
    offsets: tuple[float, ...] | None = None,
) -> list[float]:
    """Extra probe times near owner 👍 so the pool actually contains learnable fights.

    Does not enqueue labels as sends — only expands discovery around what the
    owner already marked good. Skips owner-👎 neighborhoods.
    """
    goods = owner_labeled_good_times(game, vod)
    if not goods:
        return []
    if offsets is None:
        raw = os.environ.get("SHOOTER_VOD_OWNER_PROBE_OFFSETS_SEC", "0,28,-28,55,-55,90")
        offsets = tuple(
            float(x.strip()) for x in raw.split(",") if x.strip()
        ) or (0.0, 28.0, -28.0, 55.0, -55.0)
    out: list[float] = []
    for g in goods:
        for off in offsets:
            peak = max(20.0, float(g) + float(off))
            if _is_owner_rejected_peak(game, vod, peak):
                continue
            if any(abs(peak - p) <= 8.0 for p in out):
                continue
            out.append(peak)
    return out


def resolve_owner_neighborhood_bounds(
    vod: Path,
    peak: float,
    *,
    lead_fallback: float = 8.0,
    dur_fallback: float = 45.0,
) -> tuple[float, float]:
    """Trim empty lead and keep the gun act — fixed lead/dur kept cutting mid-fight.

    Scan ± around peak for gun onset / offset. Used for near-👍 assists so
    openings are not loot/run and tails are not truncated on live fire
    (owner: zRQC@3960 useless start + cut mid-gun; zRQC@4264 ended mid-burst
    because offset required high burst while gun was still hot, then min_dur
    padded into the climax).
    """
    try:
        from pubg_shooting_gate import pubg_probe_segment
    except Exception:
        start = max(0.0, float(peak) - float(lead_fallback))
        return start, float(dur_fallback)

    min_gun = float(os.environ.get("PUBG_OWNER_NEIGHBORHOOD_ONSET_GUN", "0.055"))
    min_burst = float(os.environ.get("PUBG_OWNER_NEIGHBORHOOD_ONSET_BURST", "3.2"))
    # Continuation can be high gun / low burst (full-auto smear) — do not stop there.
    cont_gun = float(os.environ.get("PUBG_OWNER_NEIGHBORHOOD_CONT_GUN", "0.04"))
    quiet_gun = float(os.environ.get("PUBG_OWNER_NEIGHBORHOOD_QUIET_GUN", "0.035"))
    # Reload / peek gaps are often 3–5s; require a longer quiet before ending.
    quiet_need = float(os.environ.get("PUBG_OWNER_NEIGHBORHOOD_QUIET_SEC", "6.0"))
    look_ahead = float(os.environ.get("PUBG_OWNER_NEIGHBORHOOD_QUIET_LOOKAHEAD_SEC", "10.0"))
    # Defaults trimmed so near-👍 windows ship fight, not long run/loot pads.
    max_dur = float(os.environ.get("PUBG_OWNER_NEIGHBORHOOD_MAX_DUR_SEC", "55"))
    min_dur = float(os.environ.get("PUBG_OWNER_NEIGHBORHOOD_MIN_DUR_SEC", "28"))
    # Extra room past max_dur only to land on quiet instead of mid-burst.
    quiet_grace = float(os.environ.get("PUBG_OWNER_NEIGHBORHOOD_QUIET_GRACE_SEC", "8"))
    pad_before = float(os.environ.get("PUBG_OWNER_NEIGHBORHOOD_PAD_BEFORE_SEC", "1.5"))
    pad_after = float(os.environ.get("PUBG_OWNER_NEIGHBORHOOD_PAD_AFTER_SEC", "3.0"))
    step = 3.0
    peak_v = float(peak)
    probe_cache: dict[int, dict] = {}

    def _probe(t: float, win: float = 3.0) -> dict:
        key = int(round(float(t) * 2))
        hit = probe_cache.get(key)
        if hit is not None:
            return hit
        row = pubg_probe_segment(vod, float(t), float(win))
        probe_cache[key] = row
        return row

    def _gun_at(t: float) -> float:
        if t < 0:
            return 0.0
        return float(_probe(t).get("gunfire_density") or 0.0)

    def _gunny(t: float) -> bool:
        """Strict onset detector — real fight start, not run-in."""
        if t < 0:
            return False
        if _is_owner_rejected_peak("pubg", vod, t):
            return False
        row = _probe(t, 6.0)
        gun = float(row.get("gunfire_density") or 0.0)
        burst = float(row.get("burst_ratio") or 0.0)
        motion = float(row.get("center_motion") or 0.0)
        # High locomotion + weak gun = run-in junk (3960 lead).
        if motion >= 0.13 and gun < min_gun:
            return False
        return gun >= min_gun and burst >= min_burst

    def _still_hot(t: float) -> bool:
        """Offset / tail: any clear gun means the act is not over."""
        if t < 0:
            return False
        if _is_owner_rejected_peak("pubg", vod, t):
            return False
        return _gun_at(t) >= cont_gun

    def _quiet_enough(t: float) -> bool:
        return _gun_at(t) < quiet_gun and not _is_owner_rejected_peak("pubg", vod, t)

    onset = peak_v
    for back in range(0, 36, int(step)):
        t = peak_v - float(back)
        if t < 5:
            break
        if _gunny(t):
            onset = t
        else:
            if back > 0:
                break
    # Walk further back while still gunny to find true onset.
    t = onset
    while t > 5:
        prev = t - step
        if not _gunny(prev):
            break
        t = prev
        onset = t

    def _hot_in_window(t0: float, span: float) -> float | None:
        """Return first hot timestamp in [t0, t0+span], sampling finely."""
        tt = float(t0)
        limit = float(t0) + float(span)
        # 2s is enough to catch mid-burst without 1s×full-probe hangs (owner drought).
        fine = float(os.environ.get("PUBG_OWNER_NEIGHBORHOOD_FINE_STEP_SEC", "2.0"))
        fine = max(1.0, fine)
        while tt <= limit + 0.05:
            if _is_owner_rejected_peak("pubg", vod, tt):
                return None
            if _still_hot(tt):
                return tt
            tt += fine
        return None

    def _heartbeat(phase: str) -> None:
        try:
            from vod_hang_detector import write_heartbeat

            write_heartbeat(
                "pubg",
                phase,
                vod=vod.name,
                peak=peak_v,
                probes=len(probe_cache),
            )
        except Exception:
            pass

    _heartbeat("owner_neighborhood_bounds")
    offset = peak_v
    t = peak_v
    hard_end = onset + max_dur + quiet_grace
    last_hb = time.monotonic()
    while t < hard_end:
        if time.monotonic() - last_hb >= 20.0:
            _heartbeat("owner_neighborhood_offset")
            last_hb = time.monotonic()
        nxt = t + step
        if _is_owner_rejected_peak("pubg", vod, nxt):
            break
        if _still_hot(nxt) or _still_hot(nxt - 1.0):
            offset = nxt
            t = nxt
            continue
        # Brief lull — peek ahead before declaring the act over.
        resume_at = _hot_in_window(nxt, look_ahead)
        if resume_at is not None:
            offset = resume_at
            t = resume_at
            continue
        break

    start = max(0.0, onset - pad_before)
    end = offset + pad_after
    dur = end - start
    if dur < min_dur:
        # Never pad blindly into a hotter climax (4264: 35s landed on peak gun).
        end = start + min_dur
        t = end
        while t < start + max_dur + quiet_grace:
            if _still_hot(t) or _still_hot(t - 1.0):
                end = t + pad_after
                t += step
                continue
            resume_at = _hot_in_window(t, look_ahead)
            if resume_at is not None:
                end = resume_at + pad_after
                t = resume_at
                continue
            break
        dur = end - start

    # If the chosen end is still mid-burst, walk to a quiet landing.
    if _still_hot(end - 1.0) or _still_hot(end - 2.0):
        quiet = 0.0
        t = end
        landed = False
        while t < start + max_dur + quiet_grace:
            if _is_owner_rejected_peak("pubg", vod, t):
                break
            if _still_hot(t):
                quiet = 0.0
                end = t + pad_after
                t += step
                continue
            if _quiet_enough(t):
                quiet += step
                end = t + 1.0
                if quiet >= quiet_need:
                    resume_at = _hot_in_window(t + 1.0, look_ahead)
                    if resume_at is not None:
                        quiet = 0.0
                        t = resume_at
                        end = t + pad_after
                        continue
                    landed = True
                    break
            else:
                quiet = 0.0
            t += step
        if not landed and (end - start) > max_dur:
            # Still hot at grace cap — keep the last max_dur (payoff), drop early lead.
            target_end = end
            new_start = max(0.0, target_end - max_dur)
            if new_start > start:
                start = max(onset - pad_before, new_start) if onset > new_start else new_start
        dur = end - start

    # Final pass: gun just past the cut still means mid-act (owner zRQC@4264).
    guard = 0
    while guard < 40:
        guard += 1
        resume_at = _hot_in_window(end - 0.5, look_ahead)
        if resume_at is None or resume_at < end - 1.0:
            break
        if (resume_at - start) > max_dur + quiet_grace:
            break
        end = max(end, resume_at + pad_after)
        quiet = 0.0
        t = end
        while t < start + max_dur + quiet_grace:
            if _still_hot(t):
                quiet = 0.0
                end = t + pad_after
                t += step
                continue
            if _quiet_enough(t):
                quiet += step
                end = t + 1.0
                if quiet >= quiet_need:
                    break
            else:
                quiet = 0.0
            t += step
        dur = end - start

    # Soft cap: allow quiet_grace past max_dur only to avoid mid-burst cuts.
    if dur > max_dur + quiet_grace:
        end = start + max_dur + quiet_grace
        dur = end - start
    elif dur > max_dur and not (_still_hot(end - 1.0) or _still_hot(end - 2.0)):
        # Already quiet — trim grace fat if we overshot a lot without need.
        if dur > max_dur + 8.0:
            end = start + min(dur, max_dur + quiet_grace)
            dur = end - start

    # Never enter a known 👎 neighborhood at the tail.
    if _is_owner_rejected_peak("pubg", vod, end - 1.0):
        end = max(start + min_dur, end - 12.0)
        dur = end - start
    log.info(
        "owner-neighborhood bounds peak=%.1f start=%.1f end=%.1f dur=%.1f",
        peak_v,
        start,
        end,
        dur,
    )
    return float(start), float(dur)


def build_owner_neighborhood_send_rows(
    game: str,
    vod: Path,
    *,
    blocked_ids: set[str] | None = None,
    used_peaks: list[float] | None = None,
    gap_sec: float = 20.0,
    peak_too_close: Callable[[float, list[float], float], bool] | None = None,
) -> list[dict]:
    """Same window recipe as the working near-👍 manual send — for the live bot.

    Fixed lead/dur with bounds_locked so fight-segmenter cannot remap a good
    neighborhood into loot/menu and then hard-reject it.
    Exact already-liked peaks are skipped; we only ship *neighbors*.
    """
    if game != "pubg":
        return []
    if os.environ.get("PUBG_OWNER_NEIGHBORHOOD_DIRECT", "1") != "1":
        return []
    goods = owner_labeled_good_times(game, vod)
    if not goods:
        return []
    from shooter_vod_segment_store import segment_id

    blocked = set(blocked_ids or [])
    used = list(used_peaks or [])
    lead = float(os.environ.get("PUBG_OWNER_NEIGHBORHOOD_LEAD_SEC", "8"))
    # Fallback only — real bounds snap to gun onset/offset at prepare time.
    dur = float(os.environ.get("PUBG_OWNER_NEIGHBORHOOD_DUR_SEC", "45"))
    # Snapping every candidate at build time is too slow; prepare does the snap.
    snap = os.environ.get("PUBG_OWNER_NEIGHBORHOOD_SNAP_AT_BUILD", "0") == "1"
    # Skip offset 0 — that is the already-rated 👍 peak itself.
    raw = os.environ.get(
        "PUBG_OWNER_NEIGHBORHOOD_OFFSETS_SEC",
        "28,-28,55,-55,90,-90,120",
    )
    offsets = tuple(float(x.strip()) for x in raw.split(",") if x.strip()) or (
        28.0,
        -28.0,
        55.0,
        -55.0,
    )
    vid = _video_id(vod)
    rows: list[dict] = []
    # Prefer dense 👍 clusters (manual wins were mid-VOD fights, not end-loot).
    # Round-robin offsets across goods so prescore budget is not burned on one
    # late-VOD peak's ±28/55/90 before ever trying the liked fight belt.
    def _cluster_rank(g: float) -> tuple[int, float]:
        near = sum(1 for x in goods if abs(float(x) - float(g)) <= 90.0)
        return (-near, -float(g))

    goods_ordered = sorted(goods, key=_cluster_rank)
    for off in offsets:
        for g in goods_ordered:
            peak = max(25.0, float(g) + float(off))
            if abs(peak - float(g)) < 12.0:
                continue
            if _is_owner_rejected_peak(game, vod, peak):
                continue
            if peak_too_close is not None and peak_too_close(peak, used, gap_sec):
                continue
            if any(abs(peak - float(u)) <= gap_sec for u in used):
                continue
            if snap:
                try:
                    start, clip_dur = resolve_owner_neighborhood_bounds(
                        vod, peak, lead_fallback=lead, dur_fallback=dur
                    )
                except Exception:
                    start = max(0.0, peak - lead)
                    clip_dur = dur
            else:
                start = max(0.0, peak - lead)
                clip_dur = dur
            sid = segment_id(vid, start)
            if sid in blocked:
                continue
            if any(abs(start - float(r.get("start") or 0)) < 20.0 for r in rows):
                continue
            rows.append(
                {
                    "segment_id": sid,
                    "start": float(start),
                    "peak_start": float(peak),
                    "score": 0.97,
                    "owner_anchor": True,
                    "owner_neighborhood_direct": True,
                    "clip": {
                        "start": float(start),
                        "peak_start": float(peak),
                        "input_duration": float(clip_dur),
                        "output_duration": float(clip_dur),
                        "bounds_locked": True,
                        "owner_anchor": True,
                        "owner_neighborhood_direct": True,
                        "gun_snapped": bool(snap),
                    },
                }
            )
    log.info(
        "owner-neighborhood direct rows vod=%s n=%s goods=%s",
        vod.name,
        len(rows),
        [int(g) for g in goods_ordered[:8]],
    )
    return rows


def prescore_owner_neighborhood_rows(
    vod: Path,
    rows: list[dict],
    *,
    max_score: int | None = None,
    keep: int | None = None,
) -> list[dict]:
    """Keep only near-👍 windows that already pass score_pubg_window (manual recipe)."""
    if not rows:
        return []
    max_score = int(
        max_score
        if max_score is not None
        else os.environ.get("PUBG_OWNER_NEIGHBORHOOD_PRESCORE_MAX", "24")
    )
    keep_n = int(
        keep
        if keep is not None
        else os.environ.get("PUBG_OWNER_NEIGHBORHOOD_PRESCORE_KEEP", "4")
    )
    from pubg_quality_score import score_pubg_window

    kept: list[dict] = []
    scored = 0
    for row in rows:
        if scored >= max_score or len(kept) >= keep_n:
            break
        start = float(row.get("start") or (row.get("clip") or {}).get("start") or 0)
        dur = float(
            (row.get("clip") or {}).get("input_duration")
            or os.environ.get("PUBG_OWNER_NEIGHBORHOOD_DUR_SEC", "45")
        )
        scored += 1
        try:
            ok, reason, report = score_pubg_window(
                vod, start, dur, single=True, use_cache=True
            )
        except Exception as exc:  # noqa: BLE001
            log.info(
                "owner-neighborhood prescore error start=%.1f: %s", start, exc
            )
            continue
        if not ok:
            log.info(
                "owner-neighborhood prescore skip start=%.1f reason=%s",
                start,
                str(reason)[:80],
            )
            continue
        out = dict(row)
        q = float((report or {}).get("quality_score") or 0.0)
        out["score"] = max(float(out.get("score") or 0.0), 0.90 + min(0.09, q))
        out["quality_metrics"] = report or {}
        if (report or {}).get("fight_candidate_owner_review"):
            out["fight_candidate"] = True
        kept.append(out)
        log.info(
            "owner-neighborhood prescore KEEP start=%.1f peak=%.1f q=%.3f",
            start,
            float(out.get("peak_start") or 0),
            q,
        )
    log.info(
        "owner-neighborhood prescore vod=%s scored=%s kept=%s",
        vod.name,
        scored,
        len(kept),
    )
    return kept


def boost_pool_near_owner_labels(
    game: str,
    vod: Path,
    pool: list[dict],
) -> list[dict]:
    """Raise scores of candidates near owner 👍 without injecting label-only sends."""
    goods = owner_labeled_good_times(game, vod)
    if not pool:
        return pool
    # Wider than old 18s — Metro fights drift; 👍 must still pull neighbors up.
    boost = float(os.environ.get("SHOOTER_VOD_OWNER_ANCHOR_SCORE_BOOST", "0.22"))
    radius = float(os.environ.get("SHOOTER_VOD_OWNER_ANCHOR_RADIUS_SEC", "45"))
    demote = float(os.environ.get("SHOOTER_VOD_OWNER_BAD_SCORE_DEMOTE", "0.35"))
    merged: list[dict] = [dict(c) for c in pool]
    boosted = 0
    demoted = 0
    for clip in merged:
        peak = float(clip.get("start", clip.get("peak_start", 0)) or 0)
        if _is_owner_rejected_peak(game, vod, peak):
            clip["score"] = max(0.01, float(clip.get("score", 0) or 0) - demote)
            hm = dict(clip.get("highlight_metrics") or {})
            hm["owner_bad_demote"] = True
            clip["highlight_metrics"] = hm
            clip["owner_bad"] = True
            demoted += 1
            continue
        if goods and any(abs(peak - g) <= radius for g in goods):
            clip["score"] = float(clip.get("score", 0) or 0) + boost
            hm = dict(clip.get("highlight_metrics") or {})
            hm["owner_label_boost"] = True
            clip["highlight_metrics"] = hm
            clip["owner_anchor"] = True
            boosted += 1
    if boosted or demoted:
        log.info(
            "owner-label boost game=%s vod=%s boosted=%s demoted=%s goods=%s",
            game,
            vod.name,
            boosted,
            demoted,
            [int(g) for g in goods[:8]],
        )
        merged.sort(
            key=lambda c: (
                0 if c.get("owner_bad") else 1,
                1 if c.get("owner_anchor") else 0,
                float(c.get("score", 0) or 0),
            ),
            reverse=True,
        )
    return merged


def _gunfire_evidence(metrics: dict | None, gate_reason: str) -> bool:
    """Require real shots — soft paths must not ship talk/loot."""
    m = metrics or {}
    gun = float(m.get("gunfire_density") or 0.0)
    burst = float(m.get("burst_ratio") or 0.0)
    panns = float(m.get("panns_gun_max") or 0.0)
    min_gun = float(os.environ.get("SHOOTER_VOD_SOFT_MIN_GUN", "0.055"))
    min_burst = float(os.environ.get("SHOOTER_VOD_SOFT_MIN_BURST", "4.5"))
    min_panns = float(os.environ.get("SHOOTER_VOD_SOFT_MIN_PANNS", "0.40"))
    if gun >= min_gun and burst >= min_burst:
        return True
    if panns >= min_panns:
        return True
    # Fallback parse from reason strings like gun0.063 / density0.04
    reason = str(gate_reason or "")
    for token in ("gun", "density"):
        idx = reason.find(token)
        if idx < 0:
            continue
        tail = reason[idx + len(token) :].lstrip("=:")
        num = []
        for ch in tail:
            if ch.isdigit() or ch == ".":
                num.append(ch)
            else:
                break
        if num:
            try:
                if float("".join(num)) >= min_gun and burst >= min_burst * 0.9:
                    return True
            except ValueError:
                pass
    return False


def soft_allow_owner_montage_part(
    game: str,
    vod: Path,
    peak_sec: float,
    gate_ok: bool,
    gate_reason: str,
    *,
    montage_part: bool = False,
    metrics: dict | None = None,
) -> tuple[bool, str]:
    """Near owner-good: forgive *borderline* gates only when gunfire evidence exists.

    Never soft-allow talk/loot/run-without-shots — that shipped today's trash.
    SHOOTER_VOD_MONTAGE_SOFT_GATE no longer bypasses combat quality.
    """
    del montage_part  # kept for call-site compatibility
    if gate_ok:
        if owner_anchor_montage_enabled() and peak_near_owner_good(game, vod, peak_sec):
            return True, f"owner_hint+{gate_reason}"
        return True, gate_reason

    hard = ("owner_bad_window", "metro_", "not_metro")
    if any(str(gate_reason).startswith(h) for h in hard):
        return False, gate_reason

    if _is_owner_rejected_peak(game, vod, peak_sec):
        return False, f"owner_rejected_peak={gate_reason}"

    base = str(gate_reason).split("=", 1)[0].strip().lower()
    reason_l = str(gate_reason).lower()
    # Global fight-act rescue (owner 6mWLqNBX1pE principle): run_fake_gun /
    # no_shots soft-pass on ANY VOD when audio matches the combat-act profile.
    # Do not require per-video owner labels.
    act_rescue = {
        "run_fake_gun",
        "no_shots",
        "run_no_shots",
        "low_gunfire",
        "weak_shots",
    }
    if base in act_rescue or any(n in reason_l for n in act_rescue):
        gun = float((metrics or {}).get("gunfire_density") or (metrics or {}).get("gun") or 0.0)
        burst = float((metrics or {}).get("burst_ratio") or (metrics or {}).get("burst") or 0.0)
        combat_ok = False
        try:
            from pubg_fight_act_profile import is_combat_act

            combat_ok = is_combat_act(gun, burst)
        except Exception:
            combat_ok = False
        if (
            os.environ.get("SHOOTER_VOD_COMBAT_ACT_SOFT_ALLOW", "1") == "1"
            and combat_ok
            and _gunfire_evidence(metrics, gate_reason)
        ):
            return True, f"combat_act_soft={gate_reason}"
        if (
            owner_anchor_montage_enabled()
            and os.environ.get("SHOOTER_VOD_OWNER_ACT_SOFT_ALLOW", "0") == "1"
            and peak_near_owner_good(game, vod, peak_sec)
            and _gunfire_evidence(metrics, gate_reason)
        ):
            return True, f"owner_act_soft={gate_reason}"

    if base in NEVER_SOFT_ALLOW_REASONS or any(n in reason_l for n in NEVER_SOFT_ALLOW_REASONS):
        return False, gate_reason

    if base not in BORDERLINE_SOFT_REASONS and not any(
        b in reason_l for b in BORDERLINE_SOFT_REASONS
    ):
        return False, gate_reason

    if not _gunfire_evidence(metrics, gate_reason):
        return False, gate_reason

    if owner_anchor_montage_enabled() and os.environ.get("SHOOTER_VOD_OWNER_ANCHOR_SOFT_ALLOW", "0") == "1":
        if peak_near_owner_good(game, vod, peak_sec):
            return True, f"owner_hint_soft={gate_reason}"

    return False, gate_reason
