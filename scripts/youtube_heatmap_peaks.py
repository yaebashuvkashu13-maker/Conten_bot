#!/usr/bin/env python3
"""YouTube Most Replayed / heatmap weak labels — crowd engagement peaks."""

from __future__ import annotations

import argparse
import json
import logging
import re
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger("youtube_heatmap")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
MIN_GAP_SEC = 60.0
TOP_N = 20


def video_id_from_path(path: Path) -> str | None:
    stem = path.stem
    if stem.startswith("yt_"):
        return stem[3:]
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", stem):
        return stem
    return None


def _fetch_watch_html(video_id: str) -> str:
    url = f"https://www.youtube.com/watch?v={video_id}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _extract_yt_initial_data(html: str) -> dict[str, Any] | None:
    m = re.search(r"var ytInitialData\s*=\s*(\{.+?\});\s*</script>", html, re.DOTALL)
    if not m:
        m = re.search(r"ytInitialData\s*=\s*(\{.+?\});\s*", html, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _walk_markers(obj: Any, out: list[dict]) -> None:
    if isinstance(obj, dict):
        if "heatMarkerRenderer" in obj:
            hmr = obj["heatMarkerRenderer"]
            for key in ("heatmap", "markers", "marker"):
                if key in hmr:
                    _parse_marker_list(hmr[key], out)
        if "timedMarkerDecorations" in obj:
            _parse_marker_list(obj["timedMarkerDecorations"], out)
        if "intensityScoreNormalized" in obj and "startMillis" in obj:
            out.append(
                {
                    "start_sec": float(obj["startMillis"]) / 1000.0,
                    "intensity": float(obj["intensityScoreNormalized"]),
                    "source": "heatmap",
                }
            )
        for v in obj.values():
            _walk_markers(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _walk_markers(item, out)


def _parse_marker_list(data: Any, out: list[dict]) -> None:
    if not isinstance(data, list):
        return
    for item in data:
        if not isinstance(item, dict):
            continue
        start_ms = item.get("startMillis") or item.get("startTimeMs")
        intensity = item.get("intensityScoreNormalized") or item.get("intensity")
        if start_ms is None:
            continue
        out.append(
            {
                "start_sec": float(start_ms) / 1000.0,
                "intensity": float(intensity or 0.5),
                "source": "heatmap",
            }
        )


def fetch_heatmap_peaks(video_id: str) -> list[dict[str, Any]]:
    """Return [{start_sec, intensity, source:'heatmap'}, ...] or [] if unavailable."""
    try:
        html = _fetch_watch_html(video_id)
    except Exception as exc:
        log.warning("heatmap fetch failed %s: %s", video_id, exc)
        return []

    data = _extract_yt_initial_data(html)
    if not data:
        log.warning("heatmap: no ytInitialData for %s", video_id)
        return []

    raw: list[dict] = []
    _walk_markers(data, raw)
    if not raw:
        log.info("heatmap: no markers for %s", video_id)
        return []

    raw.sort(key=lambda x: x["intensity"], reverse=True)
    chosen: list[dict] = []
    for row in raw:
        if any(abs(row["start_sec"] - c["start_sec"]) < MIN_GAP_SEC for c in chosen):
            continue
        chosen.append(row)
        if len(chosen) >= TOP_N:
            break
    chosen.sort(key=lambda x: x["start_sec"])
    return chosen


def heatmap_peak_starts(
    video_path: Path,
    *,
    window_sec: float = 10.0,
    top_n: int = TOP_N,
) -> list[float]:
    """Window start times aligned to heatmap peaks."""
    vid = video_id_from_path(video_path)
    if not vid:
        return []
    peaks = fetch_heatmap_peaks(vid)[:top_n]
    return [round(max(0.0, p["start_sec"] - window_sec * 0.5), 1) for p in peaks]


def load_heatmap_intensity_map(video_path: Path) -> dict[float, float]:
    vid = video_id_from_path(video_path)
    if not vid:
        return {}
    peaks = fetch_heatmap_peaks(vid)
    return {round(p["start_sec"], 1): float(p["intensity"]) for p in peaks}


def nearest_heatmap_intensity(start_sec: float, intensity_map: dict[float, float]) -> float:
    if not intensity_map:
        return 0.0
    best_key = min(intensity_map, key=lambda k: abs(k - start_sec))
    if abs(best_key - start_sec) > 120:
        return 0.0
    return intensity_map[best_key]


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--vod", required=True, help="yt_VIDEOID.mp4 or path")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    path = Path(args.vod)
    if not path.exists():
        path = Path("/root/data/mlbb/youtube_nightly/inbox") / args.vod
    vid = video_id_from_path(path) if path.exists() else video_id_from_path(Path(args.vod))
    if not vid:
        print(json.dumps([]))
        return 1

    peaks = fetch_heatmap_peaks(vid)
    if args.json:
        print(json.dumps(peaks, indent=2))
    else:
        for p in peaks:
            print(f"{p['start_sec']:.1f}s intensity={p['intensity']:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
