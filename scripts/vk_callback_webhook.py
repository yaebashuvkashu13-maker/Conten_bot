#!/usr/bin/env python3
"""VK Callback API webhook — confirmation + event sink for community bots."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

ENV_FILE = Path("/root/.video_bot.env")
LOG = Path("/root/data/mlbb/vk_callback.log")
PORT = int(os.environ.get("VK_CALLBACK_PORT", "8788"))
PATH = os.environ.get("VK_CALLBACK_PATH", "/vk/callback").rstrip("/") or "/vk/callback"


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    for key in (
        "VK_MLBB_ACCESS_TOKEN",
        "VK_ACCESS_TOKEN_MLBB",
        "VK_MLBB_GROUP_ID",
        "VK_MLBB_CONFIRMATION",
        "VK_CALLBACK_SECRET",
        "VK_MLBB_CALLBACK_SECRET",
    ):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def _cfg(env: dict[str, str]) -> tuple[str, str, str]:
    token = env.get("VK_MLBB_ACCESS_TOKEN") or env.get("VK_ACCESS_TOKEN_MLBB") or ""
    group_id = str(env.get("VK_MLBB_GROUP_ID", "234820335")).strip()
    confirmation = env.get("VK_MLBB_CONFIRMATION", "c3de1fe9").strip()
    return token, group_id, confirmation


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(msg + "\n")


class VkCallbackHandler(BaseHTTPRequestHandler):
    server_version = "VkCallback/1.0"

    def _write(self, code: int, body: str, content_type: str = "text/plain; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}

    def log_message(self, fmt: str, *args) -> None:
        log(f"{self.address_string()} {fmt % args}")

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in (PATH, PATH + "/health", "/health", "/vk/health"):
            env = load_env()
            token, group_id, confirmation = _cfg(env)
            self._write(
                200,
                json.dumps(
                    {
                        "ok": True,
                        "service": "vk_callback",
                        "path": PATH,
                        "group_id": group_id,
                        "token_set": bool(token),
                        "confirmation_set": bool(confirmation),
                    }
                ),
                "application/json",
            )
            return
        self._write(404, "not_found")

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        if path not in (PATH, PATH + "/"):
            self._write(404, "not_found")
            return

        payload = self._read_json()
        event_type = str(payload.get("type", ""))
        env = load_env()
        token, group_id, confirmation = _cfg(env)
        secret = env.get("VK_MLBB_CALLBACK_SECRET") or env.get("VK_CALLBACK_SECRET", "")

        log(f"event type={event_type} group_id={payload.get('group_id')} keys={list(payload.keys())}")

        if event_type == "confirmation":
            req_gid = str(payload.get("group_id", ""))
            if req_gid and req_gid != group_id:
                log(f"confirmation refused: group_id mismatch {req_gid} != {group_id}")
                self._write(403, "group_id_mismatch")
                return
            if not confirmation:
                self._write(500, "confirmation_not_configured")
                return
            log(f"confirmation ok group_id={req_gid}")
            self._write(200, confirmation)
            return

        if secret:
            if str(payload.get("secret", "")) != secret:
                log("refused: bad secret")
                self._write(403, "bad_secret")
                return

        if event_type == "wall_post_new":
            log(f"wall_post_new post_id={payload.get('object', {}).get('id')}")
        elif event_type:
            log(f"ack {event_type}")

        # VK expects plain "ok" for handled events.
        self._write(200, "ok")


def main() -> int:
    env = load_env()
    token, group_id, confirmation = _cfg(env)
    if not confirmation:
        raise SystemExit("VK_MLBB_CONFIRMATION missing")
    log(f"start port={PORT} path={PATH} group_id={group_id} token={'yes' if token else 'no'}")
    HTTPServer(("0.0.0.0", PORT), VkCallbackHandler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
