#!/usr/bin/env python3
"""Build MLBB kill-banner reference bank from wiki assets and labeled VOD crops."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

# Curated from https://mobile-legends.fandom.com/wiki/Battle_effects (Notifications section).
WIKI_NOTIFICATIONS: list[tuple[str, str]] = [
    ("classic", "https://static.wikia.nocookie.net/mobile-legends/images/8/85/Classic_Notification.png/revision/latest"),
    ("shinto_shrine", "https://static.wikia.nocookie.net/mobile-legends/images/5/54/Shinto_Shrine_Notification.png/revision/latest"),
    ("crystal", "https://static.wikia.nocookie.net/mobile-legends/images/9/9e/Crystal_Notification.png/revision/latest"),
    ("starlight", "https://static.wikia.nocookie.net/mobile-legends/images/c/cb/Starlight_Notification.png/revision/latest"),
    ("summer_gala", "https://static.wikia.nocookie.net/mobile-legends/images/c/c6/Summer_Gala_Notification.png/revision/latest"),
    ("lightborn_declaration", "https://static.wikia.nocookie.net/mobile-legends/images/5/53/Lightborn%27s_Declaration_Notification.png/revision/latest"),
    ("blazing_west", "https://static.wikia.nocookie.net/mobile-legends/images/a/ae/Blazing_West_Killing_Notification.png/revision/latest"),
    ("star_wars", "https://static.wikia.nocookie.net/mobile-legends/images/9/94/MLBB_x_Star_Wars_Killing_Notification.png/revision/latest"),
    ("transformers", "https://static.wikia.nocookie.net/mobile-legends/images/3/37/One_shall_stand%2C_one_shall_fall._Killing_Notification.png/revision/latest"),
    ("twilight_orb", "https://static.wikia.nocookie.net/mobile-legends/images/e/e5/Twilight_Orb_Killing_Notification.png/revision/latest"),
    ("m3_glorious", "https://static.wikia.nocookie.net/mobile-legends/images/0/0d/M3_Glorious_Notification.png/revision/latest"),
    ("showdown", "https://static.wikia.nocookie.net/mobile-legends/images/c/c8/Showdown_Killing_Notification.png/revision/latest"),
    ("say_cheese", "https://static.wikia.nocookie.net/mobile-legends/images/3/38/Notification_Say_Cheese%21_Killing_Notification.png/revision/latest"),
    ("jujutsu_kaisen", "https://static.wikia.nocookie.net/mobile-legends/images/4/4f/Jujutsu_Kaisen_Killing_Notification.png/revision/latest"),
    ("attack_on_titan", "https://static.wikia.nocookie.net/mobile-legends/images/4/4e/Attack_on_Titan_Killing_Notification.png/revision/latest"),
    ("hunter_x_hunter", "https://static.wikia.nocookie.net/mobile-legends/images/1/1a/HUNTER%C3%97HUNTER_Killing_Notification.png/revision/latest"),
]

_TIER_FROM_NOTE = {
    "double": "double",
    "triple": "triple",
    "maniac": "maniac",
    "savage": "savage",
    "legendary": "savage",
}


def repo_root() -> Path:
    env = os.environ.get("CONTENT_BOT_REPO", "").strip()
    if env:
        return Path(env)
    root = Path(__file__).resolve().parent.parent
    if (root / "data").exists():
        return root
    return Path("/root/content_bot_ml")


def banner_ref_root() -> Path:
    root = Path(os.environ.get("MLBB_BANNER_REF_ROOT", str(repo_root() / "data" / "mlbb_kill_banners")))
    root.mkdir(parents=True, exist_ok=True)
    return root


def manifest_path() -> Path:
    return banner_ref_root() / "manifest.json"


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "ref"


def _download_url(url: str, dest: Path, *, timeout: float = 30.0) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "content-bot-mlbb-banner-ref/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return False
    if len(data) < 200:
        return False
    dest.write_bytes(data)
    return True


def download_wiki_notifications(*, force: bool = False) -> list[dict]:
    """Download kill-notification frame previews from MLBB Fandom wiki."""
    out_dir = banner_ref_root() / "wiki"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for name, url in WIKI_NOTIFICATIONS:
        dest = out_dir / f"{_slug(name)}.png"
        ok = False
        if force or not dest.exists():
            ok = _download_url(url, dest)
        else:
            ok = dest.exists()
        rows.append(
            {
                "name": name,
                "source": "wiki",
                "url": url,
                "path": str(dest),
                "ok": ok and dest.exists(),
                "bytes": dest.stat().st_size if dest.exists() else 0,
            }
        )
    return rows


def _tier_from_note(note: str) -> str:
    blob = str(note or "").lower()
    for key, tier in _TIER_FROM_NOTE.items():
        if key in blob:
            return tier
    return "unknown"


def _owner_kill_marks() -> list[tuple[str, float, str]]:
    """Return (video_id, sec, tier) from owner labels + banner_miss_diag marks."""
    marks: list[tuple[str, float, str]] = []
    labels_path = Path(
        os.environ.get("MLBB_OWNER_LABELS_PATH", str(repo_root() / "data" / "mobile_legends_owner_labels.json"))
    )
    if labels_path.exists():
        try:
            data = json.loads(labels_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        for vid, rows in (data.get("videos") or {}).items():
            for row in rows:
                if str(row.get("label", "")).lower() != "good":
                    continue
                note = str(row.get("note", ""))
                if "kill" not in note.lower() and not any(k in note.lower() for k in _TIER_FROM_NOTE):
                    continue
                marks.append((vid, float(row["time_sec"]), _tier_from_note(note)))

    try:
        from banner_miss_diag import OWNER_MARKS
    except ImportError:
        OWNER_MARKS = {}
    for vid, secs in OWNER_MARKS.items():
        for sec in secs:
            marks.append((vid, float(sec), "unknown"))
    return marks


def _resolve_vod(video_id: str, vod_root: Path) -> Path | None:
    patterns = [
        vod_root / f"yt_{video_id}.mp4",
        vod_root / f"{video_id}.mp4",
        vod_root / video_id / "video.mp4",
    ]
    for path in patterns:
        if path.exists() and path.stat().st_size > 50_000:
            return path
    for path in vod_root.rglob(f"*{video_id}*.mp4"):
        if path.is_file() and path.stat().st_size > 50_000:
            return path
    return None


def extract_banner_crop(frame) -> object | None:
    """Top-center HUD zone where kill banners appear in gameplay."""
    import cv2

    if frame is None:
        return None
    h, w = frame.shape[:2]
    if h < 80 or w < 160:
        return None
    y0, y1 = int(h * 0.02), int(h * 0.30)
    x0, x1 = int(w * 0.15), int(w * 0.85)
    patch = frame[y0:y1, x0:x1]
    if patch.size == 0:
        return None
    return cv2.resize(patch, (160, 48))


def crop_from_vod(vod: Path, sec: float, *, tier: str = "unknown", video_id: str = "") -> Path | None:
    from gameplay_gate import _read_frame_at
    from mlbb_kill_banner import _read_frame

    best_patch = None
    best_color = -1.0
    from mlbb_kill_banner import _announce_color_score

    for off in (-0.35, 0.0, 0.35, 0.7):
        frame = _read_frame(vod, float(sec) + off)
        if frame is None:
            continue
        color = _announce_color_score(frame)
        patch = extract_banner_crop(frame)
        if patch is not None and color >= best_color:
            best_color = color
            best_patch = patch
    if best_patch is None:
        return None

    import cv2

    tier_dir = banner_ref_root() / "vod_crops" / _slug(tier)
    tier_dir.mkdir(parents=True, exist_ok=True)
    vid = _slug(video_id or vod.stem)
    dest = tier_dir / f"{vid}_{int(sec)}s.png"
    cv2.imwrite(str(dest), best_patch)
    return dest


def extract_from_owner_labels(
    vod_root: Path | None = None,
    *,
    force: bool = False,
) -> list[dict]:
    """Crop banner patches from owner-confirmed kill timestamps on local VODs."""
    if vod_root is None:
        vod_root = Path(os.environ.get("MLBB_VOD_INBOX", "/root/data/mlbb/youtube_nightly/inbox"))
    rows: list[dict] = []
    for video_id, sec, tier in _owner_kill_marks():
        vod = _resolve_vod(video_id, vod_root)
        row = {
            "video_id": video_id,
            "sec": sec,
            "tier": tier,
            "vod": str(vod) if vod else "",
            "ok": False,
            "path": "",
        }
        if vod is None:
            rows.append(row)
            continue
        dest = banner_ref_root() / "vod_crops" / _slug(tier) / f"{_slug(video_id)}_{int(sec)}s.png"
        if dest.exists() and not force:
            row["ok"] = True
            row["path"] = str(dest)
            rows.append(row)
            continue
        saved = crop_from_vod(vod, sec, tier=tier, video_id=video_id)
        row["ok"] = saved is not None
        row["path"] = str(saved) if saved else ""
        rows.append(row)
    return rows


def write_manifest(*, wiki_rows: list[dict] | None = None, vod_rows: list[dict] | None = None) -> dict:
    root = banner_ref_root()
    wiki_rows = wiki_rows if wiki_rows is not None else []
    vod_rows = vod_rows if vod_rows is not None else []
    refs: list[dict] = []
    for row in wiki_rows:
        if row.get("ok") and row.get("path"):
            p = Path(str(row["path"]))
            rel = p.relative_to(root) if p.is_absolute() and str(p).startswith(str(root)) else p.name
            refs.append(
                {
                    "name": row["name"],
                    "source": "wiki",
                    "path": str(rel),
                    "tier_hint": "unknown",
                }
            )
    for path in sorted(root.glob("vod_crops/**/*.png")):
        tier = path.parent.name
        rel = path.relative_to(root)
        refs.append(
            {
                "name": path.stem,
                "source": "vod_crop",
                "path": str(rel),
                "tier_hint": tier,
            }
        )
    payload = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "root": str(root),
        "count": len(refs),
        "refs": refs,
        "wiki_download": wiki_rows,
        "vod_extract": vod_rows,
    }
    manifest_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def count_refs() -> dict[str, int]:
    root = banner_ref_root()
    wiki = len(list((root / "wiki").glob("*.png"))) if (root / "wiki").exists() else 0
    vod = len(list((root / "vod_crops").rglob("*.png"))) if (root / "vod_crops").exists() else 0
    return {"wiki": wiki, "vod_crops": vod, "total": wiki + vod}


def main() -> int:
    parser = argparse.ArgumentParser(description="Build MLBB kill-banner reference bank")
    parser.add_argument("--wiki", action="store_true", help="Download wiki notification frames")
    parser.add_argument("--from-labels", action="store_true", help="Crop banners from owner-labeled VODs")
    parser.add_argument("--vod-root", default="", help="VOD inbox/cache root")
    parser.add_argument("--vod", default="", help="Single VOD path for manual crop")
    parser.add_argument("--sec", type=float, default=0.0, help="Timestamp for --vod crop")
    parser.add_argument("--tier", default="unknown", help="Tier folder for --vod crop")
    parser.add_argument("--force", action="store_true", help="Re-download / re-crop")
    parser.add_argument("--all", action="store_true", help="Wiki + owner labels")
    args = parser.parse_args()

    do_wiki = args.wiki or args.all
    do_labels = args.from_labels or args.all
    if not (do_wiki or do_labels or args.vod):
        do_wiki = True

    wiki_rows: list[dict] = []
    vod_rows: list[dict] = []
    if do_wiki:
        wiki_rows = download_wiki_notifications(force=args.force)
    if do_labels:
        vod_root = Path(args.vod_root) if args.vod_root else None
        vod_rows = extract_from_owner_labels(vod_root, force=args.force)
    if args.vod:
        saved = crop_from_vod(Path(args.vod), args.sec, tier=args.tier, video_id=Path(args.vod).stem)
        vod_rows.append({"vod": args.vod, "sec": args.sec, "ok": saved is not None, "path": str(saved) if saved else ""})

    manifest = write_manifest(wiki_rows=wiki_rows, vod_rows=vod_rows)
    counts = count_refs()
    print(json.dumps({"counts": counts, "manifest": str(manifest_path()), "refs": manifest["count"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
