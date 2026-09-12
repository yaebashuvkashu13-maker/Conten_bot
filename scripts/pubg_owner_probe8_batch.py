#!/usr/bin/env python3
"""Split last N owner-OK VOD segments into 8s probes and send with 👍/👎.

Owner asked for fact labels instead of bot guesswork: take recent OK chunks,
slice to 8s, rate each. Better ground truth than synthetic fight search.

Parents must be long original OK segments — never re-slice already-rated _p8 probes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

SLICE_SEC = float(os.environ.get("PROBE8_SLICE_SEC", "8"))
MAX_SEND = int(os.environ.get("PROBE8_MAX", "48"))
PARENTS = int(os.environ.get("PROBE8_PARENTS", "20"))
MIN_PARENT_SEC = float(os.environ.get("PROBE8_MIN_PARENT_SEC", "20"))
LABELS_PATH = Path(
    os.environ.get(
        "PUBG_VOD_SEGMENT_LABELS",
        "/root/data/pubg/vod_segment_labels.json",
    )
)
OUT_DIR = Path(
    os.environ.get("PROBE8_OUT", "/root/datasets/pubg/vod_segments/probe8")
)
LOG_PATH = Path(
    os.environ.get("PROBE8_LOG", "/root/data/pubg/owner_probe8_sent.json")
)
ENV_PATH = Path(os.environ.get("VIDEO_BOT_ENV", "/root/.video_bot.env"))


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if not ENV_PATH.is_file():
        return env
    for line in ENV_PATH.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip()] = val.strip().strip('"').strip("'")
    for key, val in env.items():
        os.environ.setdefault(key, val)
    return env


def ffprobe_duration(path: Path) -> float:
    try:
        out = subprocess.check_output(
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
            text=True,
            timeout=30,
        )
        return float(out.strip() or 0.0)
    except (subprocess.SubprocessError, ValueError, OSError):
        return 0.0


def cut_clip(src: Path, start: float, length: float, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{length:.3f}",
        "-i",
        str(src),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.SubprocessError:
        return False
    return proc.returncode == 0 and dest.is_file() and dest.stat().st_size > 10_000


def tg_api(token: str, method: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def video_id_from_row(vod_path: Path, parent_sid: str) -> str:
    if vod_path.name:
        stem = vod_path.stem
        if stem.startswith("yt_") and len(stem) > 3:
            return stem[3:]
        return stem
    if "_" in parent_sid:
        return parent_sid.rsplit("_", 1)[0]
    return parent_sid


def is_probe_parent(row: dict) -> bool:
    sid = str(row.get("segment_id") or "")
    path = str(row.get("path") or "")
    if sid.endswith("_p8") or "_p8" in sid or sid.endswith("_ext"):
        return True
    if "/probe8/" in path or path.endswith("_p8.mp4") or "_p8." in path:
        return True
    return False


def select_long_parents(goods: list[dict], limit: int) -> list[dict]:
    """Most recent long OK segments; skip already-sliced probe clips."""
    out: list[dict] = []
    for row in sorted(goods, key=lambda r: str(r.get("at") or ""), reverse=True):
        if is_probe_parent(row):
            continue
        src = Path(str(row.get("path") or ""))
        if not src.is_file():
            continue
        qm = row.get("quality_metrics") or {}
        labeled_dur = float(qm.get("duration") or 0.0)
        duration = labeled_dur if labeled_dur >= MIN_PARENT_SEC else ffprobe_duration(src)
        if duration < MIN_PARENT_SEC:
            continue
        out.append(row)
        if len(out) >= limit:
            break
    return out


def main() -> int:
    env = load_env()
    token = (env.get("TG_BOT_TOKEN") or os.environ.get("TG_BOT_TOKEN") or "").strip()
    chat = (env.get("TG_CHAT_ID") or os.environ.get("TG_CHAT_ID") or "").strip()
    if not chat:
        chats = (env.get("PUBG_CHAT_IDS") or "").split(",")
        chat = chats[0].strip() if chats else ""
    if not token or not chat:
        print("missing TG_BOT_TOKEN / TG_CHAT_ID", file=sys.stderr)
        return 2

    from pubg_owner_rated_send import send_owner_rated_clip

    labels = json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    parents = select_long_parents(labels.get("good") or [], PARENTS)

    probes: list[dict] = []
    for row in parents:
        src = Path(str(row.get("path") or ""))
        parent_start = float(row.get("start") or 0.0)
        vod_path = Path(str(row.get("vod") or ""))
        duration = ffprobe_duration(src)
        if duration < MIN_PARENT_SEC:
            print("SKIP short parent", row.get("segment_id"), duration)
            continue
        parent_sid = str(row.get("segment_id") or src.stem)
        vid = video_id_from_row(vod_path, parent_sid)
        t = 0.0
        while t + 3.0 <= duration + 1e-6:
            length = min(SLICE_SEC, duration - t)
            if length < 3.0:
                break
            abs_start = parent_start + t
            sid = f"{vid}_{int(abs_start)}_p8"
            probes.append(
                {
                    "parent": parent_sid,
                    "src": src,
                    "rel": t,
                    "length": length,
                    "abs_start": abs_start,
                    "vod": vod_path,
                    "sid": sid,
                }
            )
            t += SLICE_SEC

    prev: dict = {}
    if LOG_PATH.exists():
        try:
            prev = json.loads(LOG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            prev = {}
    sent_ids = set(prev.get("sent_ids") or [])
    results = list(prev.get("results") or [])

    remaining_before = sum(1 for p in probes if p["sid"] not in sent_ids)
    print(
        f"parents_ok={len(parents)} probes_planned={len(probes)} "
        f"already_sent={len(sent_ids)} remaining_unsent={remaining_before} "
        f"send_cap={MAX_SEND}"
    )
    for row in parents:
        print(" parent", row.get("segment_id"), "start", row.get("start"))
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    tg_api(
        token,
        "sendMessage",
        {
            "chat_id": chat,
            "text": (
                "Следующая партия 8с-проб из длинных OK-кусков.\n"
                f"В очереди ещё ~{remaining_before}, шлю до {MAX_SEND}.\n"
                "👍 только где нужный момент (бой), 👎 беготня/пусто."
            ),
        },
    )

    n_sent = 0
    for probe in probes:
        if n_sent >= MAX_SEND:
            break
        if probe["sid"] in sent_ids:
            continue
        dest = OUT_DIR / f"seg_{probe['sid']}.mp4"
        if not dest.is_file() or dest.stat().st_size < 10_000:
            if not cut_clip(
                probe["src"],
                float(probe["rel"]),
                float(probe["length"]),
                dest,
            ):
                print("CUT FAIL", probe["sid"])
                continue
        caption = (
            f"PUBG · 8s probe {n_sent + 1}/{MAX_SEND}\n"
            f"from OK {probe['parent']} · "
            f"t={probe['abs_start']:.0f}s ({probe['length']:.0f}s)\n"
            "👍 = нужный момент · 👎 = нет"
        )
        vod_arg = probe["vod"] if probe["vod"].is_file() else dest
        try:
            info = send_owner_rated_clip(
                dest,
                caption=caption,
                vod=vod_arg,
                start_sec=float(probe["abs_start"]),
                peak_sec=float(probe["abs_start"]) + float(probe["length"]) * 0.5,
                chat_id=chat,
                token=token,
                segment_id=probe["sid"],
            )
            print("SENT", probe["sid"], info.get("ok"), info.get("segment_id"))
            sent_ids.add(probe["sid"])
            results.append(
                {
                    "sid": probe["sid"],
                    "parent": probe["parent"],
                    "abs_start": probe["abs_start"],
                    "info": {
                        key: info.get(key)
                        for key in ("ok", "message_id", "segment_id")
                    },
                }
            )
            n_sent += 1
            time.sleep(0.7)
        except Exception as exc:  # noqa: BLE001
            print("SEND FAIL", probe["sid"], exc)
            time.sleep(1.2)

    remaining = sum(1 for p in probes if p["sid"] not in sent_ids)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text(
        json.dumps(
            {
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "planned": len(probes),
                "sent_count": len(sent_ids),
                "sent_ids": sorted(sent_ids),
                "batch_sent": n_sent,
                "remaining": remaining,
                "parents": [str(r.get("segment_id")) for r in parents],
                "results": results[-120:],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(
        f"DONE batch_sent={n_sent} "
        f"total_unique_sent={len(sent_ids)} remaining={remaining}"
    )
    if remaining > 0:
        tg_api(
            token,
            "sendMessage",
            {
                "chat_id": chat,
                "text": (
                    f"Партия отправлена: {n_sent}. Осталось {remaining} "
                    "восьмисекундок — напиши «ещё» после оценки."
                ),
            },
        )
    else:
        tg_api(
            token,
            "sendMessage",
            {
                "chat_id": chat,
                "text": f"Партия отправлена: {n_sent}. Очередь 8с-проб по текущим OK пуста.",
            },
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
