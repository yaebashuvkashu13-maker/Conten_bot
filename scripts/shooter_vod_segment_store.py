#!/usr/bin/env python3
"""Per-game VOD segment store (PUBG, Standoff, Genshin, WoT)."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path


def _ensure_scripts_on_path() -> None:
    import sys

    repo = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml"))
    for candidate in (Path(__file__).resolve().parent, repo / "scripts"):
        path = str(candidate)
        if path not in sys.path:
            sys.path.insert(0, path)


def _game_root(game: str) -> Path:
    g = game.strip().lower()
    base = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb")).parent
    return Path(
        os.environ.get(
            f"VOD_{g.upper()}_DATA_ROOT",
            os.environ.get(f"SHOOTER_{g.upper()}_DATA_ROOT", str(base / g)),
        )
    )


def _paths(game: str) -> dict[str, Path]:
    root = _game_root(game)
    return {
        "state": root / "vod_segment_state.json",
        "index": root / "vod_segment_index.json",
        "labels": root / "vod_segment_labels.json",
        "feed_sent": root / "vod_segment_feed_sent.json",
        "inbox": root / "youtube_nightly" / "inbox",
        "segments": Path(
            os.environ.get(
                f"SHOOTER_{game.upper()}_SEGMENTS_ROOT",
                str(Path("/root/datasets") / game / "vod_segments"),
            )
        ),
    }


def vod_youtube_id(path: Path) -> str:
    stem = path.stem
    if stem.startswith("yt_"):
        return stem[3:][:11]
    if stem.startswith("tw_"):
        return stem[3:]
    return stem[:11]


def segment_id(vod_id: str, start_sec: float) -> str:
    return f"{vod_id}_{int(start_sec)}"


def load_index(game: str) -> dict:
    p = _paths(game)["index"]
    if not p.exists():
        return {"segments": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"segments": []}


def load_labels(game: str) -> dict:
    p = _paths(game)["labels"]
    if not p.exists():
        return {"good": [], "bad": [], "feedback": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"good": [], "bad": [], "feedback": []}


def labeled_ids(game: str) -> set[str]:
    data = load_labels(game)
    out: set[str] = set()
    for bucket in ("good", "bad"):
        for row in data.get(bucket, []):
            sid = str(row.get("segment_id", ""))
            if sid:
                out.add(sid)
    for row in data.get("feedback", []):
        sid = str(row.get("segment_id", ""))
        if sid:
            out.add(sid)
    return out


def load_feed_sent(game: str) -> set[str]:
    p = _paths(game)["feed_sent"]
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {str(x) for x in data.get("sent", [])}
    except (json.JSONDecodeError, OSError):
        return set()


def mark_feed_sent(game: str, segment_ids: list[str]) -> None:
    p = _paths(game)["feed_sent"]
    sent = load_feed_sent(game)
    sent.update(segment_ids)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"sent": sorted(sent)}, indent=2), encoding="utf-8")


def upsert_segment(game: str, row: dict) -> None:
    p = _paths(game)["index"]
    data = load_index(game)
    segments = data.setdefault("segments", [])
    sid = str(row.get("segment_id", ""))
    segments[:] = [s for s in segments if str(s.get("segment_id")) != sid]
    segments.append(row)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def save_labels(game: str, data: dict) -> None:
    p = _paths(game)["labels"]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def find_segment(game: str, segment_id_str: str) -> dict | None:
    sid = segment_id_str.strip()
    for row in load_index(game).get("segments", []):
        if row.get("segment_id") == sid:
            return row
    direct = _paths(game)["segments"] / f"seg_{sid}.mp4"
    if direct.exists():
        return {"segment_id": sid, "path": str(direct), "start": 0, "score": 0}
    return None


def apply_owner_label(
    game: str,
    segment_id_str: str,
    *,
    is_good: bool,
    reason: str = "",
    by_chat: str = "",
) -> tuple[bool, str]:
    row = find_segment(game, segment_id_str)
    if not row:
        return False, f"unknown_segment:{segment_id_str}"
    path = Path(row.get("path", ""))
    if not path.exists():
        path = _paths(game)["segments"] / f"seg_{segment_id_str}.mp4"
    if not path.exists():
        return False, f"file_missing:{segment_id_str}"

    labels = load_labels(game)
    entry = {
        "segment_id": segment_id_str,
        "path": str(path),
        "vod": row.get("vod", ""),
        "start": row.get("start", 0),
        "score": row.get("score", 0),
        "reason": reason,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "by_chat": by_chat,
        "source": "vod_segment",
        "game": game.strip().lower(),
    }
    feedback = {**entry, "owner_label": "yes" if is_good else "no"}

    labels["feedback"] = [f for f in labels.get("feedback", []) if f.get("segment_id") != segment_id_str]
    labels["feedback"].append(feedback)

    peak = row.get("peak_start")
    if peak is None:
        peak = row.get("start", 0)
    entry["peak_start"] = peak

    if is_good:
        labels["good"] = [g for g in labels.get("good", []) if g.get("segment_id") != segment_id_str]
        labels["bad"] = [b for b in labels.get("bad", []) if b.get("segment_id") != segment_id_str]
        labels["good"].append(entry)
    else:
        labels["bad"] = [b for b in labels.get("bad", []) if b.get("segment_id") != segment_id_str]
        labels["good"] = [g for g in labels.get("good", []) if g.get("segment_id") != segment_id_str]
        labels["bad"].append(entry)

    label_name = "good" if is_good else "bad"
    _ensure_scripts_on_path()
    from daily_game_cycle import profile_for_game
    from vod_owner_learning import (
        append_owner_time_label,
        clear_learning_cache,
        copy_vod_exemplar,
        peak_time_sec,
        vod_id_from_row,
    )

    profile = profile_for_game(game)
    exemplar = copy_vod_exemplar(profile, path, label_name, segment_id_str)
    if exemplar:
        entry["exemplar"] = str(exemplar)

    save_labels(game, labels)

    vid = vod_id_from_row(row, segment_id_str)
    append_owner_time_label(
        profile,
        vid,
        peak_time_sec({**row, "peak_start": peak}, segment_id_str),
        label_name,
        note=reason,
        source="vod_segment",
    )
    clear_learning_cache()
    return True, label_name


def stats(game: str) -> dict:
    data = load_labels(game)
    yes = sum(1 for f in data.get("feedback", []) if f.get("owner_label") in ("yes", "good"))
    no = sum(1 for f in data.get("feedback", []) if f.get("owner_label") in ("no", "bad"))
    return {"feedback_yes": yes, "feedback_no": no, "game": game}


def _callback_prefix(game: str) -> str:
    return f"{game.strip().lower()}_vseg"


def inline_keyboard_markup(game: str, segment_id_str: str) -> dict:
    sid = segment_id_str.strip()
    prefix = _callback_prefix(game)
    return {
        "inline_keyboard": [
            [
                {"text": "👍 Ок", "callback_data": f"{prefix}_yes:{sid}"},
                {"text": "👎 Не ок", "callback_data": f"{prefix}_no:{sid}"},
            ]
        ]
    }


def labeled_keyboard_markup(
    game: str,
    label: str,
    *,
    reason: str = "",
    segment_id: str = "",
) -> dict:
    from mlbb_calibration_store import dislike_reason_label

    prefix = _callback_prefix(game)
    if label == "good":
        sid = segment_id.strip()
        rows: list[list[dict]] = [[{"text": "✅ Ок", "callback_data": "mlbb_noop"}]]
        if sid:
            rows.append([{"text": "📁 HQ файл", "callback_data": f"{prefix}_hq:{sid}"}])
        return {"inline_keyboard": rows}
    mark = f"❌ {dislike_reason_label(reason)}"
    return {"inline_keyboard": [[{"text": mark, "callback_data": "mlbb_noop"}]]}


def keyboard(game: str, seg_id: str) -> dict:
    return inline_keyboard_markup(game, seg_id)
