#!/usr/bin/env python3
"""Build negative ref crops for coordination/quick-chat HUD (not kill banners)."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

# Phrases that appear in the top banner zone but are NOT kill streaks.
COORDINATION_PHRASES: list[tuple[str, str]] = [
    ("gather", "GATHER"),
    ("retreat", "RETREAT"),
    ("attack", "ATTACK"),
    ("regroup", "REGROUP"),
    ("defend", "DEFEND"),
    ("on_my_way", "ON MY WAY"),
    ("request_backup", "REQUEST BACKUP"),
    ("initiate", "INITIATE"),
    ("clear_lane", "CLEAR LANE"),
    ("gather_ru", "СОБЕРИТЕСЬ"),
    ("attack_ru", "В АТАКУ"),
    ("retreat_ru", "ОТСТУПАЙТЕ"),
]


def banner_ref_root() -> Path:
    from mlbb_banner_ref_ingest import banner_ref_root as root_fn

    return root_fn()


def _render_coordination_patch(text: str):
    import cv2
    import numpy as np

    # Match in-game banner aspect used by ref matcher (160x48).
    img = np.zeros((48, 160, 3), dtype=np.uint8)
    img[:, :] = (30, 170, 220)  # gold HUD wash (BGR)
    cv2.rectangle(img, (8, 10), (152, 38), (50, 120, 255), 2)
    cv2.putText(
        img,
        text[:18],
        (14, 30),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.45,
        (255, 255, 255),
        1,
        cv2.LINE_AA,
    )
    # Fake portrait circles (coordination banners also show speaker icon).
    cv2.circle(img, (24, 24), 10, (80, 80, 180), -1)
    cv2.circle(img, (136, 24), 10, (80, 80, 180), -1)
    return img


def build_coordination_negatives(*, force: bool = False) -> list[dict]:
    out_dir = banner_ref_root() / "owner_cal" / "negative" / "coordination"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for slug, text in COORDINATION_PHRASES:
        path = out_dir / f"{slug}.png"
        if path.exists() and path.stat().st_size > 200 and not force:
            rows.append({"path": str(path), "reason": "coordination", "text": text, "cached": True})
            continue
        patch = _render_coordination_patch(text)
        import cv2

        cv2.imwrite(str(path), patch)
        rows.append({"path": str(path), "reason": "coordination", "text": text})
    try:
        from mlbb_banner_ref_match import clear_banner_ref_cache

        clear_banner_ref_cache()
    except Exception:
        pass
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    rows = build_coordination_negatives(force=args.force)
    print(f"coordination negatives: {len(rows)} -> {banner_ref_root() / 'owner_cal/negative/coordination'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
