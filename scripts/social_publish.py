#!/usr/bin/env python3
"""Publish approved clips to YouTube Shorts, Instagram Reels, TikTok.

Credentials live in /root/.video_bot.env (never commit). See docs/SOCIAL_PUBLISH_SETUP.md.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ENV_FILE = Path(os.environ.get("ENV_FILE", "/root/.video_bot.env"))
TOKEN_FILE = Path(os.environ.get("YOUTUBE_TOKEN_FILE", "/root/youtube_oauth_token.json"))
PUBLISH_LOG = Path(os.environ.get("SOCIAL_PUBLISH_LOG", "/root/data/mlbb/publish/social_publish_log.jsonl"))

PLATFORMS = ("youtube", "instagram", "tiktok", "vk")
PLATFORM_LABELS = {
    "youtube": "YouTube",
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "vk": "VK",
}
PLATFORM_SHORT = {"youtube": "yt", "instagram": "ig", "tiktok": "tt", "vk": "vk"}
SHORT_TO_PLATFORM = {v: k for k, v in PLATFORM_SHORT.items()}

log = logging.getLogger("social_publish")


def load_env(path: Path | None = None) -> dict[str, str]:
    env: dict[str, str] = {}
    p = path or ENV_FILE
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    for key, value in os.environ.items():
        if value:
            env[key] = value
    return env


def _env(env: dict[str, str] | None) -> dict[str, str]:
    return load_env() if env is None else env


def _truthy(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def platform_enabled(env: dict[str, str], platform: str) -> bool:
    if not _truthy(env.get("SOCIAL_PUBLISH_ENABLED"), default=True):
        return False
    key = {
        "youtube": "SOCIAL_YT_ENABLED",
        "instagram": "SOCIAL_IG_ENABLED",
        "tiktok": "SOCIAL_TT_ENABLED",
        "vk": "SOCIAL_VK_ENABLED",
    }.get(platform, "")
    return _truthy(env.get(key), default=True)


def youtube_configured(env: dict[str, str]) -> tuple[bool, str]:
    client_id = env.get("GOOGLE_OAUTH_CLIENT_ID") or env.get("YOUTUBE_OAUTH_CLIENT_ID") or ""
    client_secret = env.get("GOOGLE_OAUTH_CLIENT_SECRET") or env.get("YOUTUBE_OAUTH_CLIENT_SECRET") or ""
    refresh = env.get("GOOGLE_OAUTH_REFRESH_TOKEN") or ""
    if not refresh and TOKEN_FILE.exists():
        try:
            refresh = str(json.loads(TOKEN_FILE.read_text(encoding="utf-8")).get("refresh_token") or "")
        except Exception:
            refresh = ""
    if not client_id or not client_secret:
        return False, "нет GOOGLE_OAUTH_CLIENT_ID/SECRET"
    if not refresh:
        return False, "нет GOOGLE_OAUTH_REFRESH_TOKEN (запусти youtube_oauth_setup.py)"
    return True, "ok"


def instagram_configured(env: dict[str, str]) -> tuple[bool, str]:
    token = env.get("IG_ACCESS_TOKEN") or env.get("FACEBOOK_PAGE_ACCESS_TOKEN") or ""
    ig_user = env.get("IG_USER_ID") or env.get("INSTAGRAM_BUSINESS_ACCOUNT_ID") or ""
    if not token:
        return False, "нет IG_ACCESS_TOKEN / FACEBOOK_PAGE_ACCESS_TOKEN"
    if not ig_user:
        return False, "нет IG_USER_ID"
    return True, "ok"


def tiktok_configured(env: dict[str, str]) -> tuple[bool, str]:
    token = env.get("TIKTOK_ACCESS_TOKEN") or ""
    if not token:
        return False, "нет TIKTOK_ACCESS_TOKEN"
    return True, "ok"


def vk_configured(env: dict[str, str]) -> tuple[bool, str]:
    token = env.get("VK_MLBB_ACCESS_TOKEN") or env.get("VK_ACCESS_TOKEN_MLBB") or ""
    if not token:
        return False, "нет VK_MLBB_ACCESS_TOKEN"
    return True, "ok"


def status_report(env: dict[str, str] | None = None) -> dict[str, Any]:
    env = _env(env)
    checkers = {
        "youtube": youtube_configured,
        "instagram": instagram_configured,
        "tiktok": tiktok_configured,
        "vk": vk_configured,
    }
    out: dict[str, Any] = {"enabled": _truthy(env.get("SOCIAL_PUBLISH_ENABLED"), default=True), "platforms": {}}
    for name, checker in checkers.items():
        ok, note = checker(env)
        out["platforms"][name] = {
            "configured": ok,
            "enabled": platform_enabled(env, name),
            "ready": ok and platform_enabled(env, name),
            "note": note if not ok else ("выключено" if not platform_enabled(env, name) else "готово"),
        }
    return out


def social_button_row(callback_prefix: str, item_id: str) -> list[dict[str, str]]:
    """One Telegram inline button: open platform picker."""
    sid = item_id.strip()
    return [{"text": "📤 В соцсети", "callback_data": f"{callback_prefix}_social:{sid}"}]


def platforms_keyboard(callback_prefix: str, item_id: str, *, env: dict[str, str] | None = None) -> dict:
    """Inline keyboard to pick YouTube / Instagram / TikTok (/VK)."""
    env = _env(env)
    report = status_report(env)
    sid = item_id.strip()
    row: list[dict[str, str]] = []
    for platform in PLATFORMS:
        info = report["platforms"][platform]
        if not info["enabled"]:
            continue
        short = PLATFORM_SHORT[platform]
        mark = "✅" if info["ready"] else "⚙️"
        row.append(
            {
                "text": f"{mark} {PLATFORM_LABELS[platform]}",
                "callback_data": f"{callback_prefix}_pub:{short}:{sid}",
            }
        )
    rows: list[list[dict[str, str]]] = []
    if row:
        # Telegram prefers ≤8 buttons/row; split into pairs
        for i in range(0, len(row), 2):
            rows.append(row[i : i + 2])
    rows.append([{"text": "« Назад", "callback_data": f"{callback_prefix}_social_back:{sid}"}])
    return {"inline_keyboard": rows}


def append_publish_log(entry: dict[str, Any]) -> None:
    PUBLISH_LOG.parent.mkdir(parents=True, exist_ok=True)
    with PUBLISH_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def resolve_clip_path(
    *,
    kind: str,
    game: str,
    item_id: str,
) -> tuple[Path | None, dict[str, Any]]:
    """Resolve local mp4 for vod_segment / shorts calibration clip."""
    game = (game or "mlbb").strip().lower()
    sid = item_id.strip()
    if sid.startswith("yt_"):
        sid = sid[3:]
    kind = (kind or "vseg").strip().lower()
    meta: dict[str, Any] = {"game": game, "kind": kind, "item_id": sid}

    if kind == "vseg":
        if game == "mlbb":
            from mlbb_vod_segment_store import find_segment

            row = find_segment(sid) or {}
            path = Path(str(row.get("path") or ""))
            if not path.exists():
                path = Path(f"/root/datasets/mlbb/vod_segments/seg_{sid}.mp4")
            meta.update(row)
            return (path if path.exists() else None), meta

        from shooter_vod_segment_store import _paths, find_segment

        row = find_segment(game, sid) or {}
        path = Path(str(row.get("path") or ""))
        if not path.exists():
            path = _paths(game)["segments"] / f"seg_{sid}.mp4"
        meta.update(row)
        return (path if path.exists() else None), meta

    # shorts / calibration
    if game == "mlbb":
        from mlbb_calibration_store import find_candidate

        row = find_candidate(sid) or {}
        path = Path(str(row.get("path") or ""))
        meta.update(row)
        return (path if path.exists() else None), meta

    from game_shorts_calibration import _paths

    path = _paths(game)["shorts"] / f"yt_{sid}.mp4"
    meta["path"] = str(path)
    return (path if path.exists() else None), meta


def default_title(meta: dict[str, Any], path: Path) -> str:
    game = str(meta.get("game") or "clip").upper()
    sid = str(meta.get("item_id") or path.stem)
    custom = str(meta.get("title") or "").strip()
    if custom:
        return custom[:95]
    return f"{game} highlight #{sid}"[:95]


def default_caption(meta: dict[str, Any], path: Path) -> str:
    game = str(meta.get("game") or "").lower()
    tags = {
        "mlbb": "#mlbb #mobilelegends #shorts",
        "pubg": "#pubgmobile #pubg #shorts",
        "standoff": "#standoff2 #shorts",
        "genshin": "#genshinimpact #shorts",
        "wot": "#worldoftanks #wot #shorts",
    }.get(game, "#shorts #gaming")
    title = default_title(meta, path)
    return f"{title}\n{tags}"[:2100]


def _http_json(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 120,
) -> tuple[int, Any, dict[str, str]]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            if not body:
                return resp.status, None, hdrs
            try:
                return resp.status, json.loads(body.decode("utf-8")), hdrs
            except json.JSONDecodeError:
                return resp.status, body.decode("utf-8", errors="replace"), hdrs
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError:
            payload = raw
        return exc.code, payload, {k.lower(): v for k, v in exc.headers.items()}


def youtube_access_token(env: dict[str, str]) -> str:
    client_id = env.get("GOOGLE_OAUTH_CLIENT_ID") or env.get("YOUTUBE_OAUTH_CLIENT_ID") or ""
    client_secret = env.get("GOOGLE_OAUTH_CLIENT_SECRET") or env.get("YOUTUBE_OAUTH_CLIENT_SECRET") or ""
    refresh = env.get("GOOGLE_OAUTH_REFRESH_TOKEN") or ""
    if not refresh and TOKEN_FILE.exists():
        refresh = str(json.loads(TOKEN_FILE.read_text(encoding="utf-8")).get("refresh_token") or "")
    form = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        }
    ).encode()
    status, payload, _ = _http_json(
        "https://oauth2.googleapis.com/token",
        method="POST",
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=60,
    )
    if status >= 400 or not isinstance(payload, dict) or not payload.get("access_token"):
        raise RuntimeError(f"YouTube token refresh failed: {payload}")
    return str(payload["access_token"])


def upload_youtube(
    path: Path,
    *,
    title: str,
    description: str,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    env = _env(env)
    ok, note = youtube_configured(env)
    if not ok:
        raise RuntimeError(note)
    token = youtube_access_token(env)
    privacy = (env.get("SOCIAL_YT_PRIVACY") or "public").strip().lower()
    if privacy not in ("public", "unlisted", "private"):
        privacy = "public"
    category = (env.get("SOCIAL_YT_CATEGORY_ID") or "20").strip()  # Gaming
    meta = {
        "snippet": {
            "title": title[:100],
            "description": description[:4900],
            "categoryId": category,
            "tags": [t for t in (env.get("SOCIAL_YT_TAGS") or "shorts,gaming").split(",") if t.strip()],
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    init_url = (
        "https://www.googleapis.com/upload/youtube/v3/videos"
        "?uploadType=resumable&part=snippet,status"
    )
    status, payload, hdrs = _http_json(
        init_url,
        method="POST",
        data=json.dumps(meta).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(path.stat().st_size),
        },
        timeout=60,
    )
    upload_url = hdrs.get("location")
    if not upload_url or status >= 400:
        raise RuntimeError(f"YouTube init upload failed ({status}): {payload}")

    body = path.read_bytes()
    status2, payload2, _ = _http_json(
        upload_url,
        method="PUT",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "video/mp4",
            "Content-Length": str(len(body)),
        },
        timeout=max(300, int(path.stat().st_size / (256 * 1024)) + 120),
    )
    if status2 >= 400 or not isinstance(payload2, dict):
        raise RuntimeError(f"YouTube upload failed ({status2}): {payload2}")
    vid = str(payload2.get("id") or "")
    return {
        "platform": "youtube",
        "id": vid,
        "url": f"https://youtu.be/{vid}" if vid else "",
        "privacy": privacy,
        "raw": payload2,
    }


def _ig_api_version(env: dict[str, str]) -> str:
    return (env.get("IG_GRAPH_API_VERSION") or "v21.0").strip()


def upload_instagram_reel(
    path: Path,
    *,
    caption: str,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    env = _env(env)
    ok, note = instagram_configured(env)
    if not ok:
        raise RuntimeError(note)
    token = env.get("IG_ACCESS_TOKEN") or env.get("FACEBOOK_PAGE_ACCESS_TOKEN") or ""
    ig_user = env.get("IG_USER_ID") or env.get("INSTAGRAM_BUSINESS_ACCOUNT_ID") or ""
    ver = _ig_api_version(env)
    public_base = (env.get("SOCIAL_PUBLIC_VIDEO_BASE") or env.get("IG_PUBLIC_VIDEO_BASE") or "").rstrip("/")

    if public_base:
        # Expect file already reachable as {base}/{filename} or copy note for ops.
        video_url = f"{public_base}/{path.name}"
        create_url = f"https://graph.facebook.com/{ver}/{ig_user}/media"
        form = urllib.parse.urlencode(
            {
                "media_type": "REELS",
                "video_url": video_url,
                "caption": caption[:2200],
                "share_to_feed": "true",
                "access_token": token,
            }
        ).encode()
        status, payload, _ = _http_json(
            create_url,
            method="POST",
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=120,
        )
        if status >= 400 or not isinstance(payload, dict) or not payload.get("id"):
            raise RuntimeError(f"IG create container failed ({status}): {payload}")
        creation_id = str(payload["id"])
    else:
        # Resumable binary upload (no public URL required).
        create_url = f"https://graph.facebook.com/{ver}/{ig_user}/media"
        form = urllib.parse.urlencode(
            {
                "media_type": "REELS",
                "upload_type": "resumable",
                "caption": caption[:2200],
                "share_to_feed": "true",
                "access_token": token,
            }
        ).encode()
        status, payload, _ = _http_json(
            create_url,
            method="POST",
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=120,
        )
        if status >= 400 or not isinstance(payload, dict):
            raise RuntimeError(
                f"IG resumable init failed ({status}): {payload}. "
                "Либо настрой SOCIAL_PUBLIC_VIDEO_BASE, либо проверь права token."
            )
        creation_id = str(payload.get("id") or "")
        upload_uri = str((payload.get("uri") or payload.get("upload_url") or "")).strip()
        if not creation_id:
            raise RuntimeError(f"IG resumable init missing id: {payload}")
        if upload_uri:
            data = path.read_bytes()
            status_u, payload_u, _ = _http_json(
                upload_uri,
                method="POST",
                data=data,
                headers={
                    "Authorization": f"OAuth {token}",
                    "offset": "0",
                    "file_size": str(len(data)),
                    "Content-Type": mimetypes.guess_type(str(path))[0] or "video/mp4",
                },
                timeout=max(300, int(path.stat().st_size / (256 * 1024)) + 120),
            )
            if status_u >= 400:
                raise RuntimeError(f"IG binary upload failed ({status_u}): {payload_u}")

    # Wait until container is finished
    deadline = time.time() + int(env.get("SOCIAL_IG_WAIT_SEC", "300"))
    while time.time() < deadline:
        st_url = (
            f"https://graph.facebook.com/{ver}/{creation_id}"
            f"?fields=status_code,status&access_token={urllib.parse.quote(token)}"
        )
        st_code, st_payload, _ = _http_json(st_url, timeout=60)
        if st_code >= 400 or not isinstance(st_payload, dict):
            raise RuntimeError(f"IG status poll failed ({st_code}): {st_payload}")
        code = str(st_payload.get("status_code") or "").upper()
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"IG container error: {st_payload}")
        time.sleep(5)
    else:
        raise RuntimeError(f"IG container not ready in time: id={creation_id}")

    pub_url = f"https://graph.facebook.com/{ver}/{ig_user}/media_publish"
    form_pub = urllib.parse.urlencode(
        {"creation_id": creation_id, "access_token": token}
    ).encode()
    p_status, p_payload, _ = _http_json(
        pub_url,
        method="POST",
        data=form_pub,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=120,
    )
    if p_status >= 400 or not isinstance(p_payload, dict) or not p_payload.get("id"):
        raise RuntimeError(f"IG publish failed ({p_status}): {p_payload}")
    media_id = str(p_payload["id"])
    return {
        "platform": "instagram",
        "id": media_id,
        "url": f"https://www.instagram.com/reel/{media_id}/",
        "creation_id": creation_id,
        "raw": p_payload,
    }


def upload_tiktok(
    path: Path,
    *,
    caption: str,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    env = _env(env)
    ok, note = tiktok_configured(env)
    if not ok:
        raise RuntimeError(note)
    token = env["TIKTOK_ACCESS_TOKEN"]
    privacy = (env.get("SOCIAL_TT_PRIVACY") or "PUBLIC_TO_EVERYONE").strip()
    size = path.stat().st_size
    init_body = {
        "post_info": {
            "title": caption[:150],
            "privacy_level": privacy,
            "disable_duet": False,
            "disable_comment": False,
            "disable_stitch": False,
            "video_cover_timestamp_ms": 1000,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": size,
            "chunk_size": size,
            "total_chunk_count": 1,
        },
    }
    status, payload, _ = _http_json(
        "https://open.tiktokapis.com/v2/post/publish/video/init/",
        method="POST",
        data=json.dumps(init_body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        timeout=60,
    )
    if status >= 400 or not isinstance(payload, dict):
        raise RuntimeError(f"TikTok init failed ({status}): {payload}")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    upload_url = str(data.get("upload_url") or "")
    publish_id = str(data.get("publish_id") or "")
    if not upload_url:
        raise RuntimeError(f"TikTok init missing upload_url: {payload}")

    video = path.read_bytes()
    req = urllib.request.Request(
        upload_url,
        data=video,
        method="PUT",
        headers={
            "Content-Type": "video/mp4",
            "Content-Length": str(len(video)),
            "Content-Range": f"bytes 0-{len(video) - 1}/{len(video)}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=max(300, size // (256 * 1024) + 120)) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"TikTok upload failed: {exc.read().decode('utf-8', errors='replace')}") from exc

    return {
        "platform": "tiktok",
        "id": publish_id,
        "url": "",
        "privacy": privacy,
        "raw": payload,
    }


def upload_vk(
    path: Path,
    *,
    title: str,
    env: dict[str, str] | None = None,
    game: str = "mobile_legends",
) -> dict[str, Any]:
    env = _env(env)
    ok, note = vk_configured(env)
    if not ok:
        raise RuntimeError(note)
    from vk_mlbb_upload import publish_clip as vk_publish_clip

    result = vk_publish_clip(path, title=title, game=game, env=env)
    video_id = result.get("video_id")
    owner_id = result.get("owner_id")
    url = f"https://vk.com/video{owner_id}_{video_id}" if video_id and owner_id is not None else ""
    return {"platform": "vk", "id": str(video_id or ""), "url": url, "raw": result}


def publish_clip(
    platform: str,
    path: Path,
    *,
    meta: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    env = _env(env)
    meta = meta or {}
    platform = SHORT_TO_PLATFORM.get(platform, platform).strip().lower()
    if platform not in PLATFORMS:
        raise RuntimeError(f"unknown platform: {platform}")
    if not platform_enabled(env, platform):
        raise RuntimeError(f"{platform} выключен (SOCIAL_*_ENABLED=0)")
    if not path.exists():
        raise RuntimeError(f"файл не найден: {path}")

    title = default_title(meta, path)
    caption = default_caption(meta, path)
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        if platform == "youtube":
            result = upload_youtube(path, title=title, description=caption, env=env)
        elif platform == "instagram":
            result = upload_instagram_reel(path, caption=caption, env=env)
        elif platform == "tiktok":
            result = upload_tiktok(path, caption=caption, env=env)
        else:
            game_name = str(meta.get("game") or "mlbb")
            vk_game = {
                "mlbb": "mobile_legends",
                "pubg": "pubg",
                "standoff": "standoff",
                "genshin": "genshin",
                "wot": "wot",
            }.get(game_name, game_name)
            result = upload_vk(path, title=title, env=env, game=vk_game)
        entry = {
            "at": started,
            "ok": True,
            "platform": platform,
            "path": str(path),
            "meta": {k: meta.get(k) for k in ("game", "kind", "item_id", "vod_id", "start", "peak_start")},
            "result": {k: result.get(k) for k in ("id", "url", "privacy", "creation_id") if k in result},
        }
        append_publish_log(entry)
        return {"ok": True, **result}
    except Exception as exc:
        append_publish_log(
            {
                "at": started,
                "ok": False,
                "platform": platform,
                "path": str(path),
                "meta": {k: meta.get(k) for k in ("game", "kind", "item_id")},
                "error": str(exc)[:500],
            }
        )
        raise


def format_status_message(env: dict[str, str] | None = None) -> str:
    report = status_report(env)
    lines = ["📤 Публикация в соцсети", ""]
    if not report["enabled"]:
        lines.append("SOCIAL_PUBLISH_ENABLED=0 — всё выключено.")
        return "\n".join(lines)
    for name in PLATFORMS:
        info = report["platforms"][name]
        icon = "✅" if info["ready"] else ("⏸" if not info["enabled"] else "⚙️")
        lines.append(f"{icon} {PLATFORM_LABELS[name]}: {info['note']}")
    lines.append("")
    lines.append("Инструкция: docs/SOCIAL_PUBLISH_SETUP.md")
    return "\n".join(lines)


def main() -> int:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Social publish helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Show credential readiness")

    p_up = sub.add_parser("upload", help="Upload one local file")
    p_up.add_argument("--platform", required=True, choices=list(PLATFORMS) + list(PLATFORM_SHORT.values()))
    p_up.add_argument("--path", type=Path, required=True)
    p_up.add_argument("--game", default="mlbb")
    p_up.add_argument("--title", default="")

    args = parser.parse_args()
    env = load_env()
    if args.cmd == "status":
        print(format_status_message(env))
        print(json.dumps(status_report(env), ensure_ascii=False, indent=2))
        return 0

    meta = {"game": args.game, "title": args.title, "item_id": args.path.stem}
    result = publish_clip(args.platform, args.path, meta=meta, env=env)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
