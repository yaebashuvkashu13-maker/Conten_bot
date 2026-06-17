#!/usr/bin/env python3
"""Shared Telegram Bot API helpers for MLBB pipelines."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from pathlib import Path

ENV_PATH = Path("/root/.video_bot.env")


def load_env(path: Path | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env_file = path or ENV_PATH
    if not env_file.exists():
        return env
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        env.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    return env


def bot_token(env: dict[str, str] | None = None) -> str:
    e = env or load_env()
    return e.get("TG_BOT_TOKEN", "") or e.get("TELEGRAM_BOT_TOKEN", "")


def owner_chat_id(env: dict[str, str] | None = None) -> str:
    e = env or load_env()
    return e.get("TG_CHAT_ID", "") or e.get("TELEGRAM_CHAT_ID", "")


def is_owner(chat_id: str | int, env: dict[str, str] | None = None) -> bool:
    e = env or load_env()
    cid = str(chat_id)
    owners = {owner_chat_id(e)} - {""}
    for item in e.get("AD_OWNER_CHAT_IDS", e.get("OWNER_CHAT_IDS", "")).split(","):
        item = item.strip()
        if item:
            owners.add(item)
    return cid in owners


def send_message(text: str, *, chat_id: str = "", token: str = "", env: dict[str, str] | None = None) -> bool:
    e = env or load_env()
    token = token or bot_token(e)
    chat_id = chat_id or owner_chat_id(e)
    if not token or not chat_id:
        return False
    payload = json.dumps({"chat_id": chat_id, "text": text[:3900]}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
            return bool(body.get("ok"))
    except Exception:
        return False


def send_message_curl(text: str, *, chat_id: str = "", token: str = "", env: dict[str, str] | None = None) -> bool:
    """Fallback via curl (matches calibration_feed behaviour)."""
    e = env or load_env()
    token = token or bot_token(e)
    chat_id = chat_id or owner_chat_id(e)
    if not token or not chat_id:
        return False
    clean_env = {k: v for k, v in e.items() if "proxy" not in k.lower()}
    proc = subprocess.run(
        [
            "curl",
            "-sS",
            "--noproxy",
            "*",
            "-F",
            f"chat_id={chat_id}",
            "-F",
            f"text={text[:3900]}",
            f"https://api.telegram.org/bot{token}/sendMessage",
        ],
        capture_output=True,
        text=True,
        env=clean_env,
        timeout=30,
        check=False,
    )
    try:
        return bool(json.loads(proc.stdout or "{}").get("ok"))
    except json.JSONDecodeError:
        return False
