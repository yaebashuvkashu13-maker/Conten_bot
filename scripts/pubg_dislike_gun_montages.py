#!/usr/bin/env python3
"""Trim last N owner-👎 PUBG clips to gunfire-only and send montages of 3 via Telegram."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from youtube_download import load_env


def _parse_at(row: dict) -> datetime:
    at = str(row.get("at") or "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(at[:19], fmt)
        except ValueError:
            pass
    return datetime.min


def load_recent_bad(labels_path: Path, *, limit: int) -> list[dict]:
    data = json.loads(labels_path.read_text(encoding="utf-8"))
    bad = list(data.get("bad") or [])
    bad.sort(key=_parse_at, reverse=True)
    out: list[dict] = []
    seen: set[str] = set()
    for row in bad:
        sid = str(row.get("segment_id") or "")
        path = Path(str(row.get("path") or ""))
        if not sid or sid in seen or not path.is_file():
            continue
        seen.add(sid)
        out.append(row)
        if len(out) >= limit:
            break
    return out


def _ffprobe_duration(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nk=1:nw=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return max(0.5, float((proc.stdout or "").strip()))
    except ValueError:
        return 12.0


def find_gun_window(path: Path) -> tuple[float, float, dict]:
    """Return (start, duration, probe) covering the densest contiguous gunfire."""
    from pubg_shooting_gate import pubg_probe_segment

    dur = _ffprobe_duration(path)
    step = float(os.environ.get("PUBG_DISLIKE_TRIM_STEP", "1.0"))
    win = float(os.environ.get("PUBG_DISLIKE_TRIM_PROBE", "2.5"))
    min_gun = float(os.environ.get("PUBG_DISLIKE_TRIM_MIN_GUN", "0.035"))
    pad = float(os.environ.get("PUBG_DISLIKE_TRIM_PAD", "0.6"))
    max_out = float(os.environ.get("PUBG_DISLIKE_TRIM_MAX_SEC", "14"))

    scores: list[tuple[float, float, float]] = []  # t, gun, burst
    t = 0.0
    while t + 0.8 < dur:
        probe = pubg_probe_segment(path, t, min(win, max(0.8, dur - t)))
        gun = float(probe.get("gunfire_density") or 0.0)
        burst = float(probe.get("burst_ratio") or 0.0)
        scores.append((t, gun, burst))
        t += step

    if not scores:
        return 0.0, min(dur, max_out), {"gunfire_density": 0.0, "fallback": True}

    # Keep contiguous bins above min_gun; pick longest / densest island.
    islands: list[tuple[int, int, float]] = []
    i = 0
    while i < len(scores):
        if scores[i][1] < min_gun:
            i += 1
            continue
        j = i
        dens = 0.0
        while j < len(scores) and scores[j][1] >= min_gun:
            dens += scores[j][1]
            j += 1
        islands.append((i, j, dens))
        i = j
    if not islands:
        # Fallback: densest single window.
        best = max(scores, key=lambda x: (x[1], x[2]))
        start = max(0.0, best[0] - pad)
        length = min(max_out, max(3.0, win + 2 * pad), dur - start)
        return start, length, {"gunfire_density": best[1], "burst_ratio": best[2], "fallback": True}

    i0, i1, _ = max(islands, key=lambda x: (x[1] - x[0], x[2]))
    start = max(0.0, scores[i0][0] - pad)
    end = min(dur, scores[i1 - 1][0] + win + pad)
    length = min(max_out, max(2.5, end - start))
    mean_gun = sum(s[1] for s in scores[i0:i1]) / max(1, i1 - i0)
    mean_burst = sum(s[2] for s in scores[i0:i1]) / max(1, i1 - i0)
    return start, length, {
        "gunfire_density": round(mean_gun, 4),
        "burst_ratio": round(mean_burst, 4),
        "bins": i1 - i0,
    }


def ffmpeg_trim(src: Path, start: float, duration: float, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start:.3f}",
        "-i",
        str(src),
        "-t",
        f"{duration:.3f}",
        "-c:v",
        "libx264",
        "-preset",
        os.environ.get("PUBG_DISLIKE_X264_PRESET", "veryfast"),
        "-crf",
        os.environ.get("PUBG_DISLIKE_CRF", "18"),
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        "-y",
        str(dest),
    ]
    return subprocess.run(cmd, check=False).returncode == 0 and dest.is_file()


def ffmpeg_concat(parts: list[Path], dest: Path) -> bool:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
        for part in parts:
            handle.write(f"file '{part}'\n")
        list_path = Path(handle.name)
    try:
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-y",
            str(dest),
        ]
        ok = subprocess.run(cmd, check=False).returncode == 0 and dest.is_file()
        if not ok:
            # re-encode fallback
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_path),
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "18",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                "-y",
                str(dest),
            ]
            ok = subprocess.run(cmd, check=False).returncode == 0 and dest.is_file()
        return ok
    finally:
        list_path.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--group", type=int, default=3)
    ap.add_argument(
        "--labels",
        default="/root/data/pubg/vod_segment_labels.json",
    )
    ap.add_argument(
        "--out-dir",
        default="/root/data/pubg/dislike_gun_montages",
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--chat-id", default="")
    args = ap.parse_args()

    env = {**os.environ, **load_env()}
    token = env.get("TG_BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN") or ""
    chat_id = args.chat_id or env.get("TG_CHAT_ID") or env.get("OWNER_CHAT_ID") or ""
    if not args.dry_run and (not token or not chat_id):
        print("TG_BOT_TOKEN / chat_id missing", file=sys.stderr)
        return 2

    rows = load_recent_bad(Path(args.labels), limit=args.limit)
    if not rows:
        print("no bad segments found")
        return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    trimmed: list[tuple[dict, Path, dict]] = []
    skipped = 0
    for row in rows:
        src = Path(str(row["path"]))
        sid = str(row["segment_id"])
        try:
            start, dur, probe = find_gun_window(src)
        except Exception as exc:
            print(f"probe fail {sid}: {exc}")
            skipped += 1
            continue
        if float(probe.get("gunfire_density") or 0.0) < float(
            os.environ.get("PUBG_DISLIKE_KEEP_MIN_GUN", "0.025")
        ):
            print(f"skip weak gun {sid} gun={probe.get('gunfire_density')}")
            skipped += 1
            continue
        dest = out_dir / f"gun_{sid}.mp4"
        if not ffmpeg_trim(src, start, dur, dest):
            print(f"trim fail {sid}")
            skipped += 1
            continue
        trimmed.append((row, dest, probe))
        print(
            f"trimmed {sid} {start:.1f}+{dur:.1f}s gun={probe.get('gunfire_density')} -> {dest.name}"
        )

    if not trimmed:
        print("nothing trimmed")
        return 1

    from mlbb_telegram_video import send_document_file
    from mlbb_vod_segment_feed import send_message

    if not args.dry_run:
        send_message(
            token,
            str(chat_id),
            f"🔁 Перерезка последних {len(rows)} 👎 → {len(trimmed)} кусков со стрельбой "
            f"(групп по {args.group}). Пропущено без стрельбы: {skipped}.",
        )

    sent = 0
    group = max(1, int(args.group))
    for i in range(0, len(trimmed), group):
        batch = trimmed[i : i + group]
        parts = [p for _, p, _ in batch]
        ids = [str(r["segment_id"]) for r, _, _ in batch]
        montage = out_dir / f"montage_{i // group + 1:02d}_{int(time.time())}.mp4"
        if len(parts) == 1:
            montage = parts[0]
            ok_concat = True
        else:
            ok_concat = ffmpeg_concat(parts, montage)
        if not ok_concat:
            print(f"concat fail batch={ids}")
            continue
        caption = (
            f"👎→🔫 montage {i // group + 1} ({len(parts)} parts)\n"
            + "\n".join(ids)
        )
        if args.dry_run:
            print(f"dry-run would send {montage} :: {caption}")
            sent += 1
            continue
        ok = send_document_file(
            token,
            str(chat_id),
            montage,
            caption,
            force_file=True,
        )
        print(f"send {'ok' if ok else 'FAIL'} {montage.name} size={montage.stat().st_size}")
        if ok:
            sent += 1
        time.sleep(1.0)

    print(f"done trimmed={len(trimmed)} skipped={skipped} sent={sent}")
    return 0 if sent else 1


if __name__ == "__main__":
    raise SystemExit(main())
