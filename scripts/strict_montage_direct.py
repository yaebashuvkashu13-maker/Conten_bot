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
from strict_segment_gate import (
    GAME_LABELS,
    normalize_profile,
    passes_strict_gate,
    verify_montage_segments,
)

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


def discover_strict_candidates(
    vod: Path,
    profile: str,
    sig: str,
    used: set[str],
) -> list[dict]:
    from smart_video_editor import analyze_video, build_candidates

    profile = normalize_profile(profile)
    gate_profile = "world_of_tanks" if profile == "wot" else profile
    log.info("analyze %s profile=%s", vod.name, profile)
    analysis = analyze_video(vod)
    log.info("analyze done %s bins=%s", vod.name, analysis.get("bins"))
    global_values = {
        "motion": list(analysis["motion"]),
        "center_motion": list(analysis["center_motion"]),
        "sharpness": list(analysis["sharpness"]),
        "scene": list(analysis["scene"]),
        "audio": list(analysis["audio"]),
        "gunfire": list(analysis.get("gunfire", analysis["audio"])),
    }
    raw = build_candidates(
        0,
        vod,
        GAME_LABELS.get(profile, profile),
        analysis,
        global_values,
        gate_profile,
        sig or file_sha256(vod),
        relax_segment_gate=False,
    )

    probe_limit = int(os.environ.get("STRICT_PROBE_LIMIT", "50"))
    verified: list[dict] = []
    for cand in raw[:probe_limit]:
        start = float(cand["start"])
        dur = float(cand.get("input_duration") or cand.get("output_duration") or 9.0)
        key = segment_key(sig, start)
        if key in used:
            continue
        ok, reason, metrics = passes_strict_gate(vod, start, dur, profile)
        log.info(
            "[%s] probe start=%.1f %s reason=%s",
            "PASS" if ok else "FAIL",
            start,
            metrics,
            reason,
        )
        if not ok:
            continue
        cand["strict_metrics"] = metrics
        cand["gate_reason"] = reason
        cand["strict_score"] = float(cand.get("score", 0)) + float(metrics.get("gunfire_density", 0) or metrics.get("impact_density", 0) or 0)
        verified.append(cand)

    verified.sort(key=lambda c: c.get("strict_score", c.get("score", 0)), reverse=True)
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
        send_telegram_video,
        short_file_id,
    )

    segment_pairs = [
        (
            float(c["start"]),
            float(c.get("input_duration") or c.get("output_duration") or 9.0),
        )
        for c in clips
    ]
    all_ok, metrics_rows, table = verify_montage_segments(vod, profile, segment_pairs)
    log.info("\n%s", table)
    if not all_ok:
        log.error("abort send: not all segments passed strict gate")
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
        meta = {
            "profile": profile,
            "mode": "strict_peak",
            "game": game,
            "final_duration": final_dur,
            "output_id": short_file_id(out),
            "acceptance_table": table,
            "segment_metrics": metrics_rows,
            "selected_segments": clips,
            "strict_gate_summary": f"Game={game}, segments={len(clips)}, all passed strict gate",
        }
        out.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        if bot_token and chat_id:
            send_telegram_video(bot_token, chat_id, out, caption)
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
        return 1, f"Game={GAME_LABELS.get(profile, profile)}, found {len(pool)}/{MIN_CLIPS} strict segments"

    clips = pick_segments(pool, used, sig)
    if len(clips) < MIN_CLIPS:
        return 1, f"Game={GAME_LABELS.get(profile, profile)}, found {len(clips)}/{MIN_CLIPS} non-overlapping strict segments"

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
        return 1, f"Game={GAME_LABELS.get(profile, profile)}, render/gate failed"

    for clip in clips:
        used.add(segment_key(sig, float(clip["start"])))
    save_used_keys(used)
    game = GAME_LABELS.get(profile, profile)
    return 0, f"Game={game}, segments={len(clips)}, all passed strict gate, 0 run/loot/talk/idle -> {result.name}"
