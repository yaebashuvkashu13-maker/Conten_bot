#!/usr/bin/env python3
"""Trim last N owner-labeled PUBG clips to gunfire-only and send montages of 3 via Telegram.

Default label bucket is owner 👍 (good / ok). Pass --label bad for 👎.
"""

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


def load_recent_labeled(
    labels_path: Path,
    *,
    limit: int,
    label: str = "good",
) -> list[dict]:
    """Load newest owner labels from good/bad buckets (👍 default)."""
    bucket = "good" if str(label).lower() in {"good", "ok", "yes", "up", "like"} else "bad"
    data = json.loads(labels_path.read_text(encoding="utf-8"))
    rows = list(data.get(bucket) or [])
    rows.sort(key=_parse_at, reverse=True)
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        sid = str(row.get("segment_id") or "")
        path = Path(str(row.get("path") or ""))
        if not sid or sid in seen or not path.is_file():
            continue
        seen.add(sid)
        out.append(row)
        if len(out) >= limit:
            break
    return out


def load_recent_bad(labels_path: Path, *, limit: int) -> list[dict]:
    return load_recent_labeled(labels_path, limit=limit, label="bad")


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
    """Return (start, duration, probe) for the densest real gunfight.

    Prefers peak density over long weak islands. Returns duration<=0 when no
    qualifying fight is found (caller must skip — never keep run/loot).
    """
    from pubg_shooting_gate import pubg_passes_shooting_gate, pubg_probe_segment

    dur = _ffprobe_duration(path)
    step = float(os.environ.get("PUBG_DISLIKE_TRIM_STEP", "1.0"))
    win = float(os.environ.get("PUBG_DISLIKE_TRIM_PROBE", "2.0"))
    min_gun = float(os.environ.get("PUBG_DISLIKE_TRIM_MIN_GUN", "0.070"))
    min_burst = float(os.environ.get("PUBG_DISLIKE_TRIM_MIN_BURST", "6.0"))
    pad = float(os.environ.get("PUBG_DISLIKE_TRIM_PAD", "0.4"))
    max_out = float(os.environ.get("PUBG_DISLIKE_TRIM_MAX_SEC", "8.0"))
    min_bins = int(os.environ.get("PUBG_DISLIKE_TRIM_MIN_BINS", "2"))

    scores: list[tuple[float, float, float]] = []  # t, gun, burst
    t = 0.0
    while t + 0.8 < dur:
        probe = pubg_probe_segment(path, t, min(win, max(0.8, dur - t)))
        gun = float(probe.get("gunfire_density") or 0.0)
        burst = float(probe.get("burst_ratio") or 0.0)
        scores.append((t, gun, burst))
        t += step

    if not scores:
        return 0.0, 0.0, {"gunfire_density": 0.0, "reject": "no_bins"}

    def _active(row: tuple[float, float, float]) -> bool:
        return row[1] >= min_gun and row[2] >= min_burst

    islands: list[tuple[int, int, float, float]] = []  # i0, i1, dens_sum, peak
    i = 0
    while i < len(scores):
        if not _active(scores[i]):
            i += 1
            continue
        j = i
        dens = 0.0
        peak = 0.0
        while j < len(scores) and _active(scores[j]):
            dens += scores[j][1] * max(0.5, scores[j][2] / 8.0)
            peak = max(peak, scores[j][1])
            j += 1
        if j - i >= min_bins:
            islands.append((i, j, dens, peak))
        i = max(j, i + 1)

    if not islands:
        # Single strong peak still allowed if very clear.
        best_i, best = max(enumerate(scores), key=lambda x: (x[1][1], x[1][2]))
        if best[1] >= min_gun * 1.15 and best[2] >= min_burst:
            start = max(0.0, best[0] - pad)
            length = min(max_out, max(3.0, win + 2 * pad), dur - start)
            ok, reason, metrics = pubg_passes_shooting_gate(path, start, length)
            if not ok:
                return 0.0, 0.0, {
                    "gunfire_density": best[1],
                    "burst_ratio": best[2],
                    "reject": reason,
                }
            return start, length, {
                "gunfire_density": round(best[1], 4),
                "burst_ratio": round(best[2], 4),
                "bins": 1,
                "gate": reason,
            }
        return 0.0, 0.0, {
            "gunfire_density": best[1],
            "burst_ratio": best[2],
            "reject": "no_fight_island",
        }

    # Prefer densest fight, not the longest weak run.
    i0, i1, _, _ = max(islands, key=lambda x: (x[2], x[3], x[1] - x[0]))
    peak_idx = max(range(i0, i1), key=lambda k: (scores[k][1], scores[k][2]))
    peak_t = scores[peak_idx][0]
    # Keep a short window around the peak; expand only while bins stay hot.
    left = peak_idx
    right = peak_idx
    while left > i0 and scores[left - 1][1] >= min_gun * 0.9:
        left -= 1
    while right + 1 < i1 and scores[right + 1][1] >= min_gun * 0.9:
        right += 1
    start = max(0.0, scores[left][0] - pad)
    end = min(dur, scores[right][0] + win + pad)
    # Hard-cap around peak so we never drag loot tails.
    start = max(start, peak_t - max_out * 0.55)
    end = min(end, peak_t + max_out * 0.55)
    length = max(2.5, min(max_out, end - start))
    mean_gun = sum(s[1] for s in scores[left : right + 1]) / max(1, right - left + 1)
    mean_burst = sum(s[2] for s in scores[left : right + 1]) / max(1, right - left + 1)

    ok, reason, metrics = pubg_passes_shooting_gate(path, start, length)
    if not ok:
        return 0.0, 0.0, {
            "gunfire_density": round(mean_gun, 4),
            "burst_ratio": round(mean_burst, 4),
            "reject": reason,
            "peak_t": round(peak_t, 2),
        }
    return start, length, {
        "gunfire_density": round(mean_gun, 4),
        "burst_ratio": round(mean_burst, 4),
        "bins": right - left + 1,
        "peak_t": round(peak_t, 2),
        "gate": reason,
        "loot_walk": bool(metrics.get("loot_walk")),
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
        "--label",
        default="good",
        choices=("good", "bad", "ok", "yes"),
        help="Owner label bucket (default: good/👍/ok)",
    )
    ap.add_argument(
        "--labels",
        default="/root/data/pubg/vod_segment_labels.json",
    )
    ap.add_argument(
        "--out-dir",
        default="",
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--chat-id", default="")
    args = ap.parse_args()
    label = "good" if args.label in {"good", "ok", "yes"} else "bad"
    mark = "👍" if label == "good" else "👎"
    if not args.out_dir:
        args.out_dir = (
            "/root/data/pubg/like_gun_montages"
            if label == "good"
            else "/root/data/pubg/dislike_gun_montages"
        )

    env = {**os.environ, **load_env()}
    token = env.get("TG_BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN") or ""
    chat_id = args.chat_id or env.get("TG_CHAT_ID") or env.get("OWNER_CHAT_ID") or ""
    if not args.dry_run and (not token or not chat_id):
        print("TG_BOT_TOKEN / chat_id missing", file=sys.stderr)
        return 2

    rows = load_recent_labeled(Path(args.labels), limit=args.limit, label=label)
    if not rows:
        print(f"no {label} segments found")
        return 1

    from mlbb_telegram_video import send_document_file
    from mlbb_vod_segment_feed import send_message

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    group = max(1, int(args.group))
    if not args.dry_run:
        send_message(
            token,
            str(chat_id),
            f"🔁 Перерезка последних {len(rows)} {mark} → стрельба, склейки по {group}. "
            f"Буду слать по мере готовности.",
        )

    trimmed: list[tuple[dict, Path, dict]] = []
    pending: list[tuple[dict, Path, dict]] = []
    skipped = 0
    sent = 0
    montage_idx = 0

    def flush_pending() -> None:
        nonlocal pending, sent, montage_idx
        while len(pending) >= group:
            batch = pending[:group]
            pending = pending[group:]
            montage_idx += 1
            parts = [p for _, p, _ in batch]
            ids = [str(r["segment_id"]) for r, _, _ in batch]
            montage = out_dir / f"montage_{montage_idx:02d}_{int(time.time())}.mp4"
            if len(parts) == 1:
                montage = parts[0]
                ok_concat = True
            else:
                ok_concat = ffmpeg_concat(parts, montage)
            if not ok_concat:
                print(f"concat fail batch={ids}")
                continue
            caption = f"{mark}→🔫 montage {montage_idx} ({len(parts)} parts)\n" + "\n".join(ids)
            if args.dry_run:
                print(f"dry-run would send {montage} :: {caption}")
                sent += 1
                continue
            ok = send_document_file(
                token, str(chat_id), montage, caption, force_file=True
            )
            print(
                f"send {'ok' if ok else 'FAIL'} {montage.name} size={montage.stat().st_size}"
            )
            if ok:
                sent += 1
            time.sleep(1.0)

    force_retrim = os.environ.get("PUBG_DISLIKE_FORCE_RETRIM", "1") == "1"
    for row in rows:
        src = Path(str(row["path"]))
        sid = str(row["segment_id"])
        dest = out_dir / f"gun_{sid}.mp4"
        try:
            start, dur, probe = find_gun_window(src)
        except Exception as exc:
            print(f"probe fail {sid}: {exc}")
            skipped += 1
            continue
        if dur <= 0 or probe.get("reject"):
            print(
                f"skip no-fight {sid} gun={probe.get('gunfire_density')} "
                f"burst={probe.get('burst_ratio')} reject={probe.get('reject')}"
            )
            skipped += 1
            if dest.exists():
                dest.unlink(missing_ok=True)
            continue
        if float(probe.get("gunfire_density") or 0.0) < float(
            os.environ.get("PUBG_DISLIKE_KEEP_MIN_GUN", "0.065")
        ):
            print(f"skip weak gun {sid} gun={probe.get('gunfire_density')}")
            skipped += 1
            continue
        if (
            not force_retrim
            and dest.is_file()
            and dest.stat().st_size > 50_000
            and abs(_ffprobe_duration(dest) - dur) < 0.75
        ):
            print(f"reuse {dest.name}")
        elif not ffmpeg_trim(src, start, dur, dest):
            print(f"trim fail {sid}")
            skipped += 1
            continue
        else:
            print(
                f"trimmed {sid} {start:.1f}+{dur:.1f}s gun={probe.get('gunfire_density')} "
                f"burst={probe.get('burst_ratio')} peak={probe.get('peak_t')} -> {dest.name}"
            )
        item = (row, dest, probe)
        trimmed.append(item)
        pending.append(item)
        flush_pending()

    # Remainder (< group)
    if pending:
        batch = pending
        pending = []
        montage_idx += 1
        parts = [p for _, p, _ in batch]
        ids = [str(r["segment_id"]) for r, _, _ in batch]
        montage = out_dir / f"montage_{montage_idx:02d}_{int(time.time())}.mp4"
        if len(parts) == 1:
            montage = parts[0]
            ok_concat = True
        else:
            ok_concat = ffmpeg_concat(parts, montage)
        if ok_concat:
            caption = f"{mark}→🔫 montage {montage_idx} ({len(parts)} parts)\n" + "\n".join(ids)
            if args.dry_run:
                print(f"dry-run would send {montage} :: {caption}")
                sent += 1
            else:
                ok = send_document_file(
                    token, str(chat_id), montage, caption, force_file=True
                )
                print(
                    f"send {'ok' if ok else 'FAIL'} {montage.name} size={montage.stat().st_size}"
                )
                if ok:
                    sent += 1

    print(f"done trimmed={len(trimmed)} skipped={skipped} sent={sent}")
    return 0 if sent else 1


if __name__ == "__main__":
    raise SystemExit(main())
