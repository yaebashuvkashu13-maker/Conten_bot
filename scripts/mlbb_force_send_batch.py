#!/usr/bin/env python3
"""Find MLBB kill-UI scenes (7–22s) and send a calibration batch to Telegram."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

MLBB_TITLE_RE = re.compile(
    r"mobile legends|mlbb|bang bang|мобайл легенд|mobilelegend",
    re.I,
)
NON_MLBB_TITLE_RE = re.compile(
    r"\bpubg\b|playerunknown|metro royale|metroroyale|standoff|genshin|world of tanks|\bwot\b",
    re.I,
)

sys.path.insert(0, "/usr/local/bin")
sys.path.insert(0, str(Path(__file__).resolve().parent))

ENV_PATH = Path("/root/.video_bot.env")
INBOX = Path(os.environ.get("MLBB_VOD_INBOX", "/root/data/mlbb/youtube_nightly/inbox"))
BLOCKED_VODS_PATH = Path(os.environ.get("MLBB_BLOCKED_VODS", "/root/data/mlbb/blocked_vods.json"))


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


def _scan_kill_peaks(vod: Path) -> list[tuple[float, dict]]:
    from mlbb_kill_ui import score_mlbb_kill_ui

    duration = _ffprobe_duration(vod)
    min_t = float(os.environ.get("MLBB_VOD_MIN_PEAK_SEC", "420"))
    coarse_step = float(os.environ.get("MLBB_FORCE_SCAN_STEP", "45"))
    fine_step = float(os.environ.get("MLBB_FORCE_FINE_STEP", "8"))
    end = max(min_t + 1, duration - 25)
    hits: list[tuple[float, dict]] = []

    t = min_t
    while t <= end:
        result = score_mlbb_kill_ui(vod, t, 12.0, sample_frames=3)
        print(
            f"scan {vod.name} t={t:.0f}s kill={result.has_kill_notification} "
            f"score={result.score:.3f} {result.reason}",
            flush=True,
        )
        if result.has_kill_notification:
            hits.append((t, result.to_dict()))
        t += coarse_step

    refined: list[tuple[float, dict]] = []
    seen: set[int] = set()
    for peak_t, meta in hits:
        for dt in (-fine_step, 0, fine_step):
            t = round(peak_t + dt, 1)
            key = int(t)
            if key in seen:
                continue
            seen.add(key)
            result = score_mlbb_kill_ui(vod, t, 12.0, sample_frames=3)
            if result.has_kill_notification:
                refined.append((t, result.to_dict()))
                print(
                    f"  fine t={t:.0f}s score={result.score:.3f} {result.reason}",
                    flush=True,
                )

    refined.sort(key=lambda x: x[1].get("score", 0), reverse=True)
    return refined


def _dedupe_peaks(peaks: list[tuple[float, dict]], min_gap: float) -> list[tuple[float, dict]]:
    chosen: list[tuple[float, dict]] = []
    taken: list[float] = []
    for t, meta in peaks:
        if any(abs(t - s) < min_gap for s in taken):
            continue
        taken.append(t)
        chosen.append((t, meta))
    return chosen


def _load_forced_peaks(vods: list[Path], need: int) -> list[tuple[Path, float, dict]] | None:
    peaks_file = os.environ.get("MLBB_FORCE_PEAKS_FILE", "").strip()
    if not peaks_file:
        return None
    path = Path(peaks_file)
    if not path.exists():
        print(f"peaks file missing: {path}", file=sys.stderr)
        return None
    rows = json.loads(path.read_text())
    min_gap = float(os.environ.get("MLBB_VOD_SEGMENT_GAP_SEC", "45"))
    out: list[tuple[Path, float, dict]] = []
    vod_by_name = {v.name: v for v in vods}
    for row in rows:
        vod = vod_by_name.get(row["vod"]) or Path(row.get("vod_path", ""))
        if not isinstance(vod, Path):
            vod = Path(vod)
        if not vod.exists():
            continue
        out.append((vod, float(row["peak"]), row.get("kill_ui", {"score": row.get("score", 0), "reason": row.get("reason", "")})))
    out = _dedupe_by_vod_gap(out, min_gap)
    return out[:need]


def _collect_peaks(vods: list[Path], need: int) -> list[tuple[Path, float, dict]]:
    from mlbb_kill_ui import scan_vod_kill_peaks

    min_gap = float(os.environ.get("MLBB_VOD_SEGMENT_GAP_SEC", "45"))
    skip_starts = {
        float(x.strip())
        for x in os.environ.get("MLBB_FORCE_SKIP_STARTS", "416").split(",")
        if x.strip()
    }
    all_peaks: list[tuple[Path, float, dict]] = []
    for vod in vods:
        peaks = _dedupe_peaks(_scan_kill_peaks(vod), min_gap)
        if len(peaks) < max(2, need // len(vods)):
            for row in scan_vod_kill_peaks(vod, step_sec=30, limit=need * 2):
                peaks.append((float(row["start_sec"]), row))
        for peak_t, meta in peaks:
            if any(abs(peak_t - s) < min_gap for s in skip_starts):
                print(f"skip already-sent start~{int(peak_t)}s", flush=True)
                continue
            all_peaks.append((vod, peak_t, meta))
        all_peaks.sort(key=lambda x: x[2].get("score", 0), reverse=True)
        if len(all_peaks) >= need:
            break
    all_peaks.sort(key=lambda x: x[2].get("score", 0), reverse=True)
    return _dedupe_by_vod_gap(all_peaks, min_gap)[:need]


def _dedupe_by_vod_gap(
    peaks: list[tuple[Path, float, dict]], min_gap: float
) -> list[tuple[Path, float, dict]]:
    chosen: list[tuple[Path, float, dict]] = []
    taken: list[tuple[str, float]] = []
    for vod, t, meta in peaks:
        key = vod.name
        if any(k == key and abs(t - s) < min_gap for k, s in taken):
            continue
        taken.append((key, t))
        chosen.append((vod, t, meta))
    return chosen


def _fast_fight_bounds(peak_t: float, vod_dur: float, score: float) -> tuple[float, float, float]:
    from mlbb_fight_segment import apply_head_trim

    lead = float(os.environ.get("MLBB_VOD_LEAD_SEC", "4"))
    min_d = float(os.environ.get("MLBB_FIGHT_MIN_SEC", "7"))
    max_d = float(os.environ.get("MLBB_FIGHT_MAX_SEC", "22"))
    span = max_d - min_d
    dur = min_d + span * min(1.0, max(0.0, score) / 0.45)
    dur = round(max(min_d, min(max_d, dur)), 1)
    start = max(0.0, peak_t - lead)
    end = min(vod_dur, start + dur)
    dur = round(end - start, 1)
    start, dur = apply_head_trim(start, dur, vod_dur)
    return round(start, 2), round(start + dur, 2), dur


def _build_scene_clip(vod: Path, peak_t: float, kill_meta: dict) -> dict:
    score = float(kill_meta.get("score", 0))
    vod_dur = _ffprobe_duration(vod)
    start, end, dur = _fast_fight_bounds(peak_t, vod_dur, score)
    return {
        "source_path": str(vod),
        "source_index": 0,
        "game_name": "MLBB",
        "start": start,
        "peak_start": round(peak_t, 3),
        "fight_end": end,
        "input_duration": dur,
        "output_duration": dur,
        "speed": 1.0,
        "score": score,
        "highlight_metrics": {"pass_reason": kill_meta.get("reason", "kill_ui")},
        "gate_reason": f"kill_ui:{kill_meta.get('reason')}",
        "preserve_duration": True,
    }


def _vod_title_from_state(vid: str) -> str:
    state_path = Path(os.environ.get("MLBB_VOD_STATE", "/root/data/mlbb/vod_segment_state.json"))
    if not state_path.exists():
        return ""
    try:
        rows = json.loads(state_path.read_text(encoding="utf-8")).get("vods", [])
    except (json.JSONDecodeError, OSError):
        return ""
    for row in rows:
        if str(row.get("id", "")) == vid:
            return str(row.get("title", "") or "")
    return ""


def _vod_title(path: Path) -> str:
    vid = path.stem.replace("yt_", "")
    meta = path.with_suffix(".meta.json")
    if meta.exists():
        try:
            title = json.loads(meta.read_text()).get("title", "")
            if title:
                return str(title)
        except (json.JSONDecodeError, OSError):
            pass
    title = _vod_title_from_state(vid)
    if title:
        return title
    try:
        proc = subprocess.run(
            [
                "yt-dlp",
                "--print",
                "title",
                "--no-download",
                f"https://www.youtube.com/watch?v={vid}",
            ],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if proc.returncode == 0 and (proc.stdout or "").strip():
            return proc.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        pass
    return ""


def _blocked_vod_ids() -> set[str]:
    if not BLOCKED_VODS_PATH.exists():
        return set()
    try:
        data = json.loads(BLOCKED_VODS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    return {str(v).replace("yt_", "") for v in data.get("vods", [])}


def _looks_like_mlbb_vod(path: Path) -> bool:
    title = _vod_title(path)
    if not title:
        print(f"skip {path.name}: no title metadata", flush=True)
        return False
    if NON_MLBB_TITLE_RE.search(title):
        print(f"skip {path.name}: non-MLBB title={title[:100]}", flush=True)
        return False
    if not MLBB_TITLE_RE.search(title):
        print(f"skip {path.name}: missing MLBB markers title={title[:100]}", flush=True)
        return False
    return True


def _pick_vods() -> list[Path]:
    blocked = _blocked_vod_ids()
    min_mb = float(os.environ.get("MLBB_FORCE_MIN_VOD_MB", "200"))
    max_mb = float(os.environ.get("MLBB_FORCE_MAX_VOD_MB", "950"))
    explicit = os.environ.get("MLBB_FORCE_VOD", "").strip()
    if explicit:
        p = Path(explicit)
        vid = p.stem.replace("yt_", "")
        if vid in blocked:
            print(f"skip blocked vod {vid}", flush=True)
            return []
        return [p] if p.exists() and _looks_like_mlbb_vod(p) else []
    vods: list[Path] = []
    for p in INBOX.glob("yt_*.mp4"):
        vid = p.stem.replace("yt_", "")
        if vid in blocked:
            print(f"skip blocked {p.name}", flush=True)
            continue
        size_mb = p.stat().st_size / 1_000_000
        if size_mb < min_mb or size_mb > max_mb:
            continue
        dur = _ffprobe_duration(p)
        if dur < float(os.environ.get("MLBB_FORCE_MIN_VOD_SEC", "600")):
            continue
        if not _looks_like_mlbb_vod(p):
            continue
        vods.append(p)
    vods.sort(key=lambda p: p.stat().st_size)
    extra = os.environ.get("MLBB_FORCE_EXTRA_VODS", "").strip()
    if extra:
        for name in extra.split(","):
            p = INBOX / name.strip()
            if p.exists() and p not in vods:
                vods.append(p)
    return vods


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

    batch_n = int(os.environ.get("MLBB_FORCE_BATCH_COUNT", "10"))
    vods = _pick_vods()
    if not vods:
        print("no VODs found", file=sys.stderr)
        return 1

    print(f"batch={batch_n} vods={[v.name for v in vods]}", flush=True)
    forced_peaks = _load_forced_peaks(vods, batch_n)
    peaks = forced_peaks or _collect_peaks(vods, batch_n)
    trust_peaks = bool(forced_peaks) and os.environ.get("MLBB_FORCE_TRUST_PEAKS", "0") == "1"
    if not peaks:
        print("no kill UI peaks found", file=sys.stderr)
        return 2
    print(f"selected {len(peaks)} peaks trust={trust_peaks}", flush=True)

    from mlbb_vod_segment_feed import render_single_segment, send_message, send_video, _ffprobe_duration
    from mlbb_vod_segment_store import segment_id, segments_root, upsert_segment

    send_message(
        token,
        chat_id,
        f"MLBB — {len(peaks)} сцен (7–22с, kill UI)\n"
        f"👍 Ок / 👎 Не ок под каждым\n"
        f"Если 10 хороших — идём дальше",
    )

    sent = 0
    for vod, peak_t, kill_meta in peaks:
        clip = _build_scene_clip(vod, peak_t, kill_meta)
        start = float(clip["start"])
        dur = float(clip["input_duration"])
        if not trust_peaks:
            from mlbb_kill_ui import passes_mlbb_kill_gate

            ok, gate_reason, gate = passes_mlbb_kill_gate(vod, start, dur)
            if not ok:
                print(f"SKIP peak={peak_t:.0f}s gate={gate_reason}", flush=True)
                continue
        sid = segment_id(vod, start)
        out = segments_root() / f"seg_{sid}.mp4"
        print(f"render #{sent+1} peak={peak_t:.0f}s start={start:.0f}s -> {out.name}", flush=True)
        t0 = time.time()
        if not render_single_segment(vod, clip, out):
            print(f"render failed {sid}", file=sys.stderr)
            continue
        seg_dur = _ffprobe_duration(out)
        print(f"  done {time.time()-t0:.0f}s dur={seg_dur:.1f}s size={out.stat().st_size}", flush=True)

        caption = (
            f"MLBB кусок #{sid}\n"
            f"{vod.stem.replace('yt_', '')} @ {int(start)}s (пик {int(peak_t)}s) | {seg_dur:.0f}с\n"
            f"kill: {kill_meta.get('reason')} score={kill_meta.get('score'):.3f}\n"
            f"👍 Ок / 👎 Не ок"
        )
        if not send_video(token, chat_id, out, caption, seg_id=sid):
            print(f"telegram send failed {sid}", file=sys.stderr)
            continue
        upsert_segment(
            {
                "segment_id": sid,
                "path": str(out),
                "vod": str(vod),
                "vod_id": vod.stem.replace("yt_", ""),
                "start": start,
                "peak_start": peak_t,
                "score": clip["score"],
                "hook_score": 0.0,
                "sig": "force_kill_ui_batch",
                "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "kill_ui": kill_meta,
                "duration": seg_dur,
            }
        )
        sent += 1

    print(json.dumps({"ok": True, "sent": sent, "requested": batch_n}, ensure_ascii=False))
    return 0 if sent else 3


if __name__ == "__main__":
    raise SystemExit(main())
