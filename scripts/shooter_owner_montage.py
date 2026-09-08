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


def owner_good_fight_peaks(game: str, vod: Path) -> list[float]:
    """Deduped owner-good fight times (hints only — never the send queue).

    Owner timestamps calibrate style/combat-act floors. They must NOT be
    prepended into the live Telegram send order unless explicitly enabled.
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
        else os.environ.get("SHOOTER_VOD_OWNER_ANCHOR_RADIUS_SEC", "18")
    )
    for t in owner_good_fight_peaks(game, vod):
        if abs(float(peak_sec) - t) <= radius:
            return True
    return False


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
