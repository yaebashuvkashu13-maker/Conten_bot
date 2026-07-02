#!/usr/bin/env python3
"""Minimal HTTP webhook for n8n Cloud → VPS MLBB pipeline (stdlib only)."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

ENV_FILE = Path("/root/.video_bot.env")
REPORT_FILE = Path("/root/data/mlbb/youtube_nightly/last_report.json")
LATEST_MONTAGE = Path("/root/data/mlbb/publish/latest_montage.json")
LOG = Path("/root/data/mlbb/n8n_webhook.log")
PORT = int(os.environ.get("N8N_WEBHOOK_PORT", "8787"))
SECRET = os.environ.get("N8N_WEBHOOK_SECRET", "")


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if not ENV_FILE.exists():
        return env
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"{msg}\n"
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line)


def authorized(headers) -> bool:
    if not SECRET:
        return False
    auth = headers.get("Authorization", "")
    if auth == f"Bearer {SECRET}":
        return True
    token = headers.get("X-N8N-Token", "")
    return token == SECRET


def spawn_background(cmd: list[str]) -> dict:
    log(f"spawn {' '.join(cmd)}")
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {"ok": True, "started": True, "cmd": cmd}


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"error": "invalid_json"}


def find_latest_video() -> Path | None:
    out_dir = Path("/root/videos")
    if not out_dir.exists():
        return None
    files = sorted(out_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: D401
        log(f"HTTP {self.address_string()} {fmt % args}")

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self, method: str) -> None:
        if not authorized(self.headers):
            self._json(401, {"ok": False, "error": "unauthorized"})
            return

        path = urlparse(self.path).path.rstrip("/") or "/"

        if path in ("/health", "/"):
            self._json(200, {"ok": True, "service": "mlbb-n8n-webhook"})
            return

        if path == "/status" and method == "GET":
            self._json(
                200,
                {
                    "ok": True,
                    "youtube_nightly": read_json(REPORT_FILE),
                    "latest_montage": read_json(LATEST_MONTAGE),
                },
            )
            return

        if path == "/latest-montage" and method == "GET":
            data = read_json(LATEST_MONTAGE)
            if not data:
                vid = find_latest_video()
                if vid:
                    data = {"path": str(vid), "name": vid.name}
            self._json(200, {"ok": True, "montage": data})
            return

        if path == "/trigger/nightly-youtube" and method == "POST":
            payload = spawn_background(["/usr/local/bin/nightly_youtube.sh"])
            payload["hint"] = "Результат придёт в Telegram; статус: GET /status"
            self._json(202, payload)
            return

        if path == "/trigger/morning-plan" and method == "POST":
            payload = spawn_background(["python3", "/usr/local/bin/daily_morning_plan.py"])
            self._json(202, payload)
            return

        if path == "/trigger/discover-youtube" and method == "POST":
            payload = spawn_background(
                ["python3", "/usr/local/bin/nightly_youtube_montage.py", "--discover-only"]
            )
            self._json(202, payload)
            return

        self._json(404, {"ok": False, "error": "not_found", "path": path})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")


def main() -> None:
    env = load_env()
    global SECRET, PORT
    SECRET = SECRET or env.get("N8N_WEBHOOK_SECRET", "")
    PORT = int(env.get("N8N_WEBHOOK_PORT", str(PORT)))
    if not SECRET:
        raise SystemExit("N8N_WEBHOOK_SECRET missing in env")
    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    log(f"listening on {PORT}")
    print(f"mlbb n8n webhook :{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
