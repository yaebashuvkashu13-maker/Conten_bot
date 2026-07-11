#!/usr/bin/env python3
"""Send high-confidence kill-banner screenshots for owner positive labeling."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_banner_calibration_capture import render_check_screenshot
from mlbb_banner_calibration_reasons import inline_keyboard_markup
from mlbb_banner_calibration_store import (
    calibration_target,
    check_id,
    labeled_ids,
    load_sent,
    mark_sent,
    stats,
    vod_youtube_id,
)
from mlbb_kill_banner import KillBannerHit, find_banner_near_peak, scan_window
from mlbb_telegram_video import send_photo_file
from youtube_download import load_env

ENV_PATH = Path("/root/.video_bot.env")


def _resolve_vod(inbox: Path, vid: str) -> Path | None:
    direct = inbox / f"yt_{vid[:11]}.mp4"
    if direct.exists():
        return direct
    low = vid[:11].lower()
    for path in inbox.glob("yt_*.mp4"):
        if vod_youtube_id(path).lower() == low:
            return path
    return None


def _read_frame(vod: Path, sec: float):
    from gameplay_gate import _read_frame_at

    return _read_frame_at(vod, sec)


def hit_from_check_row(row: dict) -> KillBannerHit:
    tier = row.get("banner_tier")
    return KillBannerHit(
        sec=float(row.get("sec", 0)),
        tier=int(tier) if tier is not None else 0,
        label=str(row.get("banner_label") or ""),
        text=str(row.get("detected_text") or ""),
        source=str(row.get("banner_source") or "index"),
    )


def positive_candidate_ok(hit: KillBannerHit, frame, *, vod: Path | None = None) -> bool:
    """
    Owner positive feed: only OCR-confirmed or owner-good patches.
    Ref-only vod_crop/wiki histogram matches are too noisy.
    """
    if frame is None:
        return False
    try:
        from mlbb_banner_ref_match import (
            match_negative_banner_reference,
            match_positive_owner_reference,
        )
        from mlbb_kill_banner import _ocr_banner_zones, classify_banner_text
    except ImportError:
        return hit.source == "ocr"

    if match_negative_banner_reference(frame) is not None:
        return False
    if hit.source in ("ocr", "owner"):
        accepted = True
    elif match_positive_owner_reference(frame) is not None:
        accepted = True
    elif hit.source in ("segment", "audit", "index", "burst"):
        ocr = classify_banner_text(_ocr_banner_zones(frame, deep=True))
        accepted = ocr is not None and int(ocr.tier) >= max(2, int(hit.tier) - 1)
    elif hit.source == "ref":
        return False
    else:
        ocr = classify_banner_text(_ocr_banner_zones(frame, deep=True))
        accepted = ocr is not None and int(ocr.tier) >= max(2, int(hit.tier) - 1)

    if not accepted:
        return False

    if vod is not None and os.environ.get("MLBB_BANNER_POS_POV_MATCH", "1") == "1":
        try:
            from mlbb_banner_pov_match import banner_pov_hero_match

            pov_ok, _pov_reason, _sim = banner_pov_hero_match(vod, hit.sec)
            if not pov_ok:
                return False
        except Exception:
            pass
    return True


def verified_before_send(
    vod: Path,
    hit: KillBannerHit,
    frame=None,
) -> tuple[bool, str]:
    """Final gate before Telegram send — must pass owner negatives + OCR/pos."""
    if frame is None:
        frame = _read_frame(vod, hit.sec)
    if frame is None:
        return False, "no_frame"
    if not positive_candidate_ok(hit, frame, vod=vod):
        return False, "candidate_filter"
    if os.environ.get("MLBB_BANNER_SEND_STRICT", "1") == "1":
        try:
            from mlbb_banner_calibration_gate import check_banner_frame

            decision, reason = check_banner_frame(frame, tier=int(hit.tier))
            if decision == "reject":
                return False, reason
        except ImportError:
            pass
    return True, "ok"


def _score_candidate(hit: KillBannerHit, frame) -> float:
    """Higher = bot more confident this is a good kill banner."""
    if frame is None or not positive_candidate_ok(hit, frame):
        return -1.0
    try:
        from mlbb_banner_ref_match import (
            match_banner_reference,
            match_negative_banner_reference,
            match_positive_owner_reference,
        )
    except ImportError:
        return float(hit.tier)

    if match_negative_banner_reference(frame) is not None:
        return -1.0

    score = float(hit.tier) * 2.0
    label = str(hit.label or "").lower()
    if label in ("savage", "legendary"):
        score += 6.0
    elif label in ("maniac", "triple"):
        score += 4.0
    elif label in ("double",):
        score += 2.0
    if hit.source == "ocr":
        score += 3.0
    elif hit.source == "ref":
        score += 2.5
    pos = match_positive_owner_reference(frame)
    if pos is not None:
        score += pos[0] * 4.0
    ref = match_banner_reference(frame)
    if ref is not None:
        score += ref[0] * 3.0
    return score


def _from_hit(vod: Path, hit: KillBannerHit, *, source: str = "bot") -> tuple[Path, KillBannerHit, str, float]:
    cid = check_id(vod, hit.sec)
    tagged = KillBannerHit(
        sec=hit.sec,
        tier=hit.tier,
        label=hit.label,
        text=hit.text,
        source=source or hit.source,
    )
    frame = _read_frame(vod, hit.sec)
    return vod, tagged, cid, _score_candidate(tagged, frame)


def _audit_candidates(inbox: Path, labeled: dict, sent: set, *, min_tier: int) -> list[tuple[Path, KillBannerHit, str, float]]:
    path = Path(os.environ.get("MLBB_DENSE_AUDIT_JSON", "/root/data/mlbb/dense_audit_2026-07-08.json"))
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[tuple[Path, KillBannerHit, str, float]] = []
    for block in data.get("vods", []):
        vid = str(block.get("vod") or "").replace("yt_", "").replace(".mp4", "")
        vod = _resolve_vod(inbox, vid)
        if vod is None:
            continue
        for row in block.get("banner_times") or []:
            tier = int(row.get("tier") or 0)
            if tier < min_tier:
                continue
            sec = float(row["sec"])
            cid = check_id(vod, sec)
            if cid in labeled:
                continue
            hit = KillBannerHit(
                sec=sec,
                tier=tier,
                label=str(row.get("label") or "savage"),
                text=str(row.get("text") or "")[:80],
                source=str(row.get("source") or "audit"),
            )
            frame = _read_frame(vod, sec)
            score = _score_candidate(hit, frame)
            if score < 0:
                continue
            out.append((vod, hit, cid, score))
    return out


def _segment_candidates(inbox: Path, labeled: dict, sent: set, *, min_tier: int) -> list[tuple[Path, KillBannerHit, str, float]]:
    idx_path = Path(os.environ.get("MLBB_VOD_SEGMENT_INDEX", "/root/data/mlbb/vod_segment_index.json"))
    if not idx_path.exists():
        return []
    data = json.loads(idx_path.read_text(encoding="utf-8"))
    out: list[tuple[Path, KillBannerHit, str, float]] = []
    for row in data.get("segments", []):
        tier = int(row.get("kill_banner_tier") or 0)
        label = str(row.get("kill_banner") or "").lower()
        if tier <= 0 and label in ("savage", "legendary", "maniac", "triple"):
            tier = {"savage": 5, "legendary": 5, "maniac": 4, "triple": 3}.get(label, 0)
        clip_score = float(row.get("clip_score") or row.get("score") or 0)
        if tier < min_tier and clip_score < float(os.environ.get("MLBB_POS_CAL_MIN_CLIP", "0.22")):
            continue
        vod_path = str(row.get("vod") or "")
        vod = Path(vod_path) if vod_path and Path(vod_path).exists() else None
        if vod is None:
            vid = str(row.get("vod_id") or str(row.get("segment_id", "")).rsplit("_", 1)[0])
            vod = _resolve_vod(inbox, vid)
        if vod is None:
            continue
        sec = float(row.get("peak_start") if row.get("peak_start") is not None else row.get("start") or 0)
        cid = check_id(vod, sec)
        if cid in labeled:
            continue
        hit = find_banner_near_peak(vod, sec, quick=True)
        if hit is None:
            hit = KillBannerHit(
                sec=sec,
                tier=max(tier, 3),
                label=label or "segment",
                text="segment_peak",
                source="segment",
            )
        if int(hit.tier) < min_tier:
            continue
        frame = _read_frame(vod, hit.sec)
        if not positive_candidate_ok(hit, frame, vod=vod):
            continue
        score = _score_candidate(hit, frame) + clip_score * 2.0
        if score < 0:
            continue
        out.append((vod, hit, cid, score))
    return out


def _peak_scan_candidates(
    inbox: Path,
    labeled: dict,
    sent: set,
    *,
    min_tier: int,
    vod_limit: int,
) -> list[tuple[Path, KillBannerHit, str, float]]:
    from mlbb_kill_banner import _ffmpeg_sample_frames, _classify_frame

    skip_ids = set(labeled.keys())
    vods = sorted(inbox.glob("yt_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[tuple[Path, KillBannerHit, str, float]] = []
    vods_scanned = 0
    for vod in vods:
        frames = _ffmpeg_sample_frames(
            vod,
            float(os.environ.get("MLBB_POS_CAL_T0", "90")),
            float(os.environ.get("MLBB_POS_CAL_T1", "1200")),
            int(os.environ.get("MLBB_POS_CAL_SAMPLES", "14")),
        )
        found_on_vod = 0
        for sec, frame in frames:
            hit = _classify_frame(sec, frame)
            if hit is None or int(hit.tier) < min_tier:
                continue
            cid = check_id(vod, hit.sec)
            if cid in skip_ids or cid in {x[2] for x in out}:
                continue
            score = _score_candidate(hit, frame)
            if score < float(os.environ.get("MLBB_POS_CAL_MIN_SCORE", "6")):
                continue
            out.append((vod, hit, cid, score))
            found_on_vod += 1
        if found_on_vod > 0 or True:
            vods_scanned += 1
        if vods_scanned >= vod_limit:
            break
    return out


def collect_positive_candidates(*, limit: int) -> list[tuple[Path, KillBannerHit, str, float]]:
    inbox = Path(os.environ.get("MLBB_VOD_INBOX", "/root/data/mlbb/youtube_nightly/inbox"))
    labeled = labeled_ids()
    skip_sent = os.environ.get("MLBB_POS_CAL_SKIP_SENT", "0") == "1"
    sent = load_sent() if skip_sent else set()
    min_tier = int(os.environ.get("MLBB_POS_CAL_MIN_TIER", "4"))

    merged: dict[str, tuple[Path, KillBannerHit, str, float]] = {}
    for source in (
        _segment_candidates(inbox, labeled, sent, min_tier=min_tier),
        _audit_candidates(inbox, labeled, sent, min_tier=min_tier),
        _peak_scan_candidates(inbox, labeled, sent, min_tier=min_tier, vod_limit=int(os.environ.get("MLBB_POS_CAL_VODS", "8"))),
    ):
        for vod, hit, cid, score in source:
            prev = merged.get(cid)
            if prev is None or score > prev[3]:
                merged[cid] = (vod, hit, cid, score)

    rows = sorted(merged.values(), key=lambda x: -x[3])
    return rows[:limit]


def main() -> int:
    env = {**os.environ, **load_env(ENV_PATH)}
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("missing telegram creds")
        return 1

    batch = int(os.environ.get("MLBB_POS_CAL_BATCH", "12"))
    candidates = collect_positive_candidates(limit=batch)
    print(json.dumps({"candidates": len(candidates), "top": [(c[2], round(c[3], 2)) for c in candidates[:8]]}, ensure_ascii=False))

    sent_n = 0
    target = calibration_target()
    for i, (vod, hit, cid, score) in enumerate(candidates, start=1):
        frame = _read_frame(vod, hit.sec)
        ok, why = verified_before_send(vod, hit, frame)
        if not ok:
            print(f"skip_send {cid}: {why}")
            continue
        try:
            shot, meta = render_check_screenshot(vod, hit.sec, hit=hit)
        except Exception as exc:
            print(f"capture_fail {cid}: {exc}")
            continue
        st = stats()
        caption = (
            f"✅ Кандидат {i}/{len(candidates)} | размечено {st['labeled']}/{target}\n"
            f"бот: {hit.label} tier={hit.tier} score={score:.1f}\n"
            f"{meta.get('vod_id', '')} @ {hit.sec:.1f}s\n"
            f"#{cid}\n"
            f"Если ок — жми ✅ Свой kill / 🔥 Savage / ⚡ Double-Triple"
        )
        if send_photo_file(token, chat_id, shot, caption, reply_markup=inline_keyboard_markup(cid)):
            mark_sent([cid])
            sent_n += 1
            print(f"sent {cid} score={score:.1f}")
        time.sleep(float(os.environ.get("MLBB_POS_CAL_DELAY_SEC", "0.3")))

    print(json.dumps({"sent": sent_n, "stats": stats()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
