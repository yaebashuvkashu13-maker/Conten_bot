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


def _ocr_zones(frame) -> str:
    from pubg_combat_gate import _ocr_zone_text

    zones = (
        (0.02, 0.22, 0.62, 0.98),  # killfeed top-right
        (0.28, 0.62, 0.18, 0.82),  # center announce
        (0.02, 0.18, 0.18, 0.82),  # top banner
    )
    parts = [_ocr_zone_text(frame, y0=a, y1=b, x0=c, x1=d) for a, b, c, d in zones]
    return " ".join(parts)


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
        classified = classify_kill_text(_ocr_zones(frame))
        if classified:
            hits.append(
                KillMomentHit(
                    sec=round(ts, 2),
                    tier=classified.tier,
                    label=classified.label,
                    text=classified.text,
                    source="ocr",
                )
            )
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


def discover_vod_kill_moments(
    vod: Path,
    *,
    min_tier: int | None = None,
    hint_peaks: list[float] | None = None,
) -> list[KillMomentHit]:
    """Sparse OCR around motion peaks — same strategy as MLBB banner discover."""
    if os.environ.get("PUBG_VOD_KILL_DISCOVER", "1") != "1":
        return []
    from mlbb_fight_segment import _analysis_for

    analysis = _analysis_for(vod)
    duration = float(analysis.get("duration") or 0.0)
    if duration < 20.0:
        return []
    need = min_tier if min_tier is not None else _min_tier()
    max_probes = max(2, int(os.environ.get("PUBG_KILL_DISCOVER_MAX_PROBES", "6")))
    max_sec = max(15.0, float(os.environ.get("PUBG_KILL_DISCOVER_MAX_SEC", "35")))
    deadline = time.monotonic() + max_sec
    hits: list[KillMomentHit] = []
    probes = 0

    def _merge(hit: KillMomentHit) -> None:
        if hit.tier < need:
            return
        if hits and abs(hit.sec - hits[-1].sec) < 5.0:
            if hit.tier > hits[-1].tier:
                hits[-1] = hit
        else:
            hits.append(hit)

    peak_limit = max(2, int(os.environ.get("PUBG_KILL_DISCOVER_PEAK_HINTS", "4")))
    for peak in sorted(set(hint_peaks or []))[:peak_limit]:
        if probes >= max_probes or time.monotonic() >= deadline:
            break
        probes += 1
        hit = find_kill_near_peak(vod, peak, quick=True)
        if hit:
            _merge(hit)

    hits.sort(key=lambda h: h.sec)
    log.info("pubg kill discover %s: probes=%s hits=%s tier>=%s", vod.name, probes, len(hits), need)
    return hits
