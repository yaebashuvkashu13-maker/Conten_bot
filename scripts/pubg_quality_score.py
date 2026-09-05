#!/usr/bin/env python3
"""PUBG presend quality fusion: hard-reject junk, score ambiguous signals."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _owner_redo_trusted(video_path: Path, start_sec: float, duration_sec: float) -> bool:
    """Owner explicitly approved this fight window — skip automated hard rejects."""
    if os.environ.get("PUBG_OWNER_REDO", "0") != "1":
        return False
    pad = float(os.environ.get("PUBG_OWNER_REDO_RADIUS_SEC", "45"))
    end = float(start_sec) + float(duration_sec)
    try:
        from pubg_owner_calibration import labels_for_video

        for row in labels_for_video(video_path):
            if str(row.get("label") or "") != "good":
                continue
            if str(row.get("role") or "").lower() == "anti_style":
                continue
            t = float(row["time_sec"])
            if (float(start_sec) - pad) <= t <= (end + pad):
                return True
        from shooter_owner_montage import peak_near_owner_good

        probes = (
            start_sec + duration_sec * 0.5,
            start_sec + min(4.0, duration_sec * 0.2),
            start_sec + max(duration_sec - 4.0, duration_sec * 0.8),
        )
        return any(
            peak_near_owner_good("pubg", video_path, float(t), radius_sec=pad) for t in probes
        )
    except Exception:
        return False


def _owner_bad(video_path: Path, start_sec: float, duration_sec: float) -> bool:
    if os.environ.get("PUBG_OWNER_BAD_HARD_REJECT", "1") != "1":
        return False
    try:
        from pubg_owner_calibration import owner_bad_pad_sec, segment_overlaps_owner_label

        return bool(
            segment_overlaps_owner_label(
                video_path,
                start_sec,
                duration_sec,
                label="bad",
                pad_sec=owner_bad_pad_sec(),
            )
        )
    except Exception:
        return False


def _primary_has_kill(
    *,
    notification_hit: bool,
    keyword_hit: bool,
    killfeed: float,
    best_flash: float,
    best_weapon: float,
    gun: float,
    motion: float,
) -> bool:
    """Kill from notification/killfeed first; flash/weapon only as weak backup."""
    if notification_hit:
        min_gun = float(os.environ.get("PUBG_KILL_NOTIFICATION_MIN_GUN", "0.04"))
        if not keyword_hit and gun < min_gun:
            return False
        return True
    if keyword_hit and float(killfeed) >= 0.30:
        return True
    flash_min = float(os.environ.get("SHOOTER_AUTHOR_KILL_MIN_HIT_FLASH", "0.004")) * 1.8
    weapon_min = float(os.environ.get("SHOOTER_AUTHOR_KILL_MIN_WEAPON_EDGE", "0.030")) * 1.35
    if best_flash >= flash_min and gun >= 0.065:
        return True
    if best_weapon >= weapon_min and gun >= 0.065 and motion >= 0.035:
        return True
    return False


def _compute_fight_payoff(
    *,
    gun: float,
    burst: float,
    rms: float,
    motion: float,
    panns: dict[str, float],
    panns_gun: float,
    visual_ok: bool,
    loot_walk: bool,
    notification_score: float,
    notification_hit: bool,
    notification_mode: str,
    effective_killfeed: float,
    has_kill: bool,
    author_death: bool,
    best_flash: float,
) -> tuple[float, float, dict, dict, dict, dict]:
    fight_components = {
        "gun": _clip(gun / 0.080) * 0.28,
        "burst": _clip(burst / 8.0) * 0.14,
        "motion": _clip(motion / 0.060) * 0.22,
        "panns": _clip(panns_gun / 0.45) * 0.18,
        "visual": (0.12 if visual_ok else 0.0),
        "audio_presence": _clip(rms / 0.050) * 0.06,
    }
    fight_penalties = {
        "loot_walk": 0.22 if loot_walk else 0.0,
        "speech_music": _clip(
            max(float(panns.get("panns_speech", 0.0)), float(panns.get("panns_music", 0.0)))
            - panns_gun
        )
        * 0.10,
        "visual_fail": 0.08 if not visual_ok else 0.0,
    }
    fight_score = _clip(sum(fight_components.values()) - sum(fight_penalties.values()))

    payoff_components = {
        "kill_notification": _clip(notification_score) * 0.40,
        "killfeed": _clip(effective_killfeed) * 0.28,
        "author_kill": (0.22 if has_kill else 0.0),
        "hit_flash": _clip(
            best_flash / max(float(os.environ.get("SHOOTER_AUTHOR_KILL_MIN_HIT_FLASH", "0.004")), 1e-6)
        )
        * 0.10,
    }
    payoff_penalties = {
        "no_author_kill": 0.20 if not has_kill else 0.0,
        "author_death": 0.40 if author_death and not has_kill else 0.0,
        "missing_kill_notification": (
            0.10 if notification_mode == "prefer" and not notification_hit else 0.0
        ),
    }
    payoff_score = _clip(sum(payoff_components.values()) - sum(payoff_penalties.values()))
    return (
        fight_score,
        payoff_score,
        fight_components,
        fight_penalties,
        payoff_components,
        payoff_penalties,
    )


def _singles_gun_bypass_enabled(env_key: str = "PUBG_SINGLES_GUN_PAYOFF_BYPASS") -> bool:
    """Gun-payoff bypass only under drought soften (or explicit force-on).

    Normal path defaults OFF (deploy pins 0). Drought/force_send sets the env
    key to 1; hang recover also pins 1 while VOD_FORCE_SOFTEN=1.
    Outside drought the bypass stays OFF even if a stale env pin remains.
    """
    soften = os.environ.get("VOD_FORCE_SOFTEN", "0") == "1"
    try:
        esc = int(os.environ.get("VOD_FORCE_ESCALATION", "0") or 0)
    except ValueError:
        esc = 0
    if not (soften or esc > 0):
        return False
    return os.environ.get(env_key, "0") == "1"


def score_pubg_window(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    use_cache: bool = True,
    single: bool = False,
) -> tuple[bool, str, dict[str, Any]]:
    """Return acceptance, reason and complete feature/penalty report."""
    if use_cache:
        try:
            from vod_presend_cache import get_presend, put_presend

            hit = get_presend(video_path, start_sec, duration_sec)
            if hit is not None:
                ok, reason, report = hit
                report = dict(report)
                report["presend_cache_hit"] = True
                return ok, reason, report
        except Exception:
            get_presend = None  # type: ignore
            put_presend = None  # type: ignore
    else:
        get_presend = None
        put_presend = None

    from highlight_scorer import score_panns_audio
    from pubg_combat_gate import _pubg_scan_training_ui, pubg_combat_visual_strict
    from pubg_killfeed_ocr import score_killfeed_segment
    from pubg_shooting_gate import pubg_probe_segment
    from shooter_author_kill_gate import detect_author_death_signals

    report: dict[str, Any] = {
        "start": round(float(start_sec), 3),
        "duration": round(float(duration_sec), 3),
        "score_mode": True,
        "presend_cache_hit": False,
    }

    def _finish(ok: bool, reason: str) -> tuple[bool, str, dict[str, Any]]:
        if put_presend is not None:
            try:
                put_presend(video_path, start_sec, duration_sec, ok, reason, report)
            except Exception:
                pass
        return ok, reason, report

    if _owner_bad(video_path, start_sec, duration_sec):
        report["hard_reject"] = "owner_bad_window"
        return _finish(False, "hard_owner_bad_window")

    shoot = pubg_probe_segment(video_path, start_sec, duration_sec)
    report.update(shoot)
    panns = score_panns_audio(video_path, start_sec, duration_sec)
    report.update({key: round(float(value), 4) for key, value in panns.items()})

    gun = float(shoot.get("gunfire_density", 0.0))
    burst = float(shoot.get("burst_ratio", 0.0))
    rms = float(shoot.get("audio_rms", 0.0))
    motion = float(shoot.get("center_motion", 0.0))
    panns_gun = float(panns.get("panns_gun_max", 0.0))
    center_text = float(shoot.get("center_text", 0.0))

    if os.environ.get("PUBG_PRESEND_SHOOTING_GATE", "1") == "1":
        from pubg_shooting_gate import pubg_passes_shooting_gate

        shoot_ok, shoot_reason, shoot_row = pubg_passes_shooting_gate(
            video_path,
            start_sec,
            duration_sec,
            panns_gun_max=panns_gun,
        )
        report["shooting_gate"] = shoot_row
        if not shoot_ok:
            if _owner_redo_trusted(video_path, start_sec, duration_sec):
                report["owner_redo_trusted"] = True
            else:
                report["hard_reject"] = shoot_reason.split("=", 1)[0]
                return _finish(False, shoot_reason)

    loot_walk = (
        (motion >= 0.030 and gun < 0.040)
        or (motion < 0.014 and gun < 0.028)
        or (center_text > 0.14 and gun < 0.040)
    )
    try:
        from gameplay_gate import segment_looks_like_pubg_loot_or_walk

        crop = tuple(shoot["crop_box"]) if shoot.get("crop_box") else None
        if crop is not None:
            crop = tuple(int(v) for v in crop)
        if segment_looks_like_pubg_loot_or_walk(
            video_path,
            start_sec,
            duration_sec,
            crop_box=crop,
            gunfire_density=gun,
            burst_ratio=burst,
        ):
            loot_walk = True
    except Exception:
        pass
    report["loot_walk"] = bool(loot_walk)
    report["legacy_gate_ok"] = not loot_walk

    if gun < 0.010 and panns_gun < 0.08 and rms < 0.012:
        report["hard_reject"] = "no_action"
        return _finish(False, "hard_no_action")

    if loot_walk and os.environ.get("PUBG_REJECT_LOOT_WALK", "1") == "1":
        if _owner_redo_trusted(video_path, start_sec, duration_sec):
            report["owner_redo_trusted"] = True
        else:
            report["hard_reject"] = "loot_walk"
            return _finish(False, "hard_loot_walk")

    if center_text > 0.18 and gun < 0.030 and panns_gun < 0.18:
        training, training_text = _pubg_scan_training_ui(video_path, start_sec, duration_sec)
        if training:
            report["training_ui"] = training_text
            report["hard_reject"] = "training_ui"
            return _finish(False, f"hard_training_ui={training_text}")

    # Payoff signal before expensive visual/death OCR.
    try:
        killfeed, killfeed_row = score_killfeed_segment(
            video_path, start_sec, duration_sec, "pubg"
        )
    except Exception:
        killfeed, killfeed_row = 0.0, {}
    report["killfeed_density"] = round(float(killfeed), 4)
    report["killfeed"] = killfeed_row
    notification_score = float(killfeed_row.get("notification_score", 0.0) or 0.0)
    notification_min = float(os.environ.get("PUBG_KILL_NOTIFICATION_MIN_SCORE", "0.50"))
    notification_hit = notification_score >= notification_min
    # HUD/map false-positives must never count as kill payoff (6tBEG4XXXP8_1783).
    nclass = str(
        killfeed_row.get("notification_class")
        or (killfeed_row.get("kill_notification") or {}).get("notification_class")
        or ""
    ).strip().lower()
    report["kill_notification_class"] = nclass or None
    if nclass in {"hud_fp", "map_blue", "hud_false_positive"}:
        notification_hit = False
        notification_score = min(notification_score, notification_min * 0.45)
        report["kill_notification_hud_fp_ignored"] = True
    keyword_hit = bool(killfeed_row.get("killfeed_hits"))
    effective_killfeed = float(killfeed) if (notification_hit or keyword_hit) else 0.0
    report["kill_notification_score"] = round(notification_score, 4)
    report["kill_notification_hit"] = notification_hit
    report["kill_notification_keyword_hit"] = keyword_hit

    notification_mode = os.environ.get("PUBG_KILL_NOTIFICATION_MODE", "prefer").strip().lower()
    report["kill_notification_mode"] = notification_mode

    fast_payoff_min = float(os.environ.get("PUBG_FAST_PAYOFF_MIN", "0.12"))
    fast_payoff = _clip(notification_score) * 0.65 + _clip(effective_killfeed) * 0.35
    report["fast_payoff"] = round(fast_payoff, 4)
    early_reject = os.environ.get("PUBG_EARLY_PAYOFF_REJECT", "1") == "1"
    # Singles: default OFF — killfeed OCR misses caused drought with bypass disabled.
    if single and os.environ.get("PUBG_EARLY_PAYOFF_REJECT_SINGLES", "0") != "1":
        early_reject = False
    if (
        early_reject
        and fast_payoff < fast_payoff_min
        and not notification_hit
        and not keyword_hit
    ):
        # Strong gun audio can still pass early gate when OCR killfeed is blind.
        gun_rescue = (
            single
            and _singles_gun_bypass_enabled("PUBG_SINGLES_GUN_PAYOFF_BYPASS")
            and gun >= float(os.environ.get("PUBG_SINGLE_MIN_GUN_DENSITY", "0.045"))
            and burst >= float(os.environ.get("PUBG_CLIP_MIN_BURST_RATIO", "4.8"))
            and not loot_walk
        )
        if gun_rescue:
            report["singles_gun_early_payoff_rescue"] = True
        elif _owner_redo_trusted(video_path, start_sec, duration_sec):
            report["owner_redo_trusted"] = True
        else:
            report["hard_reject"] = "early_payoff_low"
            return _finish(False, f"early_payoff_low={fast_payoff:.3f}:min{fast_payoff_min:.2f}")

    visual_ok, visual_reason, visual = pubg_combat_visual_strict(
        video_path,
        start_sec,
        duration_sec,
        "pubg",
    )
    report["visual_ok"] = visual_ok
    report["visual_reason"] = visual_reason
    report["visual"] = visual
    # Menu/lobby/loot-UI frames are never shippable — even if mid/end "pass"
    # (_-HbZ0zNDOs_2538: start=menu_overlay inventory, visual_ok still True).
    frame_reasons = [
        str(fr.get("reason") or "")
        for fr in (visual.get("frames") or [])
        if isinstance(fr, dict)
    ]
    menu_hit = "menu_overlay" in str(visual_reason or "") or any(
        "menu_overlay" in r for r in frame_reasons
    )
    if (
        os.environ.get("PUBG_HARD_REJECT_MENU_OVERLAY", "1") == "1"
        and menu_hit
        and not _owner_redo_trusted(video_path, start_sec, duration_sec)
    ):
        report["hard_reject"] = "menu_overlay"
        return _finish(
            False,
            f"hard_menu_overlay={visual_reason or ','.join(frame_reasons) or 'menu_overlay'}",
        )
    best_flash = float(visual.get("best_hit_flash", 0.0))
    best_weapon = float(visual.get("best_weapon_edge", 0.0))

    has_kill = _primary_has_kill(
        notification_hit=notification_hit,
        keyword_hit=keyword_hit,
        killfeed=float(killfeed),
        best_flash=best_flash,
        best_weapon=best_weapon,
        gun=gun,
        motion=motion,
    )
    author_death = False
    author_reason = "author_kill_signal" if has_kill else "no_author_kill"
    author: dict[str, Any] = {
        "has_author_kill": has_kill,
        "author_death": False,
        "killfeed_density": float(killfeed),
        "hit_flash": best_flash,
        "weapon_edge": best_weapon,
    }
    if not has_kill and os.environ.get("PUBG_SKIP_DEATH_OCR_WITHOUT_KILL_CANDIDATE", "1") == "1":
        author_death, death_reason, death_metrics = detect_author_death_signals(
            video_path,
            start_sec,
            duration_sec,
        )
        author["author_death"] = author_death
        author["death_metrics"] = death_metrics
        if author_death:
            author_reason = death_reason or "author_death"
    report["author_ok"] = has_kill or not author_death
    report["author_reason"] = author_reason
    report["author"] = author
    if author_death and not has_kill:
        report["hard_reject"] = "author_death"
        return _finish(False, f"hard_{author_reason or 'author_death'}")

    notification_required = (
        notification_mode == "required"
        or os.environ.get("PUBG_REQUIRE_KILL_NOTIFICATION", "0") == "1"
    )
    if notification_required and not notification_hit:
        report["quality_score"] = 0.0
        report["quality_threshold"] = float(os.environ.get("PUBG_QUALITY_SCORE_MIN", "0.48"))
        return _finish(
            False,
            f"kill_notification_missing={notification_score:.3f}:min{notification_min:.2f}",
        )

    fight_score, payoff_score, fc, fp, pc, pp = _compute_fight_payoff(
        gun=gun,
        burst=burst,
        rms=rms,
        motion=motion,
        panns=panns,
        panns_gun=panns_gun,
        visual_ok=visual_ok,
        loot_walk=loot_walk,
        notification_score=notification_score,
        notification_hit=notification_hit,
        notification_mode=notification_mode,
        effective_killfeed=effective_killfeed,
        has_kill=has_kill,
        author_death=author_death,
        best_flash=best_flash,
    )
    fight_min = float(os.environ.get("PUBG_FIGHT_SCORE_MIN", "0.38"))
    payoff_min = float(os.environ.get("PUBG_PAYOFF_SCORE_MIN", "0.28"))
    if single:
        payoff_min = float(os.environ.get("PUBG_PAYOFF_SCORE_MIN_SINGLES", "0.16"))
    report["fight_score"] = round(fight_score, 4)
    report["payoff_score"] = round(payoff_score, 4)
    report["fight_threshold"] = fight_min
    report["payoff_threshold"] = payoff_min
    report["fight_components"] = {k: round(v, 4) for k, v in fc.items()}
    report["payoff_components"] = {k: round(v, 4) for k, v in pc.items()}
    if fight_score < fight_min:
        if _owner_redo_trusted(video_path, start_sec, duration_sec):
            report["owner_redo_trusted"] = True
        else:
            report["quality_score"] = round(fight_score, 4)
            report["quality_threshold"] = fight_min
            return _finish(False, f"fight_low={fight_score:.3f}:min{fight_min:.2f}")
    if payoff_score < payoff_min:
        # Singles with clear gunfire: ship for owner 👍/👎 instead of drought on OCR miss.
        # Never bypass a near-zero payoff with no kill signal — that shipped loot runs
        # (_-HbZ0zNDOs_2538: payoff=0.0, no killfeed/notification).
        has_payoff_signal = bool(notification_hit or keyword_hit or float(killfeed) >= 0.25)
        if (
            single
            and _singles_gun_bypass_enabled("PUBG_SINGLES_GUN_PAYOFF_BYPASS")
            and gun >= float(os.environ.get("PUBG_SINGLE_MIN_GUN_DENSITY", "0.045"))
            and burst >= float(os.environ.get("PUBG_CLIP_MIN_BURST_RATIO", "4.8"))
            and not loot_walk
            and has_payoff_signal
            and payoff_score >= float(os.environ.get("PUBG_SINGLES_PAYOFF_BYPASS_FLOOR", "0.05"))
        ):
            report["singles_gun_payoff_bypass"] = True
        elif _owner_redo_trusted(video_path, start_sec, duration_sec):
            report["owner_redo_trusted"] = True
        else:
            report["quality_score"] = round(payoff_score, 4)
            report["quality_threshold"] = payoff_min
            return _finish(False, f"payoff_low={payoff_score:.3f}:min{payoff_min:.2f}")

    components = {
        "panns": _clip(panns_gun / 0.45) * 0.20,
        "gun": _clip(gun / 0.080) * 0.16,
        "burst": _clip(burst / 8.0) * 0.08,
        "motion": _clip(motion / 0.060) * 0.10,
        "killfeed": _clip(effective_killfeed) * 0.14,
        "author_kill": (0.18 if has_kill else 0.0),
        "visual": (0.09 if visual_ok else 0.0),
        "audio_presence": _clip(rms / 0.050) * 0.05,
    }
    penalties = {
        "loot_walk": 0.16 if loot_walk else 0.0,
        "no_author_kill": 0.12 if not has_kill else 0.0,
        "visual_fail": 0.08 if not visual_ok else 0.0,
        "missing_kill_notification": (
            0.14 if notification_mode == "prefer" and not notification_hit else 0.0
        ),
        "speech_music": _clip(
            max(float(panns.get("panns_speech", 0.0)), float(panns.get("panns_music", 0.0)))
            - panns_gun
        )
        * 0.08,
    }
    heuristic = _clip(sum(components.values()) - sum(penalties.values()))

    ranker_score = None
    try:
        from pubg_moment_ranker import predict_from_features

        ranker_score = predict_from_features(
            {
                **{key: float(value) for key, value in panns.items()},
                **{key: float(shoot.get(key, 0.0)) for key in (
                    "gunfire_density",
                    "burst_ratio",
                    "audio_rms",
                    "center_motion",
                )},
                "killfeed_density": float(killfeed),
            }
        )
    except Exception:
        ranker_score = None
    blend = float(os.environ.get("PUBG_QUALITY_RANKER_WEIGHT", "0.70"))
    quality = heuristic
    if ranker_score is not None:
        quality = _clip(heuristic * (1.0 - blend) + float(ranker_score) * blend)

    threshold = float(os.environ.get("PUBG_QUALITY_SCORE_MIN", "0.48"))
    if single:
        threshold = float(os.environ.get("PUBG_QUALITY_SCORE_MIN_SINGLES", "0.32"))
    report.update(
        {
            "components": {key: round(value, 4) for key, value in components.items()},
            "penalties": {key: round(value, 4) for key, value in penalties.items()},
            "heuristic_score": round(heuristic, 4),
            "ranker_score": None if ranker_score is None else round(float(ranker_score), 4),
            "quality_score": round(quality, 4),
            "quality_threshold": threshold,
            "has_author_kill": has_kill,
        }
    )
    if quality < threshold:
        if _owner_redo_trusted(video_path, start_sec, duration_sec):
            report["owner_redo_trusted"] = True
            report["quality_score"] = round(max(quality, threshold), 4)
            return _finish(True, f"owner_redo_trusted={quality:.3f}")
        if (
            single
            and _singles_gun_bypass_enabled("PUBG_SINGLES_GUN_QUALITY_BYPASS")
            and gun >= float(os.environ.get("PUBG_SINGLE_MIN_GUN_DENSITY", "0.045"))
            and burst >= float(os.environ.get("PUBG_CLIP_MIN_BURST_RATIO", "4.8"))
            and not loot_walk
            and fight_score >= fight_min
        ):
            report["singles_gun_quality_bypass"] = True
            return _finish(True, f"quality_singles_gun={quality:.3f}:fight{fight_score:.3f}")
        return _finish(False, f"quality_low={quality:.3f}:min{threshold:.2f}")
    return _finish(True, f"quality_ok={quality:.3f}")


__all__ = ["score_pubg_window"]
