#!/usr/bin/env python3
"""Queue inbox VODs with savage/maniac titles for dense rescan by segment feed."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_vod_segment_feed import (  # noqa: E402
    INBOX,
    _ffprobe_duration,
    _load_state,
    _registry_entry,
    _save_state,
    _vod_length_ok,
    send_message,
)
from mlbb_vod_segment_store import vod_youtube_id
from mlbb_vod_title import title_min_banner_tier, title_promises_kill_streak, vod_title_blob
from vod_scan_state import invalidate_pool_cache
from youtube_download import load_env

ENV_PATH = Path("/root/.video_bot.env")
log = logging.getLogger("mlbb_title_rescan")


def _match_title(blob: str) -> bool:
    if title_promises_kill_streak(blob):
        return True
    return title_min_banner_tier(blob) >= 2


def queue_inbox_title_rescan(
    inbox: Path | None = None,
    *,
    include_exhausted: bool = True,
) -> list[dict]:
    inbox = inbox or INBOX
    state = _load_state()
    registry: list[dict] = list(state.get("vods", []))
    by_id = {str(r.get("id", "")): r for r in registry if r.get("id")}
    by_path = {str(r.get("path", "")): r for r in registry}

    queued: list[dict] = []
    for vod in sorted(inbox.glob("yt_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
        if not vod.is_file():
            continue
        dur = _ffprobe_duration(vod)
        if not _vod_length_ok(vod, dur):
            continue
        blob = vod_title_blob(vod)
        if not _match_title(blob):
            continue
        vid = vod_youtube_id(vod)
        row = by_id.get(vid) or by_path.get(str(vod))
        if row is None:
            row = _registry_entry(vod, title=blob[:120], exhausted=False)
            registry.append(row)
            by_id[vid] = row
            by_path[str(vod)] = row
        if row.get("exhausted") and not include_exhausted:
            continue
        row["exhausted"] = False
        row["title_rescan_priority"] = True
        row["title"] = blob[:200]
        row["zero_send_sessions"] = 0
        row.pop("reject_reason", None)
        invalidate_pool_cache(row)
        row.pop("last_scan_at", None)
        row.pop("last_pool_at", None)
        queued.append(
            {
                "id": vid,
                "path": str(vod),
                "title": blob[:100],
                "tier_need": title_min_banner_tier(blob),
                "duration_min": round(dur / 60, 1),
            }
        )
        log.info("queued title rescan id=%s tier=%s", vid, title_min_banner_tier(blob))

    state["vods"] = registry
    state["title_rescan_queue"] = [q["id"] for q in queued]
    state["title_rescan_queued_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    scanned = set(state.get("scanned_vods", []))
    for q in queued:
        name = Path(q["path"]).name
        scanned.discard(name)
    state["scanned_vods"] = sorted(scanned)
    _save_state(state)
    return queued


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--inbox", type=Path, default=None)
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--list-only", action="store_true")
    args = parser.parse_args()

    queued = queue_inbox_title_rescan(args.inbox)
    report_path = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb")) / "title_rescan_queue.json"
    report_path.write_text(json.dumps({"queued": queued, "count": len(queued)}, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps({"queued": len(queued), "ids": [q["id"] for q in queued[:20]]}, ensure_ascii=False))

    if args.notify:
        env = load_env(ENV_PATH)
        token = env.get("TG_BOT_TOKEN", "")
        chat_id = env.get("TG_CHAT_ID", "")
        if token and chat_id:
            lines = [f"📋 Title-rescan: {len(queued)} VOD в очереди (dense 1Hz)"]
            for q in queued[:12]:
                lines.append(f"• {q['id']} ({q['duration_min']}м) tier≥{q['tier_need']}")
            if len(queued) > 12:
                lines.append(f"… ещё {len(queued) - 12}")
            send_message(token, chat_id, "\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
