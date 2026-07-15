#!/usr/bin/env python3
"""
Smart owner teach: send frames that look like kill banners for owner labeling.

Order (fast → slow):
1. EDGE PASS — structural match to owner /banner screenshots (70+ crops)
2. OCR seeds around already-labeled positives (cheap direct OCR)
3. Optional OCR peak scan (slow; capped)

Never sends color-only / unverified gold-HUD spam.
"""

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
from mlbb_kill_banner import (
    KillBannerHit,
    _announce_color_score,
    _ffmpeg_sample_frames,
    _min_tier,
    classify_banner_text,
)
from mlbb_telegram_video import send_photo_file
from youtube_download import load_env

ENV_PATH = Path("/root/.video_bot.env")


def _read_frame(vod: Path, sec: float):
    from gameplay_gate import _read_frame_at

    return _read_frame_at(vod, sec)


def _ocr_confirm(vod: Path, sec: float, *, min_tier: int) -> KillBannerHit | None:
    from mlbb_kill_banner import _ocr_banner_zones

    frame = _read_frame(vod, sec)
    if frame is None:
        return None
    # Always deep — upscaled banner-zone OCR is what actually reads Double Kill
    text = _ocr_banner_zones(frame, deep=True)
    hit = classify_banner_text(text)
    if hit is None or int(hit.tier) < min_tier:
        return None
    return KillBannerHit(
        sec=round(float(sec), 2),
        tier=int(hit.tier),
        label=str(hit.label),
        text=str(hit.text or "")[:120],
        source="ocr",
    )


