#!/usr/bin/env python3
"""One-time YouTube OAuth: get refresh token (run on VPS after client id+secret in .video_bot.env)."""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ENV_FILE = Path(os.environ.get("ENV_FILE", "/root/.video_bot.env"))
TOKEN_OUT = Path(os.environ.get("YOUTUBE_TOKEN_FILE", "/root/youtube_oauth_token.json"))
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]
REDIRECT_URI = os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:8080/")


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def append_env(path: Path, key: str, value: str) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    prefix = f"{key}="
    lines = [ln for ln in lines if not ln.startswith(prefix)]
    lines.append(f"{prefix}{value}")
    path.write_text("\n".join(lines) + "\n")


def exchange_code(client_id: str, client_secret: str, code: str) -> dict:
    data = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        }
    ).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


class Handler(BaseHTTPRequestHandler):
    code: str | None = None

    def do_GET(self) -> None:
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        Handler.code = (params.get("code") or [None])[0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK. You can close this tab and return to the terminal.")

    def log_message(self, *_args) -> None:
        return


def main() -> int:
    env = load_env(ENV_FILE)
    client_id = env.get("GOOGLE_OAUTH_CLIENT_ID") or env.get("YOUTUBE_OAUTH_CLIENT_ID")
    client_secret = env.get("GOOGLE_OAUTH_CLIENT_SECRET") or env.get("YOUTUBE_OAUTH_CLIENT_SECRET")
    if not client_id:
        print("Set GOOGLE_OAUTH_CLIENT_ID in /root/.video_bot.env", file=sys.stderr)
        return 1
    if not client_secret:
        print("Set GOOGLE_OAUTH_CLIENT_SECRET in /root/.video_bot.env", file=sys.stderr)
        return 1

    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
    }
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    print("Open this URL in browser (Google account that owns the YouTube channel):\n")
    print(url)
    print()
    try:
        webbrowser.open(url)
    except Exception:
        pass

    print(f"Waiting for redirect on {REDIRECT_URI} ...")
    server = HTTPServer(("127.0.0.1", 8080), Handler)
    server.handle_request()
    code = Handler.code
    if not code:
        print("No authorization code received.", file=sys.stderr)
        return 1

    token = exchange_code(client_id, client_secret, code)
    TOKEN_OUT.write_text(json.dumps(token, indent=2), encoding="utf-8")
    TOKEN_OUT.chmod(0o600)
    refresh = token.get("refresh_token")
    if refresh:
        append_env(ENV_FILE, "GOOGLE_OAUTH_REFRESH_TOKEN", refresh)
        print(f"Saved refresh token to {ENV_FILE} and {TOKEN_OUT}")
    else:
        print(f"Token saved to {TOKEN_OUT} but no refresh_token — revoke app access and retry with prompt=consent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
