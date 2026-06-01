"""Minimal HTTP API for n8n (when Execute Command node is unavailable)."""
from __future__ import annotations

import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse


PROJECT_DIR = os.environ.get("PROJECT_DIR", "/workspace/Conten_bot")
VIDEO_ROOT = os.environ.get("VIDEO_ROOT", "/workspace/datasets/tiktok/mlbb")
GAMEPLAY_CSV = os.environ.get(
    "GAMEPLAY_CSV", "/workspace/datasets/tiktok/reports/gameplay_filter_latest.csv"
)
BIND_HOST = os.environ.get("N8N_API_HOST", "0.0.0.0")
BIND_PORT = int(os.environ.get("N8N_API_PORT", "8765"))


def _run(cmd: list[str], *, cwd: str | None = None, timeout: int = 3600) -> dict:
    env = os.environ.copy()
    proc = subprocess.run(
        cmd,
        cwd=cwd or PROJECT_DIR,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": proc.stdout[-8000:],
        "stderr": proc.stderr[-4000:],
    }


def handle_check() -> dict:
    script = (
        "echo '=== mp4 ===' && "
        f"find {VIDEO_ROOT} -name '*.mp4' 2>/dev/null | wc -l && "
        "echo '=== reports ===' && "
        "ls -lh /workspace/datasets/tiktok/reports/ 2>/dev/null"
    )
    return _run(["bash", "-lc", script], timeout=120)


def handle_instagram() -> dict:
    return _run(
        ["bash", "-lc", "python3 -m pip install -e . -q && python3 -m content_bot.main --config config.yaml"],
        timeout=1800,
    )


def handle_montage(hero: str) -> dict:
    hero = hero.lower().strip()
    allowed = {"gusion", "lancelot", "chou", "fanny", "hayabusa"}
    if hero not in allowed:
        return {"ok": False, "error": f"Unknown hero {hero}. Allowed: {sorted(allowed)}"}
    cmd = (
        f"python3 -m pip install -e . -q && "
        f"python3 -m content_bot.montage_builder --hero {hero} "
        f"--video-root {VIDEO_ROOT} --gameplay-csv {GAMEPLAY_CSV} --send-telegram"
    )
    return _run(["bash", "-lc", cmd], timeout=3600)


class N8nAPIHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        print(f"[n8n_api] {self.address_string()} {format % args}")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _respond(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/health":
            self._respond(200, {"ok": True, "service": "conten_bot_n8n_api"})
            return
        self._respond(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        body = self._read_json()
        qs = parse_qs(urlparse(self.path).query)

        if path == "/check":
            self._respond(200, handle_check())
        elif path == "/instagram":
            self._respond(200, handle_instagram())
        elif path == "/montage":
            hero = body.get("hero") or (qs.get("hero") or ["gusion"])[0]
            self._respond(200, handle_montage(str(hero)))
        else:
            self._respond(404, {"ok": False, "error": "not found"})


def main() -> None:
    server = HTTPServer((BIND_HOST, BIND_PORT), N8nAPIHandler)
    print(f"Conten_bot n8n API listening on http://{BIND_HOST}:{BIND_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
