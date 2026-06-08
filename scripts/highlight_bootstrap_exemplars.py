#!/usr/bin/env python3
"""Cut good/bad exemplar clips from owner labels for CLIP scoring."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

LABEL_ENV_KEYS = {
    "pubg": "PUBG_OWNER_LABELS_PATH",
    "standoff": "STANDOFF_OWNER_LABELS_PATH",
    "mobile_legends": "MLBB_OWNER_LABELS_PATH",
    "genshin": "GENSHIN_OWNER_LABELS_PATH",
    "wot": "WOT_OWNER_LABELS_PATH",
}
DEFAULT_LABEL_FILES = {
    "pubg": "pubg_owner_labels.json",
    "standoff": "standoff_owner_labels.json",
    "mobile_legends": "mobile_legends_owner_labels.json",
    "genshin": "genshin_owner_labels.json",
    "wot": "wot_owner_labels.json",
}


def owner_labels_path(game: str) -> Path:
    game = game.strip().lower()
    if game == "mlbb":
        game = "mobile_legends"
    env_key = LABEL_ENV_KEYS.get(game, "PUBG_OWNER_LABELS_PATH")
    default_name = DEFAULT_LABEL_FILES.get(game, "pubg_owner_labels.json")
    path = Path(os.environ.get(env_key, str(REPO / "data" / default_name)))
    if path.exists():
        return path
    for fb in (Path(f"/root/data/mlbb/{default_name}"), REPO / "data" / default_name):
        if fb.exists():
            return fb
    return path
INBOX = Path("/root/data/mlbb/youtube_nightly/inbox")
OUT = Path(os.environ.get("HIGHLIGHT_EXEMPLAR_ROOT", str(REPO / "data" / "highlight_exemplars")))
CLIP_SEC = 4.0


def cut_clip(vod: Path, start: float, out: Path) -> bool:
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-ss",
        f"{max(0, start):.2f}",
        "-t",
        str(CLIP_SEC),
        "-i",
        str(vod),
        "-c",
        "copy",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, check=False, timeout=120)
    return proc.returncode == 0 and out.exists()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--game", default="pubg")
    parser.add_argument("--vod", default="yt_n97cHIR9Qow.mp4")
    parser.add_argument("--labels-path", type=Path, default=None)
    args = parser.parse_args()

    vod = INBOX / args.vod if not Path(args.vod).exists() else Path(args.vod)
    if not vod.exists():
        print(f"REFUSED: bootstrap, reason=vod_missing {vod}")
        return 1
    labels_path = args.labels_path or owner_labels_path(args.game)
    if not labels_path.exists():
        print(f"REFUSED: bootstrap, reason=no_owner_labels {labels_path}")
        return 1

    data = json.loads(labels_path.read_text(encoding="utf-8"))
    vid = vod.stem[3:] if vod.stem.startswith("yt_") else vod.stem
    rows = data.get("videos", {}).get(vid, [])
    good_n = bad_n = 0
    for row in rows:
        label = row.get("label")
        if label not in ("good", "bad"):
            continue
        t = float(row["time_sec"])
        name = f"{vid}_{int(t)}_{label}.mp4"
        dest = OUT / args.game / label / name
        if cut_clip(vod, t - 1.0, dest):
            if label == "good":
                good_n += 1
            else:
                bad_n += 1
    print(f"OK exemplars game={args.game} good={good_n} bad={bad_n} dir={OUT / args.game}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
