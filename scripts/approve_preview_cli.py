#!/usr/bin/env python3
"""Owner approves preview -> sendVideo once."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from segment_preview import approve_preview, send_approved_montage

ENV_FILE = Path("/root/.video_bot.env")


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if not ENV_FILE.exists():
        return env
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("preview_id")
    args = parser.parse_args()

    env = load_env()
    pkg = approve_preview(args.preview_id, by_chat="cli")
    if not pkg:
        print(f"REFUSED: preview, reason=unknown_id {args.preview_id}, visual_passed=0/0")
        return 1

    montage_meta_path = Path(pkg.get("montage_path", "")).with_suffix(".json")
    caption = pkg["game"] + " peak montage (owner approved)"
    if montage_meta_path.exists():
        try:
            meta = json.loads(montage_meta_path.read_text(encoding="utf-8"))
            caption = meta.get("game", pkg["game"]) + " | owner approved"
        except (json.JSONDecodeError, OSError):
            pass

    send_approved_montage(pkg, env, caption)
    segs = pkg.get("segments", [])
    screens = [s["path"] for seg in segs for s in seg.get("screenshots", [])]
    ts = [seg["start"] for seg in segs]
    from segment_preview import PROOF_ROOT

    print(
        f"SENT: json={PROOF_ROOT / args.preview_id / 'proof.json'}, "
        f"screens={len(screens)}, timestamps={ts}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
