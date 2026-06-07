#!/usr/bin/env python3
"""Strict peak montages for 5 games — no rescue tiers, pre-send metrics table."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from montage_env import strict_peak_env
from segment_preview import PROOF_ROOT, build_proof_package, preview_id_for, send_proof_to_owner
from strict_segment_gate import (
    GAME_LABELS,
    normalize_profile,
    passes_strict_gate,
    verify_montage_segments,
)
from visual_action_check import verify_segments_visual

MIN_CLIPS = 3
TARGET_CLIPS = 4
MIN_FINAL_DURATION = 33.0
MAX_FINAL_DURATION = 57.0
MIN_GAP_SEC = 90.0
GLOBAL_HISTORY = Path("/root/data/mlbb/strict_global_segment_history.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("strict_montage")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def segment_key(sig: str, start: float) -> str:
    return f"{sig}:{round(start, 3)}"


def apply_strict_env(profile: str, env: dict[str, str]) -> dict[str, str]:
    merged = dict(strict_peak_env(profile))
    merged.update(env)
    for key, val in merged.items():
        os.environ[key] = val
    return merged


def load_used_keys(profile: str) -> set[str]:
    used: set[str] = set()
    if GLOBAL_HISTORY.exists():
        try:
            payload = json.loads(GLOBAL_HISTORY.read_text(encoding="utf-8"))
            used |= {str(k) for k in payload.get("segment_keys", [])}
        except (json.JSONDecodeError, OSError):
            pass
    slug = normalize_profile(profile)
    for pattern in (f"strict_{slug}_*.json", f"showcase_{slug}_*.json", f"{slug}_*.json"):
        for path in Path("/root/videos").glob(pattern):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("mode") not in (None, "strict_peak", "brawl_direct"):
                continue
            for seg in data.get("selected_segments", []):
                sig = seg.get("source_signature", "")
                if sig:
                    used.add(segment_key(sig, float(seg.get("start", 0))))
    return used


def save_used_keys(keys: set[str]) -> None:
    GLOBAL_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    GLOBAL_HISTORY.write_text(
        json.dumps(
            {"segment_keys": sorted(keys), "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _fast_peak_starts(analysis: dict, profile: str, *, limit: int = 45) -> list[float]:
    """Top motion/audio/gunfire bins — avoids slow build_candidates on 3h VODs."""
    import numpy as np

    win = float(analysis.get("window_seconds", 2.0))
    gun = np.asarray(analysis.get("gunfire", analysis["audio"]), dtype=np.float32)
    motion = np.asarray(analysis["center_motion"], dtype=np.float32)
    audio = np.asarray(analysis["audio"], dtype=np.float32)
    if profile in ("pubg", "standoff", "wot"):
        combined = gun * 0.62 + motion * 0.22 + audio * 0.16
    elif profile == "genshin":
        scene = np.asarray(analysis["scene"], dtype=np.float32)
        combined = motion * 0.35 + audio * 0.30 + scene * 0.35
    else:
        combined = motion * 0.40 + audio * 0.35 + gun * 0.25

    skip_intro = 120.0 if profile != "pubg" else 90.0
    min_gap = 75.0
    order = np.argsort(combined)[::-1]
    starts: list[float] = []
    for idx in order:
        start = float(idx) * win
        if start < skip_intro:
            continue
        if any(abs(start - s) < min_gap for s in starts):
            continue
        starts.append(start)
        if len(starts) >= limit:
            break
    return starts


def discover_strict_candidates(
    vod: Path,
    profile: str,
    sig: str,
    used: set[str],
) -> list[dict]:
    profile = normalize_profile(profile)
    use_highlight = os.environ.get("HIGHLIGHT_SCORER", "1") == "1"
    if use_highlight and profile in ("pubg", "standoff", "mobile_legends", "genshin", "wot"):
        from highlight_scorer import discover_highlight_candidates

        probe_limit = int(os.environ.get("STRICT_PROBE_LIMIT", "40"))
        verified = discover_highlight_candidates(
            vod,
            profile,
            used_keys=used,
            segment_key_fn=segment_key,
            sig=sig,
            limit=probe_limit,
        )
        for cand in verified:
            cand["source_signature"] = sig
            cand["source_index"] = 0
        return verified

    from smart_video_editor import profile_action_clip_bounds, analyze_video

    log.info("legacy analyze %s profile=%s", vod.name, profile)
    analysis = analyze_video(vod)
    clip_lo, clip_hi = profile_action_clip_bounds(
        "world_of_tanks" if profile == "wot" else profile
    )
    default_dur = min(10.0, clip_hi)
    probe_limit = int(os.environ.get("STRICT_PROBE_LIMIT", "45"))
    peak_starts = _fast_peak_starts(analysis, profile, limit=probe_limit * 2)
    verified: list[dict] = []
    for start in peak_starts:
        dur = default_dur
        key = segment_key(sig, start)
        if key in used:
            continue
        ok, reason, metrics = passes_strict_gate(vod, start, dur, profile)
        if not ok:
            continue
        score = float(metrics.get("gunfire_density", 0) or metrics.get("impact_density", 0) or 0)
        verified.append(
            {
                "source_index": 0,
                "source_signature": sig,
                "source_path": str(vod),
                "game_name": GAME_LABELS.get(profile, profile),
                "start": round(start, 3),
                "input_duration": dur,
                "output_duration": dur,
                "speed": 1.0,
                "score": score,
                "strict_metrics": metrics,
                "gate_reason": reason,
                "strict_score": score,
            }
        )
        if len(verified) >= probe_limit:
            break
    verified.sort(key=lambda c: c.get("strict_score", 0), reverse=True)
    return verified


def pick_segments(candidates: list[dict], used: set[str], sig: str) -> list[dict]:
    chosen: list[dict] = []
    for cand in candidates:
        start = float(cand["start"])
        key = segment_key(sig, start)
        if key in used:
            continue
        if any(abs(start - float(c["start"])) < MIN_GAP_SEC for c in chosen):
            continue
        chosen.append(cand)
        if len(chosen) >= TARGET_CLIPS:
            break

    est = sum(float(c.get("output_duration") or c.get("input_duration") or 9) for c in chosen)
    if len(chosen) < MIN_CLIPS:
        return []
    if est < MIN_FINAL_DURATION:
        # Highlight windows are 10s; xfade eats ~0.28s per join — pad target sum.
        xfade = float(os.environ.get("TRANSITION_DURATION", "0.28"))
        target = MIN_FINAL_DURATION + xfade * max(0, len(chosen) - 1)
        per = target / len(chosen)
        for cand in chosen:
            cur = float(cand.get("output_duration") or cand.get("input_duration") or 9)
            if cur < per:
                cand["input_duration"] = round(per, 3)
                cand["output_duration"] = round(per, 3)
        est = sum(float(c.get("output_duration") or c.get("input_duration") or 9) for c in chosen)
        if est < MIN_FINAL_DURATION:
            return []
    if est > MAX_FINAL_DURATION and len(chosen) > MIN_CLIPS:
        while len(chosen) > MIN_CLIPS and est > MAX_FINAL_DURATION:
            chosen.pop()
            est = sum(float(c.get("output_duration") or c.get("input_duration") or 9) for c in chosen)
    return chosen


def build_and_send(
    vod: Path,
    profile: str,
    clips: list[dict],
    *,
    output_dir: Path,
    basename: str,
    caption: str,
    chat_id: str,
    bot_token: str,
    sig: str,
) -> Path | None:
    from smart_video_editor import (
        build_xfade_command,
        ffprobe_duration,
        render_segment,
        run_command,
        short_file_id,
    )

    segment_pairs = [
        (
            float(c["start"]),
            float(c.get("input_duration") or c.get("output_duration") or 9.0),
        )
        for c in clips
    ]
    has_highlight = all(
        "panns_gunshot" in (clips[i].get("highlight_metrics") or clips[i].get("strict_metrics") or {})
        or "clip_score" in (clips[i].get("highlight_metrics") or clips[i].get("strict_metrics") or {})
        for i in range(len(clips))
    )
    if has_highlight:
        metrics_rows = [c.get("highlight_metrics") or c.get("strict_metrics", {}) for c in clips]
        for row in metrics_rows:
            row["pass"] = bool(row.get("rule_pass"))
        all_ok = all(row.get("pass") for row in metrics_rows)
        from strict_segment_gate import format_acceptance_table

        game = GAME_LABELS.get(normalize_profile(profile), profile)
        table = format_acceptance_table(
            game,
            [{**row, "gate_reason": row.get("pass_reason", "")} for row in metrics_rows],
        )
        log.info("\n%s", table.replace("gun=", "panns=").replace("burst=", "clip="))
        if not all_ok:
            log.error("REFUSED: highlight gate failed")
            return None
    else:
        all_ok, metrics_rows, table = verify_montage_segments(vod, profile, segment_pairs)
        log.info("\n%s", table)
        if not all_ok:
            log.error("REFUSED: legacy audio/UI gate failed")
            return None

    # Highlight scorer already fused PANNs+CLIP; legacy visual check only if no highlight metrics
    if has_highlight:
        visual_rows = []
        for c in clips:
            row = dict(c.get("highlight_metrics") or c.get("strict_metrics") or {})
            row["visual_pass"] = bool(row.get("rule_pass"))
            visual_rows.append(row)
        vis_passed = sum(1 for r in visual_rows if r.get("rule_pass"))
        vis_total = len(visual_rows)
        if vis_passed < vis_total:
            log.error(
                "REFUSED: game=%s reason=highlight_rule_fail visual_passed=%s/%s",
                GAME_LABELS.get(normalize_profile(profile), profile),
                vis_passed,
                vis_total,
            )
            return None
    else:
        from visual_action_check import verify_segments_visual

        vis_passed, vis_total, visual_rows, vis_reason = verify_segments_visual(
            vod, profile, segment_pairs, segment_metrics=metrics_rows
        )
        if vis_passed < vis_total:
            log.error(
                "REFUSED: game=%s reason=%s visual_passed=%s/%s",
                GAME_LABELS.get(normalize_profile(profile), profile),
                vis_reason,
                vis_passed,
                vis_total,
            )
            return None

    logo = Path(os.environ.get("LOGO_FILE", "/root/logo.png"))
    temp_dir = Path(tempfile.mkdtemp(prefix="strict-peak-"))
    try:
        for cand in clips:
            cand.setdefault("source_signature", sig)
            cand["source_path"] = str(vod)
            cand["source_index"] = 0

        segment_paths: list[Path] = []
        durations: list[float] = []
        for idx, cand in enumerate(clips):
            seg_path = temp_dir / f"seg_{idx:02d}.mp4"
            durations.append(render_segment(cand, seg_path, logo))
            segment_paths.append(seg_path)

        out = output_dir / f"{basename}_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
        run_command(build_xfade_command(segment_paths, durations, out))
        final_dur = ffprobe_duration(out)

        if final_dur < MIN_FINAL_DURATION or final_dur > MAX_FINAL_DURATION:
            log.error("abort send: duration %.1fs outside %.0f-%.0fs", final_dur, MIN_FINAL_DURATION, MAX_FINAL_DURATION)
            return None

        game = GAME_LABELS.get(normalize_profile(profile), profile)
        pid = preview_id_for(profile, basename)
        env_snapshot = {
            "TG_BOT_TOKEN": bot_token,
            "TG_CHAT_ID": os.environ.get("TG_CHAT_ID", chat_id),
        }
        pkg = build_proof_package(
            video_path=vod,
            profile=profile,
            game_label=game,
            segments=clips,
            visual_rows=visual_rows,
            audio_metrics=metrics_rows,
            montage_path=out,
            preview_id=pid,
        )
        meta = {
            "profile": profile,
            "mode": "visual_preview",
            "game": game,
            "final_duration": final_dur,
            "output_id": short_file_id(out),
            "acceptance_table": table,
            "segment_metrics": metrics_rows,
            "visual_proof": visual_rows,
            "preview_id": pid,
            "preview_status": "PENDING_OWNER",
            "selected_segments": clips,
        }
        out.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        (PROOF_ROOT / pid / "montage.json").write_text(
            json.dumps({"montage": str(out), "caption": caption, "profile": profile}, indent=2),
            encoding="utf-8",
        )

        if bot_token and env_snapshot.get("TG_CHAT_ID"):
            send_proof_to_owner(pkg, env_snapshot)
            log.info(
                "REFUSED sendVideo: game=%s reason=awaiting_owner_preview visual_passed=%s/%s preview_id=%s",
                game,
                vis_passed,
                vis_total,
                pid,
            )
        return out
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def make_strict_montage(
    *,
    profile: str,
    vod: Path,
    output_basename: str,
    caption: str,
    env: dict[str, str],
) -> tuple[int, str]:
    """Returns (0, detail) on success, (1, refuse reason), (2, missing input)."""
    if not vod.exists():
        return 2, f"vod missing: {vod}"

    profile = normalize_profile(profile)
    if profile == "pubg":
        from pubg_brawl_direct import make_brawl_montage

        merged = apply_strict_env(profile, env)
        return make_brawl_montage(output_basename=output_basename, caption=caption, env=merged, vod=vod)

    merged = apply_strict_env(profile, env)
    sig = file_sha256(vod)
    used = load_used_keys(profile)
    pool = discover_strict_candidates(vod, profile, sig, used)

    if len(pool) < MIN_CLIPS:
        game = GAME_LABELS.get(profile, profile)
        return 1, f"REFUSED: game={game}, reason=insufficient_audio_candidates, visual_passed=0/{MIN_CLIPS}"

    clips = pick_segments(pool, used, sig)
    if len(clips) < MIN_CLIPS:
        game = GAME_LABELS.get(profile, profile)
        return 1, f"REFUSED: game={game}, reason=insufficient_segments, visual_passed=0/{MIN_CLIPS}"

    out_dir = Path(merged.get("OUTPUT_DIR", "/root/videos"))
    out_dir.mkdir(parents=True, exist_ok=True)
    chat_id = merged.get("TG_CHAT_ID", "")
    bot_token = merged.get("TG_BOT_TOKEN", "")

    result = build_and_send(
        vod,
        profile,
        clips,
        output_dir=out_dir,
        basename=output_basename,
        caption=caption,
        chat_id=chat_id,
        bot_token=bot_token,
        sig=sig,
    )
    if result is None:
        game = GAME_LABELS.get(profile, profile)
        return 1, f"REFUSED: game={game}, reason=render_or_visual_gate_failed, visual_passed=0/{len(clips)}"

    for clip in clips:
        used.add(segment_key(sig, float(clip["start"])))
    save_used_keys(used)
    game = GAME_LABELS.get(profile, profile)
    meta_path = result.with_suffix(".json")
    preview_id = ""
    try:
        preview_id = json.loads(meta_path.read_text(encoding="utf-8")).get("preview_id", "")
    except (json.JSONDecodeError, OSError):
        pass
    return 3, (
        f"REFUSED: game={game}, reason=awaiting_owner_preview, "
        f"visual_passed={len(clips)}/{len(clips)}, preview_id={preview_id}, montage={result.name}"
    )
