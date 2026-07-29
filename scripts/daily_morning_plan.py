#!/usr/bin/env python3
"""09:00 MSK morning ops plan — live cycle/feedback snapshot, not a static template."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from daily_ops_stats import format_morning, gather_ops_snapshot, msk_now

ENV_FILE = Path("/root/.video_bot.env")
STATE_DIR = Path(os.environ.get("MLBB_STATE_DIR") or "/root/data/mlbb") / "daily_ops"


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def apply_env(env: dict[str, str]) -> None:
    """Expose bot env to gather_ops_snapshot (montage flags, quotas, paths)."""
    for key, value in env.items():
        if key and key not in os.environ:
            os.environ[key] = value
    # Prefer file values for montage/quota flags so reports match live feed.
    for key in (
        "MLBB_VOD_MONTAGE",
        "MONTAGE_ONLY_MODE",
        "POST_QUOTA_MONTAGE",
        "DAILY_GAME_CYCLE_STATE",
        "MONTAGE_DEDUP_STATE",
        "MLBB_STATE_DIR",
    ):
        if key in env:
            os.environ[key] = env[key]


def send_text(token: str, chat_id: str, text: str) -> bool:
    clean = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    result = subprocess.run(
        [
            "curl",
            "--noproxy",
            "*",
            "-sS",
            "-m",
            "60",
            "-F",
            f"chat_id={chat_id}",
            "-F",
            f"text={text[:3900]}",
            f"https://api.telegram.org/bot{token}/sendMessage",
        ],
        capture_output=True,
        text=True,
        env=clean,
    )
    try:
        return bool(json.loads(result.stdout).get("ok"))
    except json.JSONDecodeError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Daily morning ops plan (09:00 MSK)")
    ap.add_argument("--dry-run", action="store_true", help="Print only, do not Telegram")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", default="")
    ap.add_argument("--day", default="")
    args = ap.parse_args()

    env = load_env(ENV_FILE)
    apply_env(env)

    snap = gather_ops_snapshot(args.day or None)
    day = str(snap.get("day") or msk_now().date().isoformat())
    text = format_morning(snap)
    payload = {
        "kind": "morning_plan",
        "day": day,
        "generated_at_msk": msk_now().isoformat(timespec="seconds"),
        "text": text,
        "snapshot": {
            k: snap[k]
            for k in (
                "day",
                "hm",
                "active_game",
                "sends",
                "quotas",
                "remaining",
                "total_sent",
                "total_quota",
                "total_yes",
                "total_no",
                "inbox",
                "catchup_done",
                "montage_on",
            )
            if k in snap
        },
    }

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if str(args.out or "").strip() else STATE_DIR / f"morning_{day}.txt"
    out.write_text(text + "\n", encoding="utf-8")
    out.with_suffix(".json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    if args.dry_run:
        print(text)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        return 0

    token = env.get("TG_BOT_TOKEN") or os.environ.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID") or os.environ.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("TG_BOT_TOKEN or TG_CHAT_ID missing", file=sys.stderr)
        return 1
    ok = send_text(token, chat_id, text)
    print(f"morning_plan sent={ok} day={day}")
    if args.json:
        print(json.dumps({**payload, "sent": ok}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
