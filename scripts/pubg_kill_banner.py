#!/usr/bin/env python3
"""PUBG kill-moment detection — killfeed OCR + center-screen eliminate/knock text."""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("pubg_kill_banner")

# tier: 1=single kill signal, 2=double/multi-kill or strong killfeed
_KILL_PATTERNS: list[tuple[re.Pattern[str], int, str]] = [
    (re.compile(r"double\s*kill|двойн.{0,12}убий|2\s*x\s*kill", re.I), 2, "double"),
    (re.compile(r"triple\s*kill|тройн.{0,12}убий", re.I), 2, "triple"),
    (re.compile(r"head\s*shot|хедшот|хэдшот", re.I), 2, "headshot"),
    (re.compile(r"eliminated|элиминир|устранил|убил", re.I), 1, "eliminated"),
    (re.compile(r"knocked\s*out|knock\s*out|нокаут|нокнул", re.I), 1, "knock"),
    (re.compile(r"\bkill\b|убийств", re.I), 1, "kill"),
]

_FEED_KEYWORDS = (
    "eliminated",
    "knock",
    "headshot",
    "kill",
    "убил",
    "убийство",
    "нок",
    "элимин",
)


@dataclass(frozen=True)
class KillMomentHit:
    sec: float
    tier: int
    label: str
    text: str
    source: str = "ocr"


def _min_tier() -> int:
    raw = (os.environ.get("PUBG_KILL_MIN_TIER") or "single").strip().lower()
    if raw.isdigit():
        return max(1, int(raw))
    return {"single": 1, "double": 2}.get(raw, 1)


def dense_scan_enabled() -> bool:
    """MLBB-style ~1 frame/sec dense killfeed discover."""
    return os.environ.get("PUBG_VOD_KILL_DENSE_SEC", "0") == "1"


def classify_kill_text(text: str) -> KillMomentHit | None:
    blob = " ".join(str(text or "").split())
    if not blob:
        return None
    best_tier = 0
    best_label = ""
    for pat, tier, label in _KILL_PATTERNS:
        if pat.search(blob) and tier > best_tier:
            best_tier = tier
            best_label = label
    feed_hits = sum(1 for kw in _FEED_KEYWORDS if kw.lower() in blob.lower())
    if feed_hits >= 2 and best_tier < 2:
        best_tier = 2
        best_label = "multi_feed"
    if best_tier <= 0:
        return None
    return KillMomentHit(sec=0.0, tier=best_tier, label=best_label, text=blob[:160])


def _ocr_zones(frame, *, dense: bool = False) -> str:
    from pubg_combat_gate import _ocr_zone_text

    # Dense 1Hz: killfeed-only OCR (MLBB dense stays shallow for the same reason).
    if dense:
        zones = ((0.02, 0.22, 0.62, 0.98),)
    else:
        zones = (
            (0.02, 0.22, 0.62, 0.98),  # killfeed top-right
            (0.28, 0.62, 0.18, 0.82),  # center announce
            (0.02, 0.18, 0.18, 0.82),  # top banner
        )
    parts = [_ocr_zone_text(frame, y0=a, y1=b, x0=c, x1=d) for a, b, c, d in zones]
    return " ".join(parts)


def _classify_frame(sec: float, frame, *, dense: bool = False) -> KillMomentHit | None:
    classified = classify_kill_text(_ocr_zones(frame, dense=dense))
    if classified is None:
        return None
    return KillMomentHit(
        sec=round(sec, 2),
        tier=classified.tier,
        label=classified.label,
        text=classified.text,
        source="ocr",
    )


