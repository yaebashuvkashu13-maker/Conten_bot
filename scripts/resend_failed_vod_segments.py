#!/usr/bin/env python3
"""Resend rendered VOD segments that failed Telegram 20MB inline limit."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_telegram_video import (
    TELEGRAM_DOCUMENT_MAX_BYTES,
    TELEGRAM_MAX_BYTES,
    compress_for_inline_video,
    send_document_file,
    send_video_file,
)
from mlbb_vod_segment_store import inline_keyboard_markup, load_feed_sent, mark_feed_sent, segments_root, upsert_segment
from youtube_download import load_env

ENV_PATH = Path("/root/.video_bot.env")
LOG_PATH = Path("/root/data/mlbb/mlbb_vod_segment_feed.log")


def _failed_ids_from_log(log_path: Path) -> list[str]:
  if not log_path.exists():
    return []
  text = log_path.read_text(encoding="utf-8", errors="replace")
  out: list[str] = []
  seen: set[str] = set()
  for m in re.finditer(r"#([A-Za-z0-9_-]+_\d+)", text):
    sid = m.group(1)
    if sid in seen:
      continue
    chunk = text[m.start() : m.start() + 600]
    if "20MB" in chunk or "не отправился" in chunk:
      seen.add(sid)
      out.append(sid)
  return out


def _deliver_file(path: Path) -> tuple[Path, bool]:
  if path.stat().st_size <= TELEGRAM_MAX_BYTES:
    return path, False
  return compress_for_inline_video(path, max_bytes=TELEGRAM_MAX_BYTES)


def _send_segment(
  token: str,
  chat_id: str,
  path: Path,
  sid: str,
  *,
  caption: str,
) -> bool:
  markup = inline_keyboard_markup(sid)
  deliver, is_temp = _deliver_file(path)
  try:
    if deliver.stat().st_size <= TELEGRAM_MAX_BYTES:
      return send_video_file(token, chat_id, deliver, caption, reply_markup=markup)
    if deliver.stat().st_size <= TELEGRAM_DOCUMENT_MAX_BYTES:
      return send_document_file(
        token,
        chat_id,
        deliver,
        f"{caption}\n📎 файл (догоняющая отправка)",
        reply_markup=markup,
      )
    return False
  finally:
    if is_temp:
      deliver.unlink(missing_ok=True)


def resend_segments(
  segment_ids: list[str],
  *,
  token: str,
  chat_id: str,
  skip_sent: bool = True,
) -> tuple[int, int]:
  root = segments_root()
  already = load_feed_sent() if skip_sent else set()
  sent = 0
  failed = 0

  for sid in segment_ids:
    if skip_sent and sid in already:
      continue
    path = root / f"seg_{sid}.mp4"
    if not path.exists():
      print(f"skip missing {sid}")
      failed += 1
      continue
    vid = sid.rsplit("_", 1)[0]
    mb = path.stat().st_size / (1024 * 1024)
    caption = (
      f"MLBB кусок #{sid} (догон)\n"
      f"{vid} | {mb:.1f} MB на диске\n"
      f"👍 Ок / 👎 Не ок"
    )
    if _send_segment(token, chat_id, path, sid, caption=caption):
      upsert_segment(
        {
          "segment_id": sid,
          "path": str(path),
          "vod_id": vid,
          "start": float(sid.rsplit("_", 1)[-1]),
          "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
      )
      mark_feed_sent([sid])
      sent += 1
      print(f"sent {sid} ({mb:.1f}MB)")
      time.sleep(float(os.environ.get("MLBB_RESEND_DELAY_SEC", "2.5")))
    else:
      failed += 1
      print(f"fail {sid} ({mb:.1f}MB)")
  return sent, failed


def main() -> int:
  parser = argparse.ArgumentParser(description="Resend VOD segments that hit Telegram 20MB limit")
  parser.add_argument("--log", default=str(LOG_PATH))
  parser.add_argument("--ids", nargs="*", help="Explicit segment ids (default: parse log)")
  parser.add_argument("--all-large", action="store_true", help="All seg_*.mp4 >20MB not yet sent")
  parser.add_argument("--dry-run", action="store_true")
  parser.add_argument("--limit", type=int, default=0)
  args = parser.parse_args()

  env = {**os.environ, **load_env(ENV_PATH)}
  token = env.get("TG_BOT_TOKEN", "")
  chat_id = env.get("TG_CHAT_ID", "")
  if not token or not chat_id:
    print("TG_BOT_TOKEN or TG_CHAT_ID missing", file=sys.stderr)
    return 1

  ids: list[str] = list(args.ids or [])
  if not ids:
    ids = _failed_ids_from_log(Path(args.log))
  if args.all_large:
    sent = load_feed_sent()
    for p in sorted(segments_root().glob("seg_*.mp4")):
      sid = p.stem.replace("seg_", "", 1)
      if p.stat().st_size > TELEGRAM_MAX_BYTES and sid not in sent and sid not in ids:
        ids.append(sid)

  if args.limit > 0:
    ids = ids[: args.limit]

  print(f"queue={len(ids)} dry_run={args.dry_run}")
  if args.dry_run:
    for sid in ids:
      p = segments_root() / f"seg_{sid}.mp4"
      sz = p.stat().st_size // 1_000_000 if p.exists() else -1
      print(f"  {sid} {sz}MB")
    return 0

  from mlbb_vod_segment_feed import send_message

  send_message(token, chat_id, f"📤 Догоняю {len(ids)} клипов, которые не ушли из‑за лимита 20MB…")
  sent, failed = resend_segments(ids, token=token, chat_id=chat_id)
  send_message(token, chat_id, f"✅ Догон завершён: отправлено {sent}, ошибок {failed}")
  print(f"done sent={sent} failed={failed}")
  return 0 if failed == 0 else 1


if __name__ == "__main__":
  raise SystemExit(main())
