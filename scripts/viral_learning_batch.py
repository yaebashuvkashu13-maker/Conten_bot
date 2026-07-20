#!/usr/bin/env python3
"""
Batch viral learning: download N high-view Shorts per game, extract features,
cluster archetypes, refresh exemplars, emit learning report.

Designed for VPS cron/systemd-run — does not block VOD montage feeds.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from highlight_scorer import normalize_profile
from viral_reference_ingest import ALL_PROFILES, ingest_game
from viral_learning_report import build_report, format_telegram, send_message

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
    notify: bool,
) -> int:
    os.environ.setdefault("HIGHLIGHT_HEATMAP", "0")
    os.environ.setdefault("HIGHLIGHT_USE_OWNER_ANCHORS", "0")
    os.environ["VIRAL_INGEST_FAST"] = os.environ.get("VIRAL_INGEST_FAST", "1")
    os.environ["VIRAL_INGEST_SKIP_RULE_GATE"] = os.environ.get("VIRAL_INGEST_SKIP_RULE_GATE", "1")

    failed: list[str] = []
    for profile in profiles:
        log(f"=== ingest {profile} max={per_game} ===")
        rc = ingest_game(
            profile,
            max_download=per_game,
            skip_download=skip_download,
            tiktok_limit=tiktok_limit,
        )
        if rc != 0:
            failed.append(profile)
            log(f"WARN {profile} ingest rc={rc}")

    if train:
        for profile in ("pubg", "mobile_legends"):
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

    log("=== learning report ===")
    report = build_report(profiles)
    report_path = REPO / "data" / "viral_reference" / "learning_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    import json

    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    text = format_telegram(report)
    print(text)

    if notify:
        from youtube_download import load_env

        env = load_env()
        token = env.get("TG_BOT_TOKEN", "")
        chat_id = env.get("TG_CHAT_ID", "")
        if token and chat_id:
            send_message(token, chat_id, text)

    log(f"done failed={failed or 'none'} total_clips={report['total_clips']}")
    return 1 if failed and report["total_clips"] == 0 else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Viral Shorts learning batch (all games)")
    parser.add_argument("--per-game", type=int, default=10, help="Shorts to download/analyze per game")
    parser.add_argument("--profile", default="all", choices=["all", *ALL_PROFILES, "mlbb"])
    parser.add_argument("--skip-download", action="store_true", help="Re-analyze local files only")
    parser.add_argument("--tiktok-limit", type=int, default=0, help="Extra TikTok MLBB clips (0=off)")
    parser.add_argument("--train", action="store_true", help="Run highlight_train for pubg/mlbb after ingest")
    parser.add_argument("--telegram", action="store_true", help="Send summary to owner chat")
    args = parser.parse_args()

    if args.profile == "all":
        profiles = ALL_PROFILES
    else:
        profiles = (normalize_profile(args.profile),)

    return run_batch(
        per_game=max(3, args.per_game),
        profiles=profiles,
        skip_download=args.skip_download,
        tiktok_limit=args.tiktok_limit,
        train=args.train,
        notify=args.telegram,
    )


if __name__ == "__main__":
    raise SystemExit(main())