def scan_window(
    video_path: Path,
    start_sec: float,
    end_sec: float,
    *,
    focus_sec: float | None = None,
) -> list[KillMomentHit]:
    from gameplay_gate import _read_frame_at

    hits: list[KillMomentHit] = []
    step = float(os.environ.get("PUBG_KILL_SCAN_STEP", "0.4"))
    t = max(0.0, start_sec)
    end = max(t + 0.5, end_sec)
    focus = focus_sec if focus_sec is not None else (start_sec + end_sec) * 0.5
    times = sorted({t, focus, end - step})
    cur = t
    while cur <= end:
        times.append(cur)
        cur += step
    for ts in sorted(set(times)):
        frame = _read_frame_at(video_path, ts)
        if frame is None:
            continue
        hit = _classify_frame(ts, frame)
        if hit:
            hits.append(hit)
    return hits


def find_kill_near_peak(video_path: Path, peak_sec: float, *, quick: bool = True) -> KillMomentHit | None:
    before = float(os.environ.get("PUBG_KILL_SCAN_BEFORE", "12"))
    after = float(os.environ.get("PUBG_KILL_SCAN_AFTER", "6"))
    if quick:
        before = min(before, 10.0)
        after = min(after, 5.0)
    need = _min_tier()
    best: KillMomentHit | None = None
    for hit in scan_window(video_path, peak_sec - before, peak_sec + after, focus_sec=peak_sec):
        if hit.tier < need:
            continue
        if best is None or hit.tier > best.tier or abs(hit.sec - peak_sec) < abs(best.sec - peak_sec):
            best = hit
    return best


def _dense_scan_bounds(duration: float) -> tuple[float, float]:
    """Start/end for dense 1Hz sweep — capped so long VODs do not hang."""
    t0 = float(os.environ.get("PUBG_KILL_DENSE_START_SEC", "30"))
    if duration <= 240:
        t0 = min(t0, 15.0)
    elif duration <= 480:
        t0 = min(t0, 20.0)
    end = max(t0 + 8.0, duration - 2.0)
    cap = float(os.environ.get("PUBG_KILL_DENSE_MAX_SPAN_SEC", "480"))
    return t0, min(end, t0 + max(60.0, cap))


