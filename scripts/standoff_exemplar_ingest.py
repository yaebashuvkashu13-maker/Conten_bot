#!/usr/bin/env python3
"""Save Standoff 2 highlight exemplars for CLIP scoring."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from pathlib import Path

UPLOAD_ROOT = Path("/root/telegram_uploads")
PENDING_ROOT = UPLOAD_ROOT / "pending"
ARCHIVE_ROOT = UPLOAD_ROOT / "archive"


def repo_root() -> Path:
    env = os.environ.get("CONTENT_BOT_REPO", "").strip()
    if env:
        return Path(env)
    local = Path(__file__).resolve().parent.parent
    if (local / "data").exists():
        return local
    return Path("/root/content_bot_ml")


def standoff_exemplar_dir() -> Path:
    root = Path(
        os.environ.get(
            "HIGHLIGHT_EXEMPLAR_ROOT",
            str(repo_root() / "data" / "highlight_exemplars"),
        )
    )
    path = root / "standoff" / "good"
    path.mkdir(parents=True, exist_ok=True)
    return path


def count_standoff_exemplars() -> int:
    folder = standoff_exemplar_dir()
    exts = {".mp4", ".jpg", ".jpeg", ".png", ".webp"}
    return sum(1 for path in folder.iterdir() if path.suffix.lower() in exts)


def _write_meta(dest: Path, *, chat_id: str, label: str, source: Path) -> None:
    meta = dest.with_name(f"{dest.name}.meta.json")
    payload = {
        "chat_id": chat_id,
        "label": label,
        "source": str(source),
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "game": "standoff",
    }
    meta.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def save_standoff_exemplar_video(
    source: Path,
    *,
    chat_id: str = "",
    label: str = "",
) -> Path:
    """Copy a short gameplay clip into highlight_exemplars/standoff/good/."""
    if not source.exists():
        raise FileNotFoundError(source)
    dest_dir = standoff_exemplar_dir()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    unique = source.stem[-16:]
    name = f"so2_{chat_id or 'owner'}_{stamp}_{unique}{source.suffix.lower() or '.mp4'}"
    dest = dest_dir / name
    shutil.copy2(source, dest)
    _write_meta(dest, chat_id=chat_id, label=label, source=source)
    return dest


def find_recent_owner_videos(chat_id: str, *, limit: int = 9) -> list[Path]:
    """Newest mp4 from owner pending queue and archive."""
    candidates: list[Path] = []
    pending = PENDING_ROOT / chat_id
    if pending.exists():
        candidates.extend(p for p in pending.glob("*.mp4") if p.is_file())
    archive = ARCHIVE_ROOT / chat_id
    if archive.exists():
        candidates.extend(p for p in archive.rglob("*.mp4") if p.is_file())
    if not candidates and UPLOAD_ROOT.exists():
        candidates.extend(p for p in UPLOAD_ROOT.rglob("*.mp4") if p.is_file())

    candidates = [p for p in candidates if p.stat().st_size >= 80_000]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    seen: set[str] = set()
    picked: list[Path] = []
    for path in candidates:
        key = path.name
        if key in seen:
            continue
        seen.add(key)
        picked.append(path)
        if len(picked) >= limit:
            break
    return picked


def _already_imported_source(dest_dir: Path, source: Path) -> bool:
    resolved = str(source.resolve())
    for meta in dest_dir.glob("*.meta.json"):
        try:
            row = json.loads(meta.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if row.get("source") == resolved:
            return True
    return False


def import_recent_standoff_exemplars(
    chat_id: str,
    *,
    limit: int = 9,
) -> tuple[list[Path], list[Path]]:
    """Import last N owner uploads as standoff/good exemplars."""
    saved: list[Path] = []
    skipped: list[Path] = []
    dest_dir = standoff_exemplar_dir()
    for source in find_recent_owner_videos(chat_id, limit=limit):
        if _already_imported_source(dest_dir, source):
            skipped.append(source)
            continue
        try:
            dest = save_standoff_exemplar_video(source, chat_id=chat_id, label="import_recent")
            saved.append(dest)
        except OSError:
            skipped.append(source)
    return saved, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description="Import Standoff exemplars from telegram uploads")
    parser.add_argument("--chat-id", default=os.environ.get("TG_CHAT_ID", ""))
    parser.add_argument("--limit", type=int, default=9)
    parser.add_argument("--source", type=Path, action="append", default=[])
    args = parser.parse_args()

    if args.source:
        saved = [
            save_standoff_exemplar_video(Path(src), chat_id=args.chat_id or "cli", label="manual")
            for src in args.source
        ]
        print(f"OK saved={len(saved)} total={count_standoff_exemplars()} dir={standoff_exemplar_dir()}")
        return 0

    if not args.chat_id:
        print("REFUSED: need --chat-id or TG_CHAT_ID")
        return 1

    saved, skipped = import_recent_standoff_exemplars(args.chat_id, limit=args.limit)
    print(
        f"OK standoff exemplars saved={len(saved)} skipped={len(skipped)} "
        f"total={count_standoff_exemplars()} dir={standoff_exemplar_dir()}"
    )
    for path in saved:
        print(f"  + {path.name}")
    return 0 if saved else 1


if __name__ == "__main__":
    raise SystemExit(main())
