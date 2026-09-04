#!/usr/bin/env python3
"""Event-level deduplication for montage peak pools."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


def merge_nearby_peaks(
    peaks: Iterable[float],
    *,
    merge_gap_sec: float | None = None,
    scores: dict[float, float] | None = None,
) -> list[float]:
    """Merge peaks within merge_gap_sec — keep highest score (or earliest)."""
    gap = float(
        merge_gap_sec
        if merge_gap_sec is not None
        else os.environ.get("VOD_EVENT_MERGE_GAP_SEC", "25")
    )
    ordered = sorted((float(p) for p in peaks), key=lambda p: -(scores or {}).get(p, 0.0))
    kept: list[float] = []
    for peak in ordered:
        if any(abs(peak - old) < gap for old in kept):
            continue
        kept.append(peak)
    return sorted(kept)


def _audio_signature(video_path: Path, peak_sec: float, window: float = 4.0) -> tuple[float, ...]:
    from highlight_scorer import score_panns_audio

    feats = score_panns_audio(video_path, max(0.0, peak_sec - window * 0.5), window)
    return tuple(round(float(feats.get(k, 0.0)), 4) for k in (
        "panns_gunshot",
        "panns_machine_gun",
        "panns_explosion",
        "panns_speech",
        "panns_music",
    ))


def _signature_distance(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    if not a or not b:
        return 1.0
    return sum((x - y) ** 2 for x, y in zip(a, b, strict=False)) ** 0.5


def dedup_by_audio_signature(
    video_path: Path,
    peaks: Iterable[float],
    *,
    max_distance: float | None = None,
) -> list[float]:
    """Drop peaks with near-identical PANNs audio fingerprints (replay / same fight)."""
    if os.environ.get("VOD_EVENT_AUDIO_DEDUP", "1") != "1":
        return list(peaks)
    limit = float(max_distance or os.environ.get("VOD_EVENT_AUDIO_DEDUP_MAX", "0.12"))
    ordered = sorted((float(p) for p in peaks))
    kept: list[float] = []
    sigs: list[tuple[float, ...]] = []
    for peak in ordered:
        sig = _audio_signature(video_path, peak)
        if any(_signature_distance(sig, old) < limit for old in sigs):
            continue
        kept.append(peak)
        sigs.append(sig)
    return kept


def _frame_phash(video_path: Path, peak_sec: float) -> int | None:
    """Cheap 64-bit perceptual hash from a single mid-fight frame (no CLIP)."""
    import subprocess
    import tempfile

    try:
        from PIL import Image
    except ImportError:
        return None
    with tempfile.TemporaryDirectory(prefix="vod_phash_") as tmp:
        out = Path(tmp) / "f.jpg"
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, float(peak_sec)):.3f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-vf",
            "scale=32:32",
            "-y",
            str(out),
        ]
        proc = subprocess.run(cmd, capture_output=True, check=False, timeout=20)
        if proc.returncode != 0 or not out.is_file():
            return None
        img = Image.open(out).convert("L").resize((8, 8))
        pixels = list(img.getdata())
        avg = sum(pixels) / max(1, len(pixels))
        bits = 0
        for i, px in enumerate(pixels):
            if px >= avg:
                bits |= 1 << i
        return bits


def _hamming64(a: int, b: int) -> int:
    return int((a ^ b).bit_count())


def dedup_by_frame_phash(
    video_path: Path,
    peaks: Iterable[float],
    *,
    max_distance: int | None = None,
) -> list[float]:
    """Drop peaks whose mid-frame pHash is nearly identical (reuploads / same angle)."""
    if os.environ.get("VOD_EVENT_PHASH_DEDUP", "1") != "1":
        return list(peaks)
    if not video_path.is_file():
        return list(peaks)
    limit = int(max_distance or os.environ.get("VOD_EVENT_PHASH_DEDUP_MAX", "6"))
    ordered = sorted((float(p) for p in peaks))
    kept: list[float] = []
    hashes: list[int] = []
    for peak in ordered:
        ph = _frame_phash(video_path, peak)
        if ph is not None and any(_hamming64(ph, old) <= limit for old in hashes):
            continue
        kept.append(peak)
        if ph is not None:
            hashes.append(ph)
    return kept


def filter_montage_peaks(
    peaks: Iterable[float],
    *,
    used: Iterable[float] | None = None,
    gap_sec: float = 55.0,
    scores: dict[float, float] | None = None,
    video_path: Path | None = None,
) -> list[float]:
    """Drop peaks too close to already-used montage parts."""
    used_list = [float(u) for u in (used or [])]
    merged = merge_nearby_peaks(peaks, scores=scores)
    if video_path is not None and video_path.is_file():
        merged = dedup_by_audio_signature(video_path, merged)
        merged = dedup_by_frame_phash(video_path, merged)
    out: list[float] = []
    for peak in merged:
        if any(abs(peak - u) < gap_sec for u in used_list):
            continue
        if any(abs(peak - o) < gap_sec * 0.85 for o in out):
            continue
        out.append(peak)
    return out


def peaks_too_similar(
    a: float,
    b: float,
    *,
    gap_sec: float = 25.0,
) -> bool:
    return abs(float(a) - float(b)) < float(gap_sec)


__all__ = [
    "dedup_by_audio_signature",
    "dedup_by_frame_phash",
    "filter_montage_peaks",
    "merge_nearby_peaks",
    "peaks_too_similar",
]