def discover_vod_kill_moments(
    vod: Path,
    *,
    min_tier: int | None = None,
    hint_peaks: list[float] | None = None,
) -> list[KillMomentHit]:
    """
    Peak-local OCR, plus optional MLBB-style dense ~1 frame/sec timeline scan
    (PUBG_VOD_KILL_DENSE_SEC=1). Dense path is chunked and wall-clock capped.
    """
    if os.environ.get("PUBG_VOD_KILL_DISCOVER", "1") != "1":
        return []
    from mlbb_fight_segment import _analysis_for

    analysis = _analysis_for(vod)
    duration = float(analysis.get("duration") or 0.0)
    if duration < 20.0:
        return []
    need = min_tier if min_tier is not None else _min_tier()
    dense = dense_scan_enabled()
    if dense:
        step = min(1.0, float(os.environ.get("PUBG_KILL_DISCOVER_STEP", "1.0")))
        t0, t_end = _dense_scan_bounds(duration)
        scan_span = max(60.0, t_end - t0)
        per_sec = int(scan_span / max(step, 1.0)) + 16
        max_probes = max(
            int(os.environ.get("PUBG_KILL_DISCOVER_MAX_PROBES", "120")),
            min(per_sec, int(os.environ.get("PUBG_KILL_DENSE_PROBE_CAP", "480"))),
        )
        max_sec = max(
            60.0,
            float(os.environ.get("PUBG_KILL_DISCOVER_MAX_SEC", "180")),
        )
    else:
        step = float(os.environ.get("PUBG_KILL_DISCOVER_STEP", "3.0"))
        t0, t_end = 0.0, duration
        max_probes = max(2, int(os.environ.get("PUBG_KILL_DISCOVER_MAX_PROBES", "6")))
        max_sec = max(15.0, float(os.environ.get("PUBG_KILL_DISCOVER_MAX_SEC", "35")))

    deadline = time.monotonic() + max_sec
    hits: list[KillMomentHit] = []
    probes = 0
    log.info(
        "pubg kill discover %s: start dense=%s duration=%.0fs max_probes=%s max_sec=%.0f",
        vod.name,
        int(dense),
        duration,
        max_probes,
        max_sec,
    )

    def _merge(hit: KillMomentHit) -> None:
        if hit.tier < need:
            return
        if hits and abs(hit.sec - hits[-1].sec) < 5.0:
            if hit.tier > hits[-1].tier:
                hits[-1] = hit
        else:
            hits.append(hit)

    # Phase 1: peak hints — in dense mode keep this cheap (1 frame/peak);
    # the 1Hz timeline pass below is the real recall path (MLBB-style).
    peak_limit = max(2, int(os.environ.get("PUBG_KILL_DISCOVER_PEAK_HINTS", "4")))
    if dense:
        peak_limit = max(0, int(os.environ.get("PUBG_KILL_DENSE_PEAK_HINTS", "4")))
    for peak in sorted(set(hint_peaks or []))[:peak_limit]:
        if probes >= max_probes or time.monotonic() >= deadline:
            break
        probes += 1
        if dense:
            from gameplay_gate import _read_frame_at

            frame = _read_frame_at(vod, float(peak))
            hit = _classify_frame(float(peak), frame) if frame is not None else None
        else:
            hit = find_kill_near_peak(vod, peak, quick=True)
        if hit:
            _merge(hit)

    # Phase 2: dense ~1 Hz chunked ffmpeg decode + OCR (same idea as MLBB banner dense).
    if dense and probes < max_probes and time.monotonic() < deadline:
        from mlbb_kill_banner import _ffmpeg_sample_frames

        chunk_sec = float(os.environ.get("PUBG_KILL_DENSE_CHUNK_SEC", "60"))
        stop_on_hit = max(1, int(os.environ.get("PUBG_KILL_DENSE_STOP_ON_HITS", "3")))
        cursor = t0
        frame_i = 0
        log.info(
            "pubg kill discover %s: dense_1hz start=%.0fs end=%.0fs span=%.0fs "
            "max_probes=%s max_sec=%.0f",
            vod.name,
            t0,
            t_end,
            t_end - t0,
            max_probes,
            max_sec,
        )
        while cursor < t_end - 0.25:
            if probes >= max_probes or time.monotonic() >= deadline:
                break
            if len(hits) >= stop_on_hit:
                log.info(
                    "pubg kill discover %s: dense stop early — hits=%s",
                    vod.name,
                    len(hits),
                )
                break
            chunk_end = min(t_end, cursor + chunk_sec)
            span = max(0.5, chunk_end - cursor)
            sample_count = max(1, min(120, int(span / max(step, 0.25)) + 1))
            batch = _ffmpeg_sample_frames(vod, cursor, chunk_end, sample_count)
            if not batch:
                cursor = chunk_end
                continue
            for t, frame in batch:
                if probes >= max_probes or time.monotonic() >= deadline:
                    break
                if len(hits) >= stop_on_hit:
                    break
                probes += 1
                hit = _classify_frame(float(t), frame, dense=True)
                if hit is not None:
                    _merge(hit)
                if frame_i % 60 == 0 or frame_i < 3:
                    log.info(
                        "pubg kill discover %s: dense t=%.0fs probes=%s/%s hits=%s",
                        vod.name,
                        t,
                        probes,
                        max_probes,
                        len(hits),
                    )
                frame_i += 1
                try:
                    from vod_pipeline_heartbeat import heartbeat

                    heartbeat(
                        "pubg_kill_dense_scan",
                        vod_id=vod.stem,
                        progress=(t - t0) / max(1.0, t_end - t0),
                        candidates_out=len(hits),
                    )
                except Exception:
                    pass
            cursor = chunk_end

    hits.sort(key=lambda h: h.sec)
    log.info(
        "pubg kill discover %s: probes=%s hits=%s tier>=%s dense=%s",
        vod.name,
        probes,
        len(hits),
        need,
        int(dense),
    )
    return hits