def _seeds_from_labels(inbox: Path) -> list[tuple[Path, float]]:
    seeds: list[tuple[Path, float]] = []
    max_seeds = int(os.environ.get("MLBB_SMART_TEACH_SEED_MAX", "12"))
    try:
        from mlbb_banner_calibration_store import load_labels

        rows = [
            row
            for row in load_labels().get("labels", [])
            if str(row.get("reason") or "")
            in {"own_kill_good", "double_triple", "savage_tier", "not_enemy_kill"}
            and not str(row.get("check_id") or "").startswith("ownerphoto")
        ]
        rows = list(reversed(rows))[: max_seeds // 2]
        for row in rows:
            vod = Path(str(row.get("vod") or ""))
            if not vod.exists():
                vid = str(row.get("check_id") or "").rsplit("_", 1)[0]
                for path in inbox.glob("yt_*.mp4"):
                    if vod_youtube_id(path).lower() == vid[:11].lower():
                        vod = path
                        break
            if not vod.exists():
                continue
            sec = float(row.get("sec") or 0)
            for delta in (-4.0, 6.0):
                seeds.append((vod, max(1.0, sec + delta)))
                if len(seeds) >= max_seeds:
                    return seeds
    except Exception as exc:
        print(f"seed_load_fail: {exc}", flush=True)
    return seeds


def _send_one(
    *,
    token: str,
    chat_id: str,
    vod: Path,
    hit: KillBannerHit,
    cid: str,
    sent_n: int,
    max_send: int,
    target: int,
    score: float,
) -> bool:
    try:
        shot, meta = render_check_screenshot(vod, hit.sec, hit=hit)
    except Exception as exc:
        print(f"capture_fail {cid}: {exc}", flush=True)
        return False
    st = stats()
    caption = (
        f"✅ Кандидат {sent_n + 1}/{max_send} | {st['labeled']}/{target}\n"
        f"{hit.label.upper()} t{hit.tier} score={score:.1f}\n"
        f"{meta.get('vod_id', '')} @ {hit.sec:.1f}s\n"
        f"#{cid}\n"
        f"источник: {hit.source} | {(hit.text or '')[:60]}\n"
        f"Жми ✅/⚡/🔥 если kill-банер виден — или ❌ Нет банера"
    )
    if send_photo_file(token, chat_id, shot, caption, reply_markup=inline_keyboard_markup(cid)):
        mark_sent([cid])
        print(f"sent {cid} {hit.label} t{hit.tier} src={hit.source}", flush=True)
        return True
    return False


def main() -> int:
    env = {**os.environ, **load_env(ENV_PATH)}
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("missing telegram creds")
        return 1

    max_send = int(os.environ.get("MLBB_BANNER_FLOOD_MAX", os.environ.get("MLBB_SMART_TEACH_MAX", "12")))
    target = calibration_target()
    min_tier = max(_min_tier(), int(os.environ.get("MLBB_SMART_TEACH_MIN_TIER", str(_min_tier()))))
    inbox = Path(os.environ.get("MLBB_VOD_INBOX", "/root/data/mlbb/youtube_nightly/inbox"))
    labeled = labeled_ids()
    sent = load_sent()
    seen: set[str] = set(labeled) | set(sent)
    sent_n = 0
    delay = float(os.environ.get("MLBB_BANNER_FLOOD_DELAY_SEC", "0.25"))
    global_deadline = time.time() + float(os.environ.get("MLBB_SMART_TEACH_MAX_SEC", "480"))
    vods = sorted(inbox.glob("yt_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)

    print(
        json.dumps(
            {"smart_teach_start": True, "max_send": max_send, "min_tier": min_tier, "labeled": len(labeled)},
            ensure_ascii=False,
        ),
        flush=True,
    )

    # ── Pass 1: EDGE (uses owner /banner screenshots) ─────────────────────
    if os.environ.get("MLBB_SMART_TEACH_EDGE_PASS", "1") == "1":
        from mlbb_banner_ref_match import (
            clear_banner_ref_cache,
            match_positive_owner_reference_strict,
            patch_edge_similarity,
            extract_banner_zone_patch,
            _load_positive_owner_ref_rows,
            _ref_patch_cached,
        )

        clear_banner_ref_cache()
        edge_vods = int(os.environ.get("MLBB_SMART_TEACH_EDGE_VODS", "10"))
        edge_samples = int(os.environ.get("MLBB_SMART_TEACH_EDGE_SAMPLES", "18"))
        edge_min = float(os.environ.get("MLBB_SMART_TEACH_EDGE_MIN", "0.34"))
        # Preload a handful of owner refs for per-frame max edge (faster than full strict for ranking)
        ref_rows = _load_positive_owner_ref_rows()[:40]
        print(f"edge_pass vods={edge_vods} samples={edge_samples} min={edge_min} refs={len(ref_rows)}", flush=True)

        for i, vod in enumerate(vods[:edge_vods]):
            if sent_n >= max_send or time.time() > global_deadline:
                break
            print(f"edge_scan {vod_youtube_id(vod)} ({i+1}/{edge_vods}) sent={sent_n}", flush=True)
            frames = _ffmpeg_sample_frames(
                vod,
                float(os.environ.get("MLBB_SMART_TEACH_T0", "60")),
                float(os.environ.get("MLBB_SMART_TEACH_T1", "1400")),
                edge_samples,
            )
            scored: list[tuple[float, float, object]] = []
            for sec, low in frames:
                if _announce_color_score(low) < float(os.environ.get("MLBB_SMART_TEACH_COLOR_HINT", "0.04")):
                    continue
                patch = extract_banner_zone_patch(low)
                if patch is None:
                    continue
                best_edge = 0.0
                for path, _reason, _tag in ref_rows:
                    ref = _ref_patch_cached(path)
                    if ref is None:
                        continue
                    best_edge = max(best_edge, patch_edge_similarity(patch, ref))
                if best_edge >= edge_min * 0.85:  # low-res prefilter slightly softer
                    scored.append((best_edge, float(sec), low))
            scored.sort(reverse=True)
            for best_edge, sec, _low in scored[:6]:
                if sent_n >= max_send or time.time() > global_deadline:
                    break
                cid = check_id(vod, sec)
                if cid in seen:
                    continue
                frame = _read_frame(vod, sec)
                if frame is None:
                    continue
                # Full-res confirm
                ocr_hit = _ocr_confirm(vod, sec, min_tier=min_tier)
                edge_row = match_positive_owner_reference_strict(frame)
                # Never send edge-only — UI FX false-positives (combat flash / chat).
                # Edge boosts ranking of OCR hits; OCR text is mandatory to send.
                if ocr_hit is None:
                    continue
                hit = ocr_hit
                score = float(hit.tier) * 3.0 + (float(edge_row[0]) * 4.0 if edge_row else 0.0)
                seen.add(cid)
                if _send_one(
                    token=token,
                    chat_id=chat_id,
                    vod=vod,
                    hit=hit,
                    cid=cid,
                    sent_n=sent_n,
                    max_send=max_send,
                    target=target,
                    score=score,
                ):
                    sent_n += 1
                    time.sleep(delay)

    # ── Pass 2: OCR seeds (optional, short) ───────────────────────────────
    if sent_n < max_send and os.environ.get("MLBB_SMART_TEACH_SEED_PASS", "1") == "1":
        seeds = _seeds_from_labels(inbox)
        print(f"seed_probes={len(seeds)}", flush=True)
        for si, (vod, peak) in enumerate(seeds, start=1):
            if sent_n >= max_send or time.time() > global_deadline:
                break
            if si == 1 or si % 5 == 0:
                print(f"seed {si}/{len(seeds)} {vod_youtube_id(vod)}@{peak:.0f}", flush=True)
            hit = _ocr_confirm(vod, peak, min_tier=min_tier)
            if hit is None:
                continue
            cid = check_id(vod, hit.sec)
            if cid in seen:
                continue
            seen.add(cid)
            if _send_one(
                token=token,
                chat_id=chat_id,
                vod=vod,
                hit=hit,
                cid=cid,
                sent_n=sent_n,
                max_send=max_send,
                target=target,
                score=float(hit.tier) * 3.0 + 2.0,
            ):
                sent_n += 1
                time.sleep(delay)

    print(json.dumps({"sent": sent_n, "stats": stats()}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
