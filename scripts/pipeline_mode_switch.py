#!/usr/bin/env python3
"""Switch VPS between VOD segment feed and multi-game Shorts calibration."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

ENV_PATH = Path(os.environ.get("ENV_FILE", "/root/.video_bot.env"))


def _read_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if not ENV_PATH.exists():
        return out
    for line in ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip().strip("'\"")
    return out


def _set_env(updates: dict[str, str]) -> None:
    text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    for key, val in updates.items():
        line = f"{key}={val}"
        if f"{key}=" in text:
            import re

            text = re.sub(rf"^{re.escape(key)}=.*$", line, text, flags=re.M)
        else:
            text = text.rstrip() + f"\n{line}\n"
    ENV_PATH.write_text(text, encoding="utf-8")


def _pkill(*patterns: str) -> None:
    for pat in patterns:
        subprocess.run(["pkill", "-f", pat], check=False)


def activate_shorts_mode() -> str:
    """Stop VOD feeds; enable Shorts calibration for all games."""
    _pkill(
        "mlbb_vod_segment_feed",
        "shooter_vod_segment_feed",
        "daily_cycle_runner",
    )
    time.sleep(1)
    _set_env(
        {
            "MLBB_VOD_ONLY": "0",
            "MLBB_VOD_DISABLED": "1",
            "MLBB_CALIBRATION_FEED_ENABLED": "1",
            "MULTI_GAME_SHORTS_MODE": "1",
            "MLBB_CALIBRATION_BATCH": "3",
            "MLBB_SEND_ENABLED": "1",
            "SHOOTER_VOD_DISABLED": "1",
        }
    )
    return (
        "✅ Режим Shorts-калибровки включён.\n"
        "VOD-нарезка остановлена.\n"
        "Команда: /shorts_all — по 3 Shorts на каждую из 5 игр.\n"
        "Или /shorts pubg — одна игра."
    )


def activate_vod_mode() -> str:
    """Restore VOD segment daily cycle."""
    _pkill("mlbb_continuous_worker")
    _set_env(
        {
            "MLBB_VOD_ONLY": "1",
            "MLBB_VOD_DISABLED": "0",
            "MLBB_CALIBRATION_FEED_ENABLED": "0",
            "MULTI_GAME_SHORTS_MODE": "0",
            "SHOOTER_VOD_DISABLED": "0",
            "DAILY_GAME_CYCLE_ENABLED": "1",
        }
    )
    return (
        "✅ Режим VOD-нарезки включён.\n"
        "Shorts-калибровка отключена.\n"
        "Команды: /mlbb_vod, дневной цикл MLBB→PUBG→Standoff."
    )


def mode_status() -> str:
    e = _read_env()
    if e.get("MULTI_GAME_SHORTS_MODE") == "1" or (
        e.get("MLBB_CALIBRATION_FEED_ENABLED") == "1" and e.get("MLBB_VOD_ONLY") != "1"
    ):
        mode = "Shorts калибровка (5 игр)"
    elif e.get("MLBB_VOD_ONLY") == "1":
        mode = "VOD нарезка"
    else:
        mode = "смешанный / неизвестный"
    return (
        f"Режим: {mode}\n"
        f"MLBB_VOD_ONLY={e.get('MLBB_VOD_ONLY', '?')}\n"
        f"MLBB_CALIBRATION_FEED_ENABLED={e.get('MLBB_CALIBRATION_FEED_ENABLED', '?')}\n"
        f"MULTI_GAME_SHORTS_MODE={e.get('MULTI_GAME_SHORTS_MODE', '?')}"
    )


if __name__ == "__main__":
    import sys

    cmd = (sys.argv[1] if len(sys.argv) > 1 else "status").strip().lower()
    if cmd == "shorts":
        print(activate_shorts_mode())
    elif cmd == "vod":
        print(activate_vod_mode())
    else:
        print(mode_status())
