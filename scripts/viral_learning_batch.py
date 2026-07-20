#!/usr/bin/env python3
"""
Batch viral learning: download N high-view Shorts per game, analyze features,
compare with current owner-good style, improve exemplars/thresholds.

Does NOT send Shorts to Telegram — only optional text improve report.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from highlight_scorer import normalize_profile
from viral_improve_from_reference import build_improve_report, format_telegram, send_message
from viral_reference_ingest import ALL_PROFILES, ingest_game

REPO = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml"))


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def run_batch(
    *,
    per_game: int,
    profiles: tuple[str, ...],
    skip_download: bool,
    tiktok_limit: int,
    train: bool,
    improve: bool,
    apply_env: bool,
    notify: bool,
) -> int:
    os.environ.setdefault("HIGHLIGHT_HEATMAP", "0")
    os.environ.setdefault("HIGHLIGHT_USE_OWNER_ANCHORS", "0")
    os.environ["VIRAL_INGEST_FAST"] = os.environ.get("VIRAL_INGEST_FAST", "1")
    os.environ["VIRAL_INGEST_SKIP_RULE_GATE"] = os.environ.get("VIRAL_INGEST_SKIP_RULE_GATE", "1")

    failed: list[str] = []
    for profile in profiles:
        log(f"=== ingest+analyze {profile} max={per_game} (no Telegram videos) ===")
        try:
            rc = ingest_game(
                profile,
                max_download=per_game,
                skip_download=skip_download,
                tiktok_limit=tiktok_limit,
            )
        except Exception as exc:
            log(f"ERROR {profile} ingest: {exc}")
            failed.append(profile)
            continue
        if rc != 0:
            failed.append(profile)
            log(f"WARN {profile} ingest rc={rc}")

    if train:
        for profile in ("pubg", "mobile_legends", "standoff", "genshin", "wot"):
            if profile not in profiles:
                continue
            script = REPO / "scripts" / "highlight_train.py"
            if script.exists():
                log(f"highlight_train {profile}")
                subprocess.run(
                    [sys.executable, str(script), "--profile", profile],
                    cwd=str(REPO),
                    check=False,
                )

    improve_report = None
    if improve:
        log("=== compare with current good + improve exemplars/thresholds ===")
        improve_report = build_improve_report(profiles, apply_env=apply_env, top_k=5)
        out = REPO / "data" / "viral_reference" / "improve_report.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(improve_report, indent=2, ensure_ascii=False), encoding="utf-8")
        text = format_telegram(improve_report)
        print(text)
        if notify:
            from youtube_download import load_env

            env = load_env()
            token = env.get("TG_BOT_TOKEN", "")
            chat_id = env.get("TG_CHAT_ID", "")
            if token and chat_id:
                send_message(token, chat_id, text)

    total = sum(g.get("viral_clips", 0) for g in (improve_report or {}).get("games", []))
    if improve_report is None:
        total = -1
    log(f"done failed={failed or 'none'} improve_clips={total}")
    return 1 if failed and not improve_report else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download+analyze viral Shorts to improve scoring (never sends Shorts)"
    )
    parser.add_argument("--per-game", type=int, default=10)
    parser.add_argument("--profile", default="all", choices=["all", *ALL_PROFILES, "mlbb"])
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--tiktok-limit", type=int, default=0)
    parser.add_argument("--train", action="store_true", help="Retrain highlight LR after improve")
    parser.add_argument(
        "--no-improve",
        action="store_true",
        help="Skip compare/improve step (ingest only)",
    )
    parser.add_argument(
        "--apply-env",
        action="store_true",
        help="Write threshold nudges into .video_bot.env",
    )
    parser.add_argument(
        "--telegram",
        action="store_true",
        help="Send TEXT improve report only (never Shorts videos)",
    )
    args = parser.parse_args()

    profiles = ALL_PROFILES if args.profile == "all" else (normalize_profile(args.profile),)
    return run_batch(
        per_game=max(3, args.per_game),
        profiles=profiles,
        skip_download=args.skip_download,
        tiktok_limit=args.tiktok_limit,
        train=args.train,
        improve=not args.no_improve,
        apply_env=args.apply_env,
        notify=args.telegram,
    )


if __name__ == "__main__":
    raise SystemExit(main())
