#!/usr/bin/env python3
"""Upload MLBB video to VK community — clip-optimized (9:16 vertical)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

ENV_FILE = Path("/root/.video_bot.env")
CLIP_MAX_SEC = float(os.environ.get("VK_MLBB_CLIP_MAX_SEC", "90"))
CLIP_W = int(os.environ.get("VK_MLBB_CLIP_WIDTH", "1080"))
CLIP_H = int(os.environ.get("VK_MLBB_CLIP_HEIGHT", "1920"))


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
        "VK_API_VERSION",
    ):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def vk_token(env: dict[str, str]) -> str:
    token = env.get("VK_MLBB_ACCESS_TOKEN") or env.get("VK_ACCESS_TOKEN_MLBB") or ""
    if not token:
        raise RuntimeError("VK_MLBB_ACCESS_TOKEN missing in /root/.video_bot.env")
    return token


def vk_group_id(env: dict[str, str]) -> int:
    return int(env.get("VK_MLBB_GROUP_ID", "234820335"))


def token_permissions_summary(token: str, env: dict[str, str] | None = None) -> str:
    env = env or load_env()
    group_id = vk_group_id(env)
    try:
        perms = vk_call("groups.getTokenPermissions", {"group_id": group_id}, token)
    except RuntimeError as exc:
        return f"unknown ({exc})"
    names = sorted(p.get("name", "") for p in perms.get("permissions", []) if p.get("name"))
    return ",".join(names) or "none"


def assert_upload_token(token: str, group_id: int) -> None:
    """Community Callback keys (manage-only) cannot call video.save."""
    try:
        perms = vk_call("groups.getTokenPermissions", {"group_id": group_id}, token)
    except RuntimeError as exc:
        if "error_code': 27" in str(exc) or "Group authorization failed" in str(exc):
            return
        raise
    names = {p.get("name", "") for p in perms.get("permissions", []) if p.get("name")}
    if names and names <= {"manage"}:
        raise RuntimeError(
            "VK token is community manage-only (Callback key). "
            "Create a community API key with rights: video (+ wall). "
            "Управление группой → Работа с API → Создать ключ. "
            "Or use user OAuth with scopes video+groups (see docs/VK_MLBB_TOKEN.md)."
        )


def assert_short_video_token(token: str, group_id: int) -> None:
    try:
        perms = vk_call("groups.getTokenPermissions", {"group_id": group_id}, token)
    except RuntimeError as exc:
        if "error_code': 27" in str(exc) or "Group authorization failed" in str(exc):
            raise RuntimeError(
                "VK community token cannot call shortVideo.create (error 27). "
                "Recreate community key with video permission, or use user OAuth token."
            ) from exc
        raise
    names = {p.get("name", "") for p in perms.get("permissions", []) if p.get("name")}
    if names and "video" not in names and names <= {"manage"}:
        raise RuntimeError(
            "VK token has only manage (Callback). Enable video on community API key "
            "and update VK_MLBB_ACCESS_TOKEN secret."
        )


def _vk_opener() -> urllib.request.OpenerDirector:
    """VK API must not go through HTTP_PROXY (TikTok proxy is often dead)."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def vk_call(method: str, params: dict, token: str) -> dict:
    payload = dict(params)
    payload["access_token"] = token
    payload["v"] = os.environ.get("VK_API_VERSION", "5.199")
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.vk.ru/method/{method}",
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with _vk_opener().open(req, timeout=90) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if "error" in body:
        raise RuntimeError(f"vk_{method}:{body['error']}")
    return body["response"]


