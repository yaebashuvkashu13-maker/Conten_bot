#!/usr/bin/env python3
"""MLBB same-VOD highlight montages (10–30 min sources).

One VOD → 3–4 kill-banner moments (singles OK) → one xfade montage.
Minimizes idle run via banner windows + anti-run trim.
Does not mix peaks across VODs. Outside daily quota by default.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("mlbb_same_vod_montage")


def _load_env(path: Path) -> dict[str, str]:
    from smart_video_editor import load_env

    env = load_env(path)
    for k, v in env.items():
        os.environ.setdefault(k, v)
    return env


def _ffprobe_duration(path: Path) -> float:
    from smart_video_editor import ffprobe_duration

    return float(ffprobe_duration(path) or 0.0)


def _prepare_montage_env() -> None:
    # Prefer mid-length ranked VODs; singles allowed inside montages.
    os.environ["MLBB_VOD_MONTAGE"] = "1"
    os.environ["MLBB_VOD_MONTAGE_MIN_CLIPS"] = os.environ.get("MLBB_VOD_MONTAGE_MIN_CLIPS", "3")
    os.environ["MLBB_VOD_MONTAGE_MAX_CLIPS"] = os.environ.get("MLBB_VOD_MONTAGE_MAX_CLIPS", "4")
    # Mid VODs need tighter spacing than long streams.
    os.environ.setdefault("MLBB_VOD_MONTAGE_GAP_SEC", "45")
    os.environ.setdefault("MLBB_VOD_MONTAGE_MIN_SEC", "32")
    os.environ.setdefault("MLBB_VOD_MONTAGE_MAX_SEC", "70")
    os.environ.setdefault("MLBB_VOD_MONTAGE_MIN_TIER", "single")
    os.environ["MLBB_KILL_BANNER_MIN_TIER"] = "single"
    os.environ["MLBB_KILL_BANNER_REQUIRED"] = "1"
    os.environ["MLBB_VOD_BANNER_DISCOVER"] = "1"
    os.environ["MLBB_VOD_MOTION_ANCHOR_OK"] = "0"
    os.environ["MLBB_VOD_TRIM_RUN"] = "1"
    # Dense banner sweep — montages live or die on banner recall.
    os.environ.setdefault("MLBB_VOD_BANNER_DENSE_SEC", "1")
    os.environ.setdefault("MLBB_KILL_BANNER_DISCOVER_STEP", "1")
    os.environ.setdefault("MLBB_KILL_BANNER_DISCOVER_MAX_PROBES", "220")
    os.environ.setdefault("MLBB_KILL_BANNER_DISCOVER_MAX_SEC", "480")
    os.environ.setdefault("MLBB_KILL_BANNER_DISCOVER_TARGET", "10")
    # Soft gates: owner OCR is weak on YT compressions.
    os.environ.setdefault("MLBB_BANNER_OWNER_GATE", "0")
    os.environ.setdefault("MLBB_BANNER_SEND_STRICT", "0")
    os.environ.setdefault("MLBB_BANNER_REF_MATCH", "1")
    os.environ.setdefault("MLBB_BANNER_POS_LIVE_MIN_SIM", "0.48")
    # Do not burn daily cycle quota on this experiment.
    os.environ["DAILY_GAME_CYCLE_ENABLED"] = "0"
    os.environ["MLBB_VOD_IGNORE_DAILY_QUOTA"] = "1"


def _inbox_vods(inbox: Path, *, min_sec: float, max_sec: float) -> list[Path]:
    rows: list[tuple[float, Path]] = []
    for p in sorted(inbox.glob("yt_*.mp4")):
        dur = _ffprobe_duration(p)
        if min_sec <= dur <= max_sec:
            rows.append((dur, p))
    rows.sort(key=lambda x: x[0])  # shorter first — faster discover
    return [p for _, p in rows]


def _vod_id(path: Path) -> str:
    name = path.stem
    return name[3:] if name.startswith("yt_") else name


def _hit_to_row(vod: Path, hit, file_dur: float) -> dict | None:
    from mlbb_fight_segment import banner_lead_sec
    from mlbb_kill_banner import bounds_from_banner
    from mlbb_vod_montage import apply_run_trim_to_clip
    from mlbb_vod_segment_store import segment_id

    tier = int(getattr(hit, "tier", 0) or 0)
    if tier < 1:
        return None
    peak = float(hit.sec)
    start, end, dur = bounds_from_banner(peak, file_dur, banner_tier=tier)
    # Floor: always keep some pre-roll before banner (streak context).
    lead = banner_lead_sec(tier)
    start = min(start, max(0.0, peak - lead))
    dur = max(dur, end - start)
    clip = {
        "start": float(start),
        "peak_start": peak,
        "banner_sec": peak,
        "input_duration": float(dur),
        "output_duration": float(dur),
        "anchor": "kill_banner",
        "kill_banner": str(getattr(hit, "label", "") or f"tier{tier}"),
        "kill_banner_tier": tier,
        "source": str(getattr(hit, "source", "") or ""),
    }
    clip = apply_run_trim_to_clip(clip, vod)
    fight_dur = float(clip.get("input_duration") or dur)
    return {
        "segment_id": segment_id(vod, float(clip["start"])),
        "start": float(clip["start"]),
        "peak_start": peak,
        "fight_dur": fight_dur,
        "score": float(tier),
        "clip_score": float(tier),
        "kill_banner": clip["kill_banner"],
        "kill_banner_tier": tier,
        "anchor": "kill_banner",
        "clip": clip,
    }


def _discover_rows(vod: Path) -> list[dict]:
    from mlbb_kill_banner import discover_vod_kill_banners

    file_dur = _ffprobe_duration(vod)
    t0 = time.monotonic()
    hits = discover_vod_kill_banners(vod, min_tier=1)
    log.info(
        "discover vod=%s hits=%s elapsed=%.0fs",
        vod.name,
        len(hits),
        time.monotonic() - t0,
    )
    rows: list[dict] = []
    for h in sorted(hits, key=lambda x: (-int(x.tier), float(x.sec))):
        row = _hit_to_row(vod, h, file_dur)
        if row:
            rows.append(row)
    return rows


def _build_and_send_one(
    vod: Path,
    *,
    token: str,
    chat_id: str,
    out_dir: Path,
) -> dict:
    from mlbb_vod_montage import (
        build_montage_id,
        cleanup_temps,
        concat_rendered_parts,
        pick_montage_rows,
    )
    from mlbb_vod_segment_feed import render_single_segment, _validate_before_send
    from mlbb_telegram_video import (
        TELEGRAM_MAX_BYTES,
        compress_for_inline_video,
        send_document_file,
        send_video_file,
    )
    from mlbb_vod_segment_store import inline_keyboard_markup, upsert_segment, mark_feed_sent
    from smart_video_editor import ffprobe_duration

    vid = _vod_id(vod)
    rows = _discover_rows(vod)
    picked = pick_montage_rows(rows)
    report: dict = {
        "vod_id": vid,
        "vod": str(vod),
        "hits": len(rows),
        "picked": [],
        "ok": False,
    }
    if len(picked) < 3:
        report["error"] = f"need>=3 bannered parts, got {len(picked)}"
        log.warning("%s %s", vid, report["error"])
        return report

    mid = build_montage_id(vid, picked)
    out = out_dir / f"seg_{mid}.mp4"
    out_dir.mkdir(parents=True, exist_ok=True)
    temps: list[Path] = []
    parts: list[Path] = []
    durs: list[float] = []
    gated: list[dict] = []
    try:
        import tempfile

        for i, row in enumerate(picked):
            part = Path(tempfile.mkstemp(suffix=f".m{i}.mp4")[1])
            temps.append(part)
            if not render_single_segment(vod, row["clip"], part):
                log.warning("render fail %s part=%s", mid, i)
                continue
            ok, reason, _ = _validate_before_send(vod, row, part)
            if not ok:
                log.warning("gate reject %s part=%s: %s", mid, i, reason)
                continue
            dur = float(row.get("fight_dur") or row["clip"].get("input_duration") or 0)
            if dur < 1:
                dur = float(ffprobe_duration(part) or 0)
            gated.append(row)
            parts.append(part)
            durs.append(dur)
        if len(gated) < 3:
            report["error"] = f"gated_parts={len(gated)} (<3)"
            return report
        # Cap at 4 after gates.
        gated, parts, durs = gated[:4], parts[:4], durs[:4]
        if not concat_rendered_parts(parts, durs, out):
            report["error"] = "concat_fail"
            return report
        seg_dur = float(ffprobe_duration(out) or 0)
        banners = [
            f"{str(r.get('kill_banner') or 'KILL').upper()}@{int(float(r.get('peak_start') or 0))}"
            for r in gated
        ]
        caption = (
            f"MLBB склейка #{mid}\n"
            f"🎯 {' · '.join(banners)}\n"
            f"{vid} | {len(gated)} куска с одного VOD | {seg_dur:.0f}с\n"
            f"10–30м источник · anti-run trim · singles ok\n"
            f"👍 Ок / 👎 Не ок"
        )
        markup = inline_keyboard_markup(mid)
        deliver, is_temp = compress_for_inline_video(out, max_bytes=TELEGRAM_MAX_BYTES)
        try:
            if deliver.stat().st_size <= TELEGRAM_MAX_BYTES:
                sent = bool(
                    send_video_file(token, chat_id, deliver, caption, reply_markup=markup)
                )
            else:
                sent = bool(
                    send_document_file(token, chat_id, deliver, caption, reply_markup=markup)
                )
        finally:
            if is_temp:
                deliver.unlink(missing_ok=True)
        report["picked"] = [
            {
                "peak": r.get("peak_start"),
                "tier": r.get("kill_banner_tier"),
                "label": r.get("kill_banner"),
                "dur": r.get("fight_dur"),
            }
            for r in gated
        ]
        report["montage_id"] = mid
        report["duration"] = seg_dur
        report["ok"] = bool(sent)
        report["path"] = str(out)
        if sent:
            upsert_segment(
                {
                    "segment_id": mid,
                    "path": str(out),
                    "vod": str(vod),
                    "vod_id": vid,
                    "start": gated[0]["start"],
                    "duration": seg_dur,
                    "peak_start": gated[0].get("peak_start"),
                    "score": max(float(r.get("score") or 0) for r in gated),
                    "montage_parts": [r["segment_id"] for r in gated],
                    "note": "same_vod_montage_10_30m",
                    "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            mark_feed_sent([r["segment_id"] for r in gated] + [mid])
        return report
    finally:
        cleanup_temps(temps)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--count", type=int, default=3, help="How many montages to send")
    ap.add_argument("--min-sec", type=float, default=600.0, help="Min VOD duration (10m)")
    ap.add_argument("--max-sec", type=float, default=1800.0, help="Max VOD duration (30m)")
    ap.add_argument(
        "--inbox",
        type=Path,
        default=Path("/root/data/mlbb/youtube_nightly/inbox"),
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=Path("/root/datasets/mlbb/vod_segments"),
    )
    ap.add_argument(
        "--env",
        type=Path,
        default=Path("/root/.video_bot.env"),
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ids", nargs="*", help="Optional explicit youtube ids")
    args = ap.parse_args()

    env = _load_env(args.env)
    _prepare_montage_env()
    token = env.get("TG_BOT_TOKEN", "").strip()
    chat = (env.get("TG_CHAT_ID") or env.get("MLBB_CHAT_ID") or "").strip()
    if not args.dry_run and (not token or not chat):
        log.error("TG token/chat missing")
        return 2

    if args.ids:
        vods = []
        for vid in args.ids:
            p = args.inbox / f"yt_{vid}.mp4"
            if p.exists():
                vods.append(p)
            else:
                log.warning("missing vod %s", p)
    else:
        vods = _inbox_vods(args.inbox, min_sec=args.min_sec, max_sec=args.max_sec)
    log.info("candidate vods=%s (%.0f–%.0fs)", len(vods), args.min_sec, args.max_sec)
    if not vods:
        log.error("no VODs in duration window")
        return 1

    reports: list[dict] = []
    sent = 0
    for vod in vods:
        if sent >= args.count:
            break
        log.info("=== montage from %s (%.0fs) ===", vod.name, _ffprobe_duration(vod))
        if args.dry_run:
            rows = _discover_rows(vod)
            from mlbb_vod_montage import pick_montage_rows

            picked = pick_montage_rows(rows)
            reports.append(
                {
                    "vod_id": _vod_id(vod),
                    "hits": len(rows),
                    "picked": len(picked),
                    "peaks": [r.get("peak_start") for r in picked],
                    "dry_run": True,
                }
            )
            if len(picked) >= 3:
                sent += 1
            continue
        rep = _build_and_send_one(vod, token=token, chat_id=chat, out_dir=args.out_dir)
        reports.append(rep)
        if rep.get("ok"):
            sent += 1
            log.info("sent montage %s parts=%s", rep.get("montage_id"), len(rep.get("picked") or []))
        else:
            log.warning("skip vod=%s err=%s", rep.get("vod_id"), rep.get("error"))

    out_json = Path("/root/data/mlbb/savage_rescan/same_vod_montage_report.json")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("DONE sent=%s/%s report=%s", sent, args.count, out_json)
    print(json.dumps({"sent": sent, "want": args.count, "reports": reports}, ensure_ascii=False))
    return 0 if sent >= args.count else 1


if __name__ == "__main__":
    raise SystemExit(main())
