#!/usr/bin/env python3
"""Fetch long Standoff VODs (>=10min) into inbox for quality montages."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_vod_segment_feed import _ffprobe_duration  # noqa: E402
from shooter_vod_segment_store import _paths  # noqa: E402
from youtube_download import download_one, load_env  # noqa: E402
from youtube_shooter_vod_prefs import (  # noqa: E402
    pick_discovery_candidate,
    title_ok,
    vod_discovery_search_cycle,
)

GAME = "standoff"
MIN_DUR = float(os.environ.get("STANDOFF_FETCH_MIN_SEC", "600"))
NEED = int(os.environ.get("STANDOFF_FETCH_NEED", "3"))
ENV_PATH = Path("/root/.video_bot.env")


def main() -> int:
    env = {**os.environ, **load_env(ENV_PATH)}
    os.environ.update(env)
    paths = _paths(GAME)
    inbox = paths["inbox"]
    parked = inbox.parent / "parked"
    inbox.mkdir(parents=True, exist_ok=True)
    parked.mkdir(parents=True, exist_ok=True)

    used: set[str] = set()
    state_path = paths["state"]
    if state_path.exists():
        st = json.loads(state_path.read_text(encoding="utf-8"))
        used.update(str(x) for x in (st.get("used_youtube_ids") or []))
        for v in st.get("vods") or []:
            if v.get("id"):
                used.add(str(v["id"]))
    for p in inbox.glob("yt_*.mp4"):
        used.add(p.stem[3:])

    # Park existing shorts that cannot pass fast probe.
    for p in list(inbox.glob("yt_*.mp4")):
        try:
            dur = _ffprobe_duration(p)
        except Exception:
            continue
        if dur < MIN_DUR:
            print(f"park short {p.name} dur={dur:.0f}", flush=True)
            dest = parked / p.name
            if dest.exists():
                p.unlink(missing_ok=True)
            else:
                p.rename(dest)

    got: list[tuple[str, float]] = []
    # Count already-good inbox files.
    for p in inbox.glob("yt_*.mp4"):
        dur = _ffprobe_duration(p)
        if dur >= MIN_DUR:
            got.append((p.name, dur))
    print(f"already long={len(got)} need={NEED}", flush=True)

    cycle = 0
    while len(got) < NEED and cycle < 24:
        params = vod_discovery_search_cycle(cycle, GAME, env)
        cycle += 1
        candidates: list[dict] = []
        from youtube_download import run_ytdlp, ytdlp_cmd, ytdlp_extra_args

        # Prefer explicit long-match search — flat playlist often omits duration and
        # then we waste time downloading 2–4min highlights.
        search_urls = list(params.get("urls", []))
        search_urls.insert(
            0,
            f"ytsearch{int(env.get('SHOOTER_VOD_SEARCH_LIMIT', '20'))}:"
            "Standoff 2 ranked full match gameplay",
        )
        for url in search_urls:
            cmd = ytdlp_cmd(env) + [
                "--flat-playlist",
                "--match-filter",
                f"duration > {int(MIN_DUR)}",
                "--print",
                "%(id)s|%(title)s|%(duration)s|%(uploader)s",
                url,
            ]
            cmd += ytdlp_extra_args(env)
            proc = run_ytdlp(cmd, env, timeout=120, label=f"search-{GAME}")
            if proc.returncode != 0:
                print(f"search fail {(proc.stderr or '')[:160]}", flush=True)
                continue
            for line in (proc.stdout or "").splitlines():
                parts = line.split("|", 3)
                if len(parts) < 2:
                    continue
                vid, title = parts[0][:11], parts[1]
                if vid in used or len(vid) != 11:
                    continue
                if not title_ok(GAME, title):
                    continue
                try:
                    dur = float(parts[2]) if len(parts) > 2 else 0.0
                except ValueError:
                    dur = 0.0
                if not dur or dur < MIN_DUR:
                    used.add(vid)
                    continue
                candidates.append(
                    {
                        "id": vid,
                        "title": title[:120],
                        "url": f"https://www.youtube.com/watch?v={vid}",
                        "duration": dur,
                        "uploader": parts[3][:60] if len(parts) > 3 else "",
                    }
                )
            time.sleep(float(params.get("delay", 4)))
            if candidates:
                break

        pick = pick_discovery_candidate(GAME, candidates)
        if not pick:
            print(f"cycle {cycle}: no candidate", flush=True)
            continue
        vid = str(pick["id"])
        print(f"cycle {cycle}: download {vid} {pick.get('title','')[:90]} dur={pick.get('duration')}", flush=True)
        used.add(vid)
        try:
            path = download_one(str(pick["url"]), inbox, env)
        except Exception as exc:  # noqa: BLE001
            print(f"dl fail {vid}: {exc}", flush=True)
            continue
        if not path:
            print(f"dl empty {vid}", flush=True)
            continue
        p = Path(path)
        dur = _ffprobe_duration(p)
        print(f"got {p.name} dur={dur:.0f} mb={p.stat().st_size // 1024 // 1024}", flush=True)
        if dur < MIN_DUR:
            dest = parked / p.name
            if dest.exists():
                p.unlink(missing_ok=True)
            else:
                p.rename(dest)
            print("parked too short", flush=True)
            continue
        got.append((p.name, dur))

    print("RESULT", got, flush=True)
    return 0 if got else 1


if __name__ == "__main__":
    raise SystemExit(main())
