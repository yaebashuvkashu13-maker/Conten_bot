#!/usr/bin/env python3
"""Force-find one MLBB kill-UI clip and send to Telegram (fast path, no full VOD scan)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/usr/local/bin")
sys.path.insert(0, str(Path(__file__).resolve().parent))

ENV_PATH = Path("/root/.video_bot.env")
VOD = Path(os.environ.get("MLBB_FORCE_VOD", "/root/data/mlbb/youtube_nightly/inbox/yt_0nvW7JiFr0o.mp4"))
PROFILE = "mobile_legends"


def _load_env() -> dict[str, str]:
    env = dict(os.environ)
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            env.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    return env


def _ffprobe_duration(path: Path) -> float:
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
        timeout=30,
    )
    try:
        return float((proc.stdout or "0").strip())
    except ValueError:
        return 0.0


def _find_kill_peak(vod: Path) -> tuple[float, dict]:
    from mlbb_kill_ui import score_mlbb_kill_ui

    duration = _ffprobe_duration(vod)
    min_t = float(os.environ.get("MLBB_VOD_MIN_PEAK_SEC", "420"))
    step = float(os.environ.get("MLBB_FORCE_SCAN_STEP", "90"))
    end = max(min_t + 1, duration - 30)
    best_t = min_t
    best = score_mlbb_kill_ui(vod, min_t, 12.0, sample_frames=3)
    t = min_t
    while t <= end:
        result = score_mlbb_kill_ui(vod, t, 12.0, sample_frames=3)
        print(
            f"scan t={t:.0f}s kill={result.has_kill_notification} "
            f"score={result.score:.3f} reason={result.reason}",
            flush=True,
        )
        if result.has_kill_notification and result.score >= best.score:
            best = result
            best_t = t
        t += step
    return best_t, best.to_dict()


def main() -> int:
    os.environ.setdefault("PYTHONPATH", "/usr/local/bin")
    os.environ["MLBB_REQUIRE_KILL_UI"] = "1"
    os.environ["SMART_MLBB_REQUIRE_KILL_UI"] = "1"
    os.environ["MLBB_KILL_UI_SKIP_OCR"] = "0"
    os.environ["MLBB_VOD_VARIABLE_LENGTH"] = "1"
    os.environ["MLBB_FIGHT_MIN_SEC"] = os.environ.get("MLBB_FIGHT_MIN_SEC", "7")
    os.environ["MLBB_FIGHT_MAX_SEC"] = os.environ.get("MLBB_FIGHT_MAX_SEC", "22")
    os.environ["HIGHLIGHT_HEATMAP"] = "0"
    os.environ["HIGHLIGHT_USE_OWNER_ANCHORS"] = "0"

    env = _load_env()
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("TG_BOT_TOKEN or TG_CHAT_ID missing", file=sys.stderr)
        return 1
    if not VOD.exists():
        print(f"VOD missing: {VOD}", file=sys.stderr)
        return 1

    print(f"force send vod={VOD.name}", flush=True)
    peak_t, kill_meta = _find_kill_peak(VOD)
    if not kill_meta.get("has_kill_notification"):
        print("no kill UI peak found", file=sys.stderr)
        return 2

    lead = float(os.environ.get("MLBB_VOD_LEAD_SEC", "4"))
    clip = {
        "source_path": str(VOD),
        "source_index": 0,
        "game_name": "MLBB",
        "start": round(peak_t, 3),
        "peak_start": round(peak_t, 3),
        "score": float(kill_meta.get("score", 0)),
        "highlight_metrics": {"pass_reason": kill_meta.get("reason", "kill_ui")},
        "gate_reason": f"kill_ui:{kill_meta.get('reason')}",
    }

    from mlbb_vod_segment_feed import render_single_segment, send_message, send_video, _ffprobe_duration
    from mlbb_vod_segment_store import inline_keyboard_markup, segment_id, segments_root, upsert_segment

    from mlbb_kill_ui import passes_mlbb_kill_gate

    ok, gate_reason, gate = passes_mlbb_kill_gate(VOD, peak_t, 15.0)
    if not ok:
        print(f"gate REJECT peak={peak_t:.0f}s reason={gate_reason}", file=sys.stderr)
        return 5

    sid = segment_id(VOD, peak_t)
    out = segments_root() / f"seg_{sid}.mp4"
    print(f"render peak={peak_t:.1f}s -> {out.name}", flush=True)
    t0 = time.time()
    if not render_single_segment(VOD, clip, out):
        print("render failed", file=sys.stderr)
        return 3
    seg_dur = _ffprobe_duration(out)
    print(f"render done in {time.time()-t0:.0f}s dur={seg_dur:.1f}s size={out.stat().st_size}", flush=True)

    row = {
        "segment_id": sid,
        "clip": clip,
        "start": peak_t,
        "peak_start": peak_t,
        "score": clip["score"],
        "hook_score": 0.0,
        "pass_reason": kill_meta.get("reason", ""),
    }
    print(f"gate OK: {gate_reason}", flush=True)

    caption = (
        f"MLBB кусок #{sid}\n"
        f"🆕 kill UI logic\n"
        f"{VOD.stem.replace('yt_', '')} @ {int(peak_t)}s | {seg_dur:.0f}с\n"
        f"kill: {kill_meta.get('reason')} score={kill_meta.get('score')}\n"
        f"📥 Скачать оригинал / 👎 Не ок"
    )
    send_message(
        token,
        chat_id,
        f"⚡ Первый клип на новой kill UI логике\n{VOD.name}\npeak={int(peak_t)}s",
    )
    if not send_video(token, chat_id, out, caption, seg_id=sid):
        print("telegram send failed", file=sys.stderr)
        return 4

    upsert_segment(
        {
            "segment_id": sid,
            "path": str(out),
            "vod": str(VOD),
            "vod_id": VOD.stem.replace("yt_", ""),
            "start": peak_t,
            "score": clip["score"],
            "hook_score": 0.0,
            "sig": "force_kill_ui",
            "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "kill_ui": kill_meta,
        }
    )
    print(json.dumps({"ok": True, "segment_id": sid, "path": str(out), "kill_ui": kill_meta}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
