#!/usr/bin/env python3
"""Anti-silence hunt: live double+ or ≥2-clip montage until daily MLBB quota closes.

Loops while remaining>0. Quality floors stay on — no solo HAS-SLAIN junk.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
os.chdir(SCRIPTS)


def _load_env() -> None:
    env_path = Path("/root/.video_bot.env")
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip().strip("\"'")


def _hunt_knobs() -> None:
    os.environ.update(
        {
            "MLBB_KILL_BANNER_DISCOVER_MAX_SEC": "200",
            "MLBB_KILL_BANNER_DISCOVER_MAX_PROBES": "40",
            "MLBB_KILL_BANNER_DISCOVER_TARGET": "2",
            "MLBB_KILL_BANNER_DISCOVER_MIN_HITS": "1",
            "MLBB_KILL_BANNER_DISCOVER_MERGE_TIER": "1",
            "MLBB_DISCOVER_SHIP_ON_FIRST": "0",
            "MLBB_DISCOVER_SHIP_ON_FIRST_DOUBLE": "1",
            "MLBB_BANNER_FIGHT_FIRST": "1",
            "MLBB_BANNER_FIGHT_FIRST_PEAKS": "12",
            "MLBB_FIGHT_FIRST_BUCKET_SEC": "90",
            "MLBB_KILL_BANNER_DISCOVER_PEAK_BUDGET_FRAC": "0.65",
            "MLBB_KILL_BANNER_DISCOVER_PEAK_HINTS": "12",
            "MLBB_FIGHT_FIRST_KILL_RICH_SPIKE_SEC": "50",
            "MLBB_FIGHT_FIRST_KILL_RICH_SPIKE_PROBES": "10",
            "MLBB_BANNER_FIGHT_DENSE_SCAN": "1",
            "MLBB_BANNER_FLASH_RANK": "1",
            "MLBB_KILL_BANNER_FIGHT_FPS": "4",
            "MLBB_KILL_BANNER_FIGHT_BEFORE": "1.5",
            "MLBB_KILL_BANNER_FIGHT_AFTER": "12",
            "MLBB_KILL_BANNER_FIGHT_MAX_FRAMES": "48",
            "MLBB_KILL_BANNER_FIGHT_MAX_CLASSIFY": "6",
            "MLBB_DISCOVER_OCR_CALL_BUDGET": "12",
            "MLBB_VOD_TITLE_DENSE_AUTO": "0",
            "MLBB_VOD_BANNER_DENSE_SEC": "0",
            "MLBB_VOD_DISCOVER_ALWAYS_DENSE": "0",
            "MLBB_BANNER_OWN_KILL_REQUIRED": "1",
            "MLBB_BANNER_OWN_HUD_MIN_SIM": "0.22",
            "MLBB_BANNER_DISCOVER_OWN_HUD_MIN_SIM": "0.17",
            "MLBB_PRESEND_OWN_KILL_SINGLE_HUD_MIN": "0.28",
            "MLBB_OCR_MULTI_TRUST_OWN_KILL": "0",
            "MLBB_PRESEND_MULTI_ALLOW_HUD_MISS": "0",
            "MLBB_BANNER_SEND_MIN_TIER": "double",
            "MLBB_SOLO_REQUIRE_LIVE_MULTI": "1",
            # Gold-blind OCR: strong labeled ref may solo-ship (double under silence).
            "MLBB_SOLO_ALLOW_STRONG_REF": "1",
            "MLBB_SOLO_STRONG_REF_HUD_MIN": "0.50",
            "MLBB_SOLO_STRONG_REF_DOUBLE": "1",
            "MLBB_SOLO_STRONG_REF_DOUBLE_HUD_MIN": "0.55",
            "MLBB_PRESEND_OWN_KILL_SINGLE": "0",
            "MLBB_VOD_MONTAGE": "1",
            "MLBB_VOD_MONTAGE_ALLOW_SINGLES": "1",
            "MLBB_VOD_MONTAGE_MIN_CLIPS": "2",
            "MLBB_PRESEND_MIN_BANNER_SEC": "90",
            "MLBB_BANNER_DISCOVER_EXCLUDE_SECS": "",
            "MLBB_BANNER_DISCOVER_EXCLUDE_GAP_SEC": "3",
            "MLBB_DISCOVER_ALLOW_NEAR_SENT": "1",
            "MLBB_BANNER_DISCOVER_REF_COLOR_MUL": "0.35",
            "MLBB_BANNER_DISCOVER_COLOR_GATE_MAX": "0.010",
            "SMART_ANALYSIS_DETAIL": "fast",
            "SMART_SAMPLE_FPS": "2.0",
            "HIGHLIGHT_CLIP_DISABLED": "1",
        }
    )


def _candidate_vods() -> list[Path]:
    inbox = Path("/root/data/mlbb/youtube_nightly/inbox")
    root = Path("/root/data/mlbb/youtube_nightly")
    prefer = [
        inbox / "yt_-kOfd_sctHY.mp4",
        inbox / "yt_8pbqKzd9Xzc.mp4",
        inbox / "yt_BM1CjA5jPLk.mp4",
        inbox / "yt_Cv7Ul8t6j6s.mp4",
        inbox / "yt_MzIiYgWelYM.mp4",
        inbox / "yt_ezW8Vn4pNbw.mp4",
        inbox / "yt_-y1tsLsKrn4.mp4",
        inbox / "yt_5BAG79_pu7w.mp4",
        inbox / "yt_nFf2MUWL7io.mp4",
        inbox / "yt_EGM-y0c2WIQ.mp4",
        inbox / "yt_TdZrmr_QHKg.mp4",
        inbox / "yt_O4CEirbntYY.mp4",
    ]
    # Draft trash / already-duplicated singles / barren crash-loop VOD.
    skip = {"UGu-LYZ-GLY", "Y3In5vMdlak", "B9L4ETvZwMo", "8LNjsK7IzCY"}
    skip_dirs = {"hold_barren", "hold_quota", "park_dead", "exhausted", "hold"}
    out: list[Path] = []
    for p in prefer:
        if p.exists() and p.stat().st_size > 50_000_000:
            out.append(p)
    for p in sorted(inbox.glob("yt_*.mp4"), key=lambda x: -x.stat().st_mtime):
        if ".part" in p.name or p.stat().st_size < 80_000_000:
            continue
        vid = p.stem.replace("yt_", "")
        if vid in skip:
            continue
        if p not in out:
            out.append(p)
        if len(out) >= 10:
            break
    # Only fall back to non-barren dirs under youtube_nightly.
    if len(out) < 6:
        for p in sorted(root.rglob("yt_*.mp4"), key=lambda x: -x.stat().st_mtime):
            if ".part" in p.name or p.stat().st_size < 80_000_000:
                continue
            if any(part in skip_dirs for part in p.parts):
                continue
            vid = p.stem.replace("yt_", "")
            if vid in skip:
                continue
            if p not in out:
                out.append(p)
            if len(out) >= 10:
                break
    return out


def _hit_to_row(vod: Path, hit, *, normalize_clip, youtube_id) -> dict:
    sec = float(hit.sec)
    tier = int(hit.tier)
    label = getattr(hit, "label", None) or str(tier)
    src = getattr(hit, "source", "") or ""
    own = (
        getattr(hit, "own_kill_reason", None)
        or getattr(hit, "own_reason", None)
        or ""
    )
    vid = youtube_id(vod)
    sid = f"{vid}_{int(sec)}"
    lead = float(os.environ.get("MLBB_KILL_BANNER_LEAD_SEC", "8"))
    post = 4.0 if tier >= 2 else 2.5
    start = max(0.0, sec - lead)
    dur = lead + post
    clip = normalize_clip(
        {
            "start": start,
            "input_duration": dur,
            "peak_start": sec,
            "banner_sec": sec,
            "kill_banner": label,
            "kill_banner_tier": tier,
            "kill_banner_text": str(getattr(hit, "text", "") or ""),
            "banner_text": str(getattr(hit, "text", "") or ""),
            "banner_source": src,
            "own_kill_reason": own,
            "anchor": "kill_banner",
        },
        vod,
    )
    return {
        "segment_id": sid,
        "start": float(clip["start"]),
        "peak_start": float(clip.get("peak_start", sec)),
        "banner_sec": float(clip.get("banner_sec", sec)),
        "fight_dur": float(clip.get("input_duration") or dur),
        "score": 1.0,
        "hook_score": 1.0,
        "clip_score": 1.0,
        "kill_banner": label,
        "kill_banner_tier": tier,
        "kill_banner_text": str(getattr(hit, "text", "") or ""),
        "banner_text": str(getattr(hit, "text", "") or ""),
        "banner_source": src,
        "own_kill_reason": own,
        "own_kill_recheck": own,
        "anchor": "kill_banner",
        "clip": clip,
    }


def _mlbb_remaining() -> int:
    try:
        from daily_game_cycle import status_summary

        rem = (status_summary() or {}).get("remaining") or {}
        return int(rem.get("mlbb") or 0)
    except Exception:
        return 0


def _hunt_once() -> bool:
    """Return True if at least one clip was sent."""
    from daily_game_cycle import status_summary
    from mlbb_kill_banner import discover_vod_kill_banners
    from mlbb_vod_montage import build_montage_id, concat_rendered_parts
    from mlbb_vod_segment_feed import (
        _normalize_clip,
        _validate_before_send,
        mark_feed_sent,
        render_single_segment,
        segments_root,
        send_video,
        vod_youtube_id,
    )

    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TG_CHAT_ID", "").strip()
    print("tg", bool(token), bool(chat_id), "quota", status_summary(), flush=True)
    if not token or not chat_id:
        print("missing TG creds", flush=True)
        return False

    vods = _candidate_vods()
    print("vods", [v.name for v in vods], flush=True)
    sent_ok = False

    for vod in vods:
        if _mlbb_remaining() <= 0:
            print("quota full — stop hunt pass", flush=True)
            break
        print(f"\n==== DISCOVER {vod.name} size={vod.stat().st_size}", flush=True)
        t0 = time.time()
        try:
            hits = discover_vod_kill_banners(vod)
        except FileNotFoundError:
            print("missing vod (moved/pruned)", vod, flush=True)
            continue
        except Exception as exc:
            print("discover fail", exc, flush=True)
            continue
        print("elapsed", round(time.time() - t0, 1), "hits", len(hits), flush=True)
        for h in hits:
            print(
                " HIT",
                getattr(h, "sec", None),
                "tier",
                getattr(h, "tier", None),
                "src",
                getattr(h, "source", None),
                "own",
                getattr(h, "own_kill_reason", None) or getattr(h, "own_reason", None),
                "label",
                getattr(h, "label", None),
                flush=True,
            )

        doubles = [h for h in hits if int(getattr(h, "tier", 0) or 0) >= 2 and float(h.sec) >= 90]
        singles = [h for h in hits if int(getattr(h, "tier", 0) or 0) == 1 and float(h.sec) >= 90]
        candidates: list[tuple[str, list]] = []
        if len(doubles) >= 2:
            candidates.append(
                ("montage_doubles", sorted(doubles, key=lambda x: x.sec)[:3])
            )
        if doubles:
            candidates.append(("solo_double", doubles[:1]))
        if len(singles) >= 2:
            candidates.append(("montage_singles", sorted(singles, key=lambda x: x.sec)[:3]))
        if not candidates:
            print("no shippable candidates", flush=True)
            continue

        for kind, hs in candidates:
            print("try", kind, [(h.sec, h.tier) for h in hs], flush=True)
            rows = [
                _hit_to_row(vod, h, normalize_clip=_normalize_clip, youtube_id=vod_youtube_id)
                for h in hs
            ]
            if kind == "solo_double":
                row = rows[0]
                out = segments_root() / f"seg_{row['segment_id']}_hunt.mp4"
                segments_root().mkdir(parents=True, exist_ok=True)
                if not render_single_segment(vod, row["clip"], out):
                    print("render fail", row["segment_id"], flush=True)
                    continue
                ok, reason, report = _validate_before_send(vod, row, out)
                live_tier = int((report or {}).get("live_streak_tier") or 0)
                print(
                    "validate",
                    ok,
                    reason,
                    {
                        "own": (report or {}).get("own_kill_recheck"),
                        "live_tier": live_tier,
                        "strong_ref": (report or {}).get("solo_strong_ref"),
                        "live": str((report or {}).get("live_overlay") or "")[:80],
                    },
                    flush=True,
                )
                if not ok:
                    continue
                if live_tier < 2 and not (report or {}).get("solo_strong_ref"):
                    print("skip live_tier", live_tier, flush=True)
                    continue
                banner = str(row.get("kill_banner") or "").upper()
                peak = int(row["peak_start"])
                caption = (
                    f"MLBB #{row['segment_id']}\n"
                    f"🎯 {banner}@{peak}\n"
                    f"{vod_youtube_id(vod)} | hunt live_tier={live_tier}\n"
                    f"✓ quality hunt (anti-silence)\n"
                    f"👍 Ок / 👎 Не ок"
                )
                if not send_video(token, chat_id, out, caption, seg_id=row["segment_id"]):
                    print("send_video failed", flush=True)
                    continue
                mark_feed_sent([row["segment_id"]])
                with open("/root/data/mlbb/mlbb_vod_sent.jsonl", "a", encoding="utf-8") as fh:
                    fh.write(
                        json.dumps(
                            {
                                "ts": time.time(),
                                "video_id": vod_youtube_id(vod),
                                "segment": row["segment_id"],
                                "kind": "mlbb_quota_hunt",
                                "game": "mlbb",
                                "live_tier": live_tier,
                                "note": "anti-silence hunt",
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                print("SENT", row["segment_id"], flush=True)
                sent_ok = True
                break
            if not kind.startswith("montage") or len(rows) < 2:
                continue
            temps: list[Path] = []
            durs: list[float] = []
            gated: list[dict] = []
            for row in rows:
                part = Path(tempfile.mkstemp(suffix=".part.mp4")[1])
                temps.append(part)
                if not render_single_segment(vod, row["clip"], part):
                    print("part render fail", row["segment_id"], flush=True)
                    continue
                prev = os.environ.get("MLBB_PRESEND_MONTAGE_SINGLE")
                os.environ["MLBB_PRESEND_MONTAGE_SINGLE"] = "1"
                try:
                    ok, reason, _rep = _validate_before_send(vod, row, part)
                finally:
                    if prev is None:
                        os.environ.pop("MLBB_PRESEND_MONTAGE_SINGLE", None)
                    else:
                        os.environ["MLBB_PRESEND_MONTAGE_SINGLE"] = prev
                print("part", row["segment_id"], ok, reason, flush=True)
                if not ok:
                    continue
                gated.append(row)
                durs.append(float(row["fight_dur"]))
            if len(gated) < 2:
                print("montage gated <2", flush=True)
                continue
            mid = build_montage_id(vod_youtube_id(vod), gated)
            out = segments_root() / f"seg_{mid}_hunt.mp4"
            if not concat_rendered_parts(temps[: len(gated)], durs, out):
                print("concat fail", flush=True)
                continue
            banners = " · ".join(
                f"{str(r.get('kill_banner') or '').upper()}@{int(r['peak_start'])}"
                for r in gated
            )
            caption = (
                f"MLBB склейка #{mid}\n"
                f"🎯 {banners}\n"
                f"{vod_youtube_id(vod)} | {len(gated)} куска | hunt\n"
                f"✓ montage (anti-silence)\n"
                f"👍 Ок / 👎 Не ок"
            )
            if not send_video(token, chat_id, out, caption, seg_id=mid):
                print("send montage failed", flush=True)
                continue
            mark_feed_sent([r["segment_id"] for r in gated] + [mid])
            with open("/root/data/mlbb/mlbb_vod_sent.jsonl", "a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "ts": time.time(),
                            "video_id": vod_youtube_id(vod),
                            "segment": mid,
                            "kind": "mlbb_quota_hunt_montage",
                            "game": "mlbb",
                            "note": "anti-silence continuous hunt",
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            print("SENT montage", mid, "quota", status_summary(), flush=True)
            sent_ok = True
            break
        if sent_ok:
            break

    print("PASS done sent_ok", sent_ok, "quota", status_summary(), flush=True)
    return sent_ok


def main() -> int:
    _load_env()
    _hunt_knobs()

    poll = max(20, int(os.environ.get("MLBB_HUNT_POLL_SEC", "45")))
    silence = max(60, int(os.environ.get("MLBB_HUNT_SILENCE_SEC", "480")))
    # After a successful send, wait this long before next pass (tempo).
    after_send = max(30, int(os.environ.get("MLBB_HUNT_AFTER_SEND_SEC", "90")))
    # One-shot mode for tests / manual: MLBB_HUNT_ONCE=1
    once = os.environ.get("MLBB_HUNT_ONCE", "0") == "1"

    passes = 0
    while True:
        rem = _mlbb_remaining()
        if rem <= 0:
            print("mlbb remaining=0 — hunt idle exit", flush=True)
            return 0
        print(
            f"\n##### HUNT PASS {passes + 1} remaining={rem} silence_gate={silence}s",
            flush=True,
        )
        # Mark silence mode so solo strong-ref doubles may ship when OCR is gold-blind.
        os.environ["MLBB_SOLO_STRONG_REF_DOUBLE"] = "1"
        os.environ["MLBB_VOD_DISCOVER_MISS_STREAK"] = str(
            max(3, int(os.environ.get("MLBB_VOD_DISCOVER_MISS_STREAK", "0") or 0))
        )
        sent = _hunt_once()
        passes += 1
        if once:
            return 0 if sent else 1
        if sent:
            rem = _mlbb_remaining()
            if rem <= 0:
                print("quota closed after send — exit", flush=True)
                return 0
            print(f"sent ok — sleep {after_send}s before next pass", flush=True)
            time.sleep(after_send)
            continue
        print(f"no send this pass — sleep {poll}s", flush=True)
        time.sleep(poll)


if __name__ == "__main__":
    raise SystemExit(main())
