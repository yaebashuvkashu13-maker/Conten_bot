#!/usr/bin/env python3
"""VK Callback API webhook — confirmation + event sink for community bots."""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vod_env import DEFAULT_ENV_PATH, load_env

LOG = Path("/root/data/mlbb/vk_callback.log")
PORT = int(os.environ.get("VK_CALLBACK_PORT", "8788"))
PATH = os.environ.get("VK_CALLBACK_PATH", "/vk/callback").rstrip("/") or "/vk/callback"


def _cfg(env: dict[str, str]) -> tuple[str, str, str]:
    token = env.get("VK_MLBB_ACCESS_TOKEN") or env.get("VK_ACCESS_TOKEN_MLBB") or ""
    group_id = str(env.get("VK_MLBB_GROUP_ID", "")).strip()
    confirmation = (env.get("VK_MLBB_CONFIRMATION") or env.get("VK_CALLBACK_CONFIRMATION") or "").strip()
    secret = (env.get("VK_CALLBACK_SECRET") or env.get("VK_MLBB_CALLBACK_SECRET") or "").strip()
    if not secret:
        raise SystemExit("VK_CALLBACK_SECRET (or VK_MLBB_CALLBACK_SECRET) is required")
    if not confirmation:
        raise SystemExit("VK_MLBB_CONFIRMATION (or VK_CALLBACK_CONFIRMATION) is required")
    if not group_id:
        raise SystemExit("VK_MLBB_GROUP_ID is required")
    return token, group_id, confirmation


def _callback_secret(env: dict[str, str]) -> str:
    secret = (env.get("VK_CALLBACK_SECRET") or env.get("VK_MLBB_CALLBACK_SECRET") or "").strip()
    if not secret:
        raise SystemExit("VK_CALLBACK_SECRET missing")
    return secret


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
            env = load_env(DEFAULT_ENV_PATH)
            try:
                token, _, confirmation = _cfg(env)
            except SystemExit:
                self._write(503, json.dumps({"ok": False, "service": "vk_callback"}), "application/json")
                return
            self._write(
                200,
                json.dumps(
                    {
                        "ok": True,
                        "service": "vk_callback",
                        "path": PATH,
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
        env = load_env(DEFAULT_ENV_PATH)
        token, group_id, confirmation = _cfg(env)
        secret = _callback_secret(env)

        log(f"event type={event_type} keys={list(payload.keys())}")

        if event_type == "confirmation":
            req_gid = str(payload.get("group_id", ""))
            if req_gid and req_gid != group_id:
                log(f"confirmation refused: group_id mismatch {req_gid} != {group_id}")
                self._write(403, "group_id_mismatch")
                return
            log("confirmation ok")
            self._write(200, confirmation)
            return

        if str(payload.get("secret", "")) != secret:
            log("refused: bad secret")
            self._write(403, "bad_secret")
            return

        if event_type == "wall_post_new":
            log(f"wall_post_new post_id={payload.get('object', {}).get('id')}")
        elif event_type:
            log(f"ack {event_type}")

        self._write(200, "ok")


def main() -> int:
    env = load_env(DEFAULT_ENV_PATH)
    token, group_id, confirmation = _cfg(env)
    _callback_secret(env)
    log(f"start port={PORT} path={PATH} group_configured=yes token={'yes' if token else 'no'}")
    HTTPServer(("0.0.0.0", PORT), VkCallbackHandler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