def ffprobe_duration(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return 0.0


def prepare_clip_source(source: Path) -> Path:
    """Vertical 9:16, max 90s — VK algorithm treats this as Clips feed candidate."""
    out = Path(tempfile.mkdtemp(prefix="vk-clip-")) / "clip.mp4"
    vf = (
        f"scale={CLIP_W}:{CLIP_H}:force_original_aspect_ratio=decrease,"
        f"pad={CLIP_W}:{CLIP_H}:(ow-iw)/2:(oh-ih)/2:black,"
        f"setsar=1"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-t",
        str(CLIP_MAX_SEC),
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "22",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        "-pix_fmt",
        "yuv420p",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not out.exists() or out.stat().st_size < 1024:
        raise RuntimeError(f"ffmpeg_clip_fail:{proc.stderr[-400:]}")
    return out


def upload_video_file(upload_url: str, path: Path) -> dict:
    proc = subprocess.run(
        ["curl", "-sS", "-m", "900", "--noproxy", "*", "-F", f"video_file=@{path}", upload_url],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"curl_upload_fail:{proc.stderr[-300:]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"upload_bad_json:{proc.stdout[:300]}") from exc


def _product_description_suffix(game: str = "mobile_legends") -> str:
    try:
        from vk_game_products import description_suffix

        return description_suffix(game)
    except Exception:
        return ""


def upload_short_video_file(upload_url: str, path: Path) -> dict:
    proc = subprocess.run(
        ["curl", "-sS", "-m", "900", "--noproxy", "*", "-F", f"file=@{path}", upload_url],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"curl_upload_fail:{proc.stderr[-300:]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"upload_bad_json:{proc.stdout[:300]}") from exc


def publish_short_clip(
    source: Path,
    *,
    title: str = "",
    description: str = "",
    game: str = "mobile_legends",
    publish: bool = True,
    env: dict[str, str] | None = None,
) -> dict:
    """Upload vertical clip via shortVideo.create (community token with video right)."""
    env = env or load_env()
    token = vk_token(env)
    group_id = vk_group_id(env)
    assert_short_video_token(token, group_id)
    prepared = prepare_clip_source(source)
    try:
        if not title:
            title = f"MLBB {time.strftime('%d.%m %H:%M')}"
        product_line = _product_description_suffix(game)
        base_desc = description or "Mobile Legends"
        if product_line:
            base_desc = f"{base_desc}\n{product_line}"
        file_size = prepared.stat().st_size
        created = vk_call(
            "shortVideo.create",
            {
                "group_id": group_id,
                "file_size": file_size,
                "description": base_desc[:5000],
                "wallpost": 0,
            },
            token,
        )
        upload_url = created.get("upload_url")
        if not upload_url:
            raise RuntimeError("vk_no_upload_url")
        up = upload_short_video_file(upload_url, prepared)
        owner_id = int(up.get("owner_id") or created.get("owner_id") or -group_id)
        video_id = int(up.get("video_id") or created.get("video_id") or 0)
        if not video_id:
            raise RuntimeError(f"vk_no_video_id upload={up}")

        if publish:
            time.sleep(float(os.environ.get("VK_SHORT_VIDEO_PROCESS_SEC", "12")))
            try:
                vk_call(
                    "shortVideo.publish",
                    {"owner_id": owner_id, "video_id": video_id, "group_id": group_id},
                    token,
                )
            except RuntimeError as exc:
                if "error_code': 27" not in str(exc):
                    raise
                # Some tokens can upload but not publish — clip may still appear after processing.
                up["publish_skipped"] = str(exc)

        if title and title.strip():
            try:
                vk_call(
                    "shortVideo.edit",
                    {
                        "owner_id": owner_id,
                        "video_id": video_id,
                        "title": title[:128],
                        "description": base_desc[:5000],
                    },
                    token,
                )
            except RuntimeError:
                pass

        return {
            "owner_id": owner_id,
            "video_id": video_id,
            "upload": up,
            "duration": ffprobe_duration(prepared),
            "prepared_size": file_size,
            "api": "shortVideo.create",
        }
    finally:
        shutil.rmtree(prepared.parent, ignore_errors=True)


def publish_clip(
    source: Path,
    *,
    title: str = "",
    description: str = "",
    game: str = "mobile_legends",
    env: dict[str, str] | None = None,
) -> dict:
    env = env or load_env()
    token = vk_token(env)
    group_id = vk_group_id(env)
    assert_upload_token(token, group_id)
    prepared = prepare_clip_source(source)
    try:
        if not title:
            title = f"MLBB {time.strftime('%d.%m %H:%M')}"
        product_line = _product_description_suffix(game)
        base_desc = description or "Mobile Legends"
        if product_line:
            base_desc = f"{base_desc}\n{product_line}"
        save = vk_call(
            "video.save",
            {
                "name": title[:128],
                "description": base_desc[:5000],
                "group_id": group_id,
                "wallpost": 0,
                "is_private": 0,
                "no_comments": 0,
            },
            token,
        )
        upload_url = save.get("upload_url")
        if not upload_url:
            raise RuntimeError("vk_no_upload_url")
        up = upload_video_file(upload_url, prepared)
        return {
            "video_id": save.get("video_id"),
            "owner_id": save.get("owner_id"),
            "upload": up,
            "duration": ffprobe_duration(prepared),
            "prepared_size": prepared.stat().st_size,
        }
    finally:
        shutil.rmtree(prepared.parent, ignore_errors=True)
