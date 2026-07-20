#!/usr/bin/env python3
"""Bootstrap WoT silver+gold exemplars: viral Shorts + owner labels + inbox peaks."""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from youtube_download import download_one, load_env

log = logging.getLogger("wot_silver_bootstrap")
REPO = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml"))
ENV_PATH = Path("/root/.video_bot.env")
EXEMPLAR_ROOT = Path(
    os.environ.get("HIGHLIGHT_EXEMPLAR_ROOT", str(REPO / "data" / "highlight_exemplars"))
)
WOT_INBOX = Path(os.environ.get("VOD_WOT_DATA_ROOT", "/root/data/wot")) / "youtube_nightly" / "inbox"
WOT_EXHAUSTED = WOT_INBOX.parent / "exhausted"
OWNER_LABELS = REPO / "data" / "wot_owner_labels.json"


def _count_exemplars() -> tuple[int, int]:
    good = list((EXEMPLAR_ROOT / "wot" / "good").glob("*.mp4")) if (EXEMPLAR_ROOT / "wot" / "good").exists() else []
    bad = list((EXEMPLAR_ROOT / "wot" / "bad").glob("*.mp4")) if (EXEMPLAR_ROOT / "wot" / "bad").exists() else []
    return len(good), len(bad)


def run_viral_ingest(*, max_download: int) -> int:
    script = REPO / "scripts" / "viral_reference_ingest.py"
    if not script.exists():
        script = Path("/usr/local/bin/viral_reference_ingest.py")
    cmd = [
        sys.executable,
        "-u",
        str(script),
        "--profile",
        "wot",
        "--max-download",
        str(max_download),
    ]
    log.info("viral ingest: %s", " ".join(cmd))
    proc = subprocess.run(cmd, check=False)
    return proc.returncode


def ensure_owner_vod(env: dict[str, str]) -> Path | None:
    """Download labeled VOD QbBwJJTio6A if missing."""
    if not OWNER_LABELS.exists():
        log.warning("no owner labels at %s", OWNER_LABELS)
        return None
    data = json.loads(OWNER_LABELS.read_text(encoding="utf-8"))
    vids = list((data.get("videos") or {}).keys())
    if not vids:
        return None
    vid = vids[0]
    for root in (WOT_INBOX, WOT_EXHAUSTED, Path("/root/data/mlbb/youtube_nightly/inbox")):
        candidate = root / f"yt_{vid}.mp4"
        if candidate.exists():
            return candidate
    WOT_INBOX.mkdir(parents=True, exist_ok=True)
    url = f"https://www.youtube.com/watch?v={vid}"
    log.info("download owner-labeled VOD %s", vid)
    try:
        return download_one(url, WOT_INBOX, env)
    except Exception as exc:
        log.warning("owner vod download failed: %s", exc)
        return None


def bootstrap_owner_labels(vod: Path) -> tuple[int, int]:
    script = REPO / "scripts" / "highlight_bootstrap_exemplars.py"
    if not script.exists():
        script = Path("/usr/local/bin/highlight_bootstrap_exemplars.py")
    cmd = [
        sys.executable,
        str(script),
        "--game",
        "wot",
        "--vod",
        str(vod),
        "--labels-path",
        str(OWNER_LABELS),
    ]
    env = {
        **os.environ,
        "HIGHLIGHT_EXEMPLAR_ROOT": str(EXEMPLAR_ROOT),
        "CONTENT_BOT_REPO": str(REPO),
    }
    proc = subprocess.run(cmd, env=env, check=False, capture_output=True, text=True)
    log.info("owner bootstrap: %s", (proc.stdout or proc.stderr or "")[:300])
    return _count_exemplars()


def cut_inbox_peak_exemplars(*, limit: int = 8) -> int:
    """Cut high-impact windows from existing inbox/exhausted VODs as provisional good clips."""
    from highlight_scorer import WINDOW_SEC, score_panns_audio
    from smart_video_editor import ffprobe_duration

    mp4s = list(WOT_INBOX.glob("yt_*.mp4")) + list(WOT_EXHAUSTED.glob("yt_*.mp4"))
    if not mp4s:
        return 0
    out_dir = EXEMPLAR_ROOT / "wot" / "good"
    out_dir.mkdir(parents=True, exist_ok=True)
    added = 0
    impact_min = float(os.environ.get("WOT_SILVER_PEAK_IMPACT_MIN", "0.22"))
    for vod in mp4s[:6]:
        if added >= limit:
            break
        dur = ffprobe_duration(vod)
        if dur < 90:
            continue
        # Probe every ~45s after intro
        starts = []
        t = 45.0
        while t + WINDOW_SEC < dur - 20 and len(starts) < 12:
            starts.append(t)
            t += 45.0
        scored: list[tuple[float, float]] = []
        for start in starts:
            panns = score_panns_audio(vod, start, WINDOW_SEC)
            impact = max(
                float(panns.get("panns_gun_max", 0) or 0),
                float(panns.get("panns_explosion", 0) or 0),
            )
            if impact >= impact_min:
                scored.append((impact, start))
        scored.sort(reverse=True)
        for impact, start in scored[:2]:
            if added >= limit:
                break
            dest = out_dir / f"peak_{vod.stem}_{int(start)}.mp4"
            if dest.exists():
                continue
            cmd = [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-ss",
                f"{max(0, start):.2f}",
                "-t",
                "6",
                "-i",
                str(vod),
                "-c",
                "copy",
                str(dest),
            ]
            proc = subprocess.run(cmd, check=False, timeout=120)
            if proc.returncode == 0 and dest.exists():
                added += 1
                log.info("peak exemplar %s impact=%.3f", dest.name, impact)
    return added


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-download", type=int, default=40)
    parser.add_argument("--skip-viral", action="store_true")
    parser.add_argument("--skip-owner", action="store_true")
    parser.add_argument("--skip-peaks", action="store_true")
    args = parser.parse_args()

    env = {**os.environ, **load_env(ENV_PATH)}
    os.environ.update(env)
    os.environ.setdefault("HIGHLIGHT_EXEMPLAR_ROOT", str(EXEMPLAR_ROOT))
    os.environ.setdefault("CONTENT_BOT_REPO", str(REPO))
    (EXEMPLAR_ROOT / "wot" / "good").mkdir(parents=True, exist_ok=True)
    (EXEMPLAR_ROOT / "wot" / "bad").mkdir(parents=True, exist_ok=True)

    before = _count_exemplars()
    log.info("exemplars before good=%s bad=%s", *before)

    viral_rc = 0
    if not args.skip_viral:
        viral_rc = run_viral_ingest(max_download=args.max_download)

    if not args.skip_owner:
        vod = ensure_owner_vod(env)
        if vod:
            bootstrap_owner_labels(vod)
        else:
            log.warning("owner-labeled VOD unavailable — skip gold bootstrap")

    peak_n = 0
    if not args.skip_peaks:
        peak_n = cut_inbox_peak_exemplars(limit=10)

    after = _count_exemplars()
    print(
        json.dumps(
            {
                "ok": True,
                "before": {"good": before[0], "bad": before[1]},
                "after": {"good": after[0], "bad": after[1]},
                "viral_rc": viral_rc,
                "peak_exemplars_added": peak_n,
                "exemplar_root": str(EXEMPLAR_ROOT / "wot"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if after[0] >= 5 else 1


if __name__ == "__main__":
    raise SystemExit(main())
