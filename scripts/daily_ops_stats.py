#!/usr/bin/env python3
"""Gather live daily-ops facts for morning/evening Telegram reports."""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")

GAME_ORDER = ("mlbb", "pubg", "standoff", "genshin", "wot")
GAME_LABELS = {
    "mlbb": "MLBB",
    "pubg": "PUBG",
    "standoff": "Standoff",
    "genshin": "Genshin",
    "wot": "WoT",
}
INBOX = {
    "mlbb": Path("/root/data/mlbb/youtube_nightly/inbox"),
    "pubg": Path("/root/data/pubg/youtube_nightly/inbox"),
    "standoff": Path("/root/data/standoff/youtube_nightly/inbox"),
    "genshin": Path("/root/data/genshin/youtube_nightly/inbox"),
    "wot": Path("/root/data/wot/youtube_nightly/inbox"),
}
LABELS = {
    "mlbb": Path("/root/data/mlbb/vod_segment_labels.json"),
    "pubg": Path("/root/data/pubg/vod_segment_labels.json"),
    "standoff": Path("/root/data/standoff/vod_segment_labels.json"),
    "genshin": Path("/root/data/genshin/vod_segment_labels.json"),
    "wot": Path("/root/data/wot/vod_segment_labels.json"),
}
CYCLE_STATE = Path(
    os.environ.get("DAILY_GAME_CYCLE_STATE", "/root/data/mlbb/daily_game_cycle.json")
)
DEDUP_STATE = Path(
    os.environ.get("MONTAGE_DEDUP_STATE", "/root/data/mlbb/montage_dedup.json")
)
FEED_LOG = Path("/root/data/mlbb/mlbb_vod_segment_feed.log")


def msk_now() -> datetime:
    return datetime.now(MSK)


def today_msk() -> str:
    return msk_now().strftime("%Y-%m-%d")


def now_msk_hm() -> str:
    return msk_now().strftime("%H:%M")


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _quota_for(game: str) -> int:
    defaults = {"mlbb": 5, "pubg": 5, "standoff": 5, "genshin": 5, "wot": 5}
    primary = f"DAILY_GAME_{game.upper()}_QUOTA"
    legacy = f"DAILY_{game.upper()}_QUOTA"
    raw = os.environ.get(primary, os.environ.get(legacy, str(defaults.get(game, 5))))
    try:
        return max(0, int(raw))
    except ValueError:
        return defaults.get(game, 5)


def _inbox_count(game: str) -> int:
    root = INBOX.get(game)
    if root is None or not root.exists():
        return 0
    return sum(1 for _ in root.glob("yt_*.mp4"))


def _feedback_for_day(game: str, day: str) -> dict:
    data = _read_json(LABELS[game], {})
    yes = no = 0
    reasons: dict[str, int] = {}
    for row in data.get("feedback") or []:
        at = str(row.get("at") or "")
        if day not in at:
            continue
        label = row.get("owner_label")
        if label in ("yes", "good"):
            yes += 1
        elif label in ("no", "bad"):
            no += 1
            reason = str(row.get("reason") or "other").strip() or "other"
            reasons[reason] = reasons.get(reason, 0) + 1
    return {"yes": yes, "no": no, "reasons": reasons}


def _montage_today(day: str) -> list[dict]:
    reg = _read_json(DEDUP_STATE, {})
    day_map = (reg.get("day_done") or {}).get(day) or {}
    rows = []
    for game, meta in day_map.items():
        if not isinstance(meta, dict):
            continue
        rows.append(
            {
                "game": game,
                "vod_id": meta.get("vod_id"),
                "montage_id": meta.get("montage_id"),
                "peaks": meta.get("peaks") or [],
                "at": meta.get("at"),
            }
        )
    return rows


def _process_snapshot() -> dict:
    out = {"cycle": False, "mlbb_feed": False, "feed_shell": False}
    try:
        for needle, key in (
            ("daily_cycle_runner", "cycle"),
            ("mlbb_vod_segment_feed.py", "mlbb_feed"),
            ("mlbb_vod_segment_feed.sh", "feed_shell"),
        ):
            r = subprocess.run(
                ["pgrep", "-af", needle],
                capture_output=True,
                text=True,
                timeout=5,
            )
            blob = r.stdout or ""
            # Ignore the pgrep line itself and this report script.
            hits = [
                ln
                for ln in blob.splitlines()
                if needle in ln and "pgrep" not in ln and "daily_ops" not in ln
            ]
            out[key] = bool(hits)
    except (subprocess.TimeoutExpired, OSError):
        pass
    return out


def _feed_log_today(day: str) -> dict:
    """Lightweight parse of today's feed log for sent/rejects."""
    sent = 0
    montage = 0
    rejects: dict[str, int] = {}
    if not FEED_LOG.exists():
        return {"sent_lines": 0, "montage": 0, "rejects": {}}
    try:
        # Read only the tail — full log can be huge.
        raw = FEED_LOG.read_text(errors="replace").splitlines()[-12000:]
    except OSError:
        return {"sent_lines": 0, "montage": 0, "rejects": {}}
    for line in raw:
        if day not in line:
            continue
        if "montage sent" in line:
            montage += 1
        if "sent=" in line and "vod=" in line:
            sent += 1
        low = line.lower()
        for key in (
            "ocr_single_reject",
            "banner_missing",
            "trusted_discover",
            "banner_ctx_run",
            "clip_run_frac",
            "kill_banner_tier_low",
            "discovery_miss",
        ):
            if key in low:
                rejects[key] = rejects.get(key, 0) + 1
    return {"sent_lines": sent, "montage": montage, "rejects": rejects}


def gather_ops_snapshot(day: str | None = None) -> dict:
    """One structured snapshot for both morning and evening reports."""
    day = day or today_msk()
    # Prefer live cycle helpers when available.
    summary = None
    try:
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        sys.path.insert(0, "/usr/local/bin")
        from daily_game_cycle import status_summary

        summary = status_summary()
    except Exception:
        summary = None

    state = _read_json(CYCLE_STATE, {})
    sends = {g: int((state.get("sends") or {}).get(g, 0)) for g in GAME_ORDER}
    quotas = {g: _quota_for(g) for g in GAME_ORDER}
    remaining = {g: max(0, quotas[g] - sends[g]) for g in GAME_ORDER}
    active = None
    if summary:
        sends = {g: int((summary.get("sends") or {}).get(g, sends[g])) for g in GAME_ORDER}
        quotas = {g: int((summary.get("quotas") or {}).get(g, quotas[g])) for g in GAME_ORDER}
        remaining = {
            g: int((summary.get("remaining") or {}).get(g, remaining[g])) for g in GAME_ORDER
        }
        active = summary.get("active_game")
        day = str(summary.get("day") or day)
    else:
        for g in GAME_ORDER:
            if remaining[g] > 0:
                active = g
                break

    feedback = {g: _feedback_for_day(g, day) for g in GAME_ORDER}
    inbox = {g: _inbox_count(g) for g in GAME_ORDER}
    montages = _montage_today(day)
    procs = _process_snapshot()
    log_bits = _feed_log_today(day)
    skipped = state.get("skipped") or {}
    misses = state.get("discovery_misses") or {}

    total_quota = sum(quotas.values())
    total_sent = sum(sends.values())
    total_yes = sum(feedback[g]["yes"] for g in GAME_ORDER)
    total_no = sum(feedback[g]["no"] for g in GAME_ORDER)

    return {
        "day": day,
        "hm": now_msk_hm(),
        "active_game": active,
        "sends": sends,
        "quotas": quotas,
        "remaining": remaining,
        "total_sent": total_sent,
        "total_quota": total_quota,
        "feedback": feedback,
        "total_yes": total_yes,
        "total_no": total_no,
        "inbox": inbox,
        "montages": montages,
        "skipped": skipped,
        "discovery_misses": misses,
        "catchup_done": bool(state.get("catchup_done")),
        "catchup_games": list(state.get("catchup_games") or []),
        "catchup_at": state.get("catchup_at"),
        "procs": procs,
        "log": log_bits,
        "montage_on": os.environ.get("MLBB_VOD_MONTAGE", "0") == "1",
        "montage_only": os.environ.get("MONTAGE_ONLY_MODE", "0") == "1",
        "post_quota": os.environ.get("POST_QUOTA_MONTAGE", "1") != "0",
    }


def _quota_line(snap: dict) -> str:
    parts = []
    for g in GAME_ORDER:
        label = GAME_LABELS[g]
        s = snap["sends"][g]
        q = snap["quotas"][g]
        mark = "✅" if s >= q else ("🔄" if snap.get("active_game") == g else "⬜")
        parts.append(f"{mark}{label} {s}/{q}")
    return " · ".join(parts)


def _feedback_line(snap: dict) -> str:
    chunks = []
    for g in GAME_ORDER:
        fb = snap["feedback"][g]
        if fb["yes"] or fb["no"]:
            chunks.append(f"{GAME_LABELS[g]} 👍{fb['yes']}/👎{fb['no']}")
    if not chunks:
        return "оценок за день ещё нет"
    return " · ".join(chunks)


def _top_dislike_reasons(snap: dict, limit: int = 3) -> list[str]:
    merged: dict[str, int] = {}
    for g in GAME_ORDER:
        for reason, n in (snap["feedback"][g].get("reasons") or {}).items():
            merged[reason] = merged.get(reason, 0) + int(n)
    rows = sorted(merged.items(), key=lambda x: -x[1])[:limit]
    return [f"{k}×{v}" for k, v in rows]


def _problem_lines(snap: dict) -> list[str]:
    lines: list[str] = []
    skipped = snap.get("skipped") or {}
    for g, meta in skipped.items():
        if not isinstance(meta, dict):
            continue
        reason = meta.get("reason") or "?"
        lines.append(f"⏭ {GAME_LABELS.get(g, g)} пропущена: {reason}")
    misses = snap.get("discovery_misses") or {}
    for g, n in misses.items():
        try:
            ni = int(n)
        except (TypeError, ValueError):
            continue
        if ni >= 3 and snap["remaining"].get(g, 0) > 0:
            lines.append(f"⚠️ {GAME_LABELS.get(g, g)}: {ni} discovery miss подряд")
    rejects = (snap.get("log") or {}).get("rejects") or {}
    if rejects.get("ocr_single_reject"):
        lines.append(f"🛡 отсечено OCR-single FP: {rejects['ocr_single_reject']}")
    if rejects.get("banner_ctx_run") or rejects.get("clip_run_frac"):
        n = int(rejects.get("banner_ctx_run") or 0) + int(rejects.get("clip_run_frac") or 0)
        lines.append(f"🏃 отсечена беготня: {n}")
    procs = snap.get("procs") or {}
    if not procs.get("cycle") and not procs.get("mlbb_feed"):
        lines.append("🛑 цикл/фид не запущен — нужен рестарт")
    empty_inbox = [GAME_LABELS[g] for g in GAME_ORDER if snap["inbox"].get(g, 0) == 0 and snap["remaining"].get(g, 0) > 0]
    if empty_inbox:
        lines.append("📥 пустой inbox у: " + ", ".join(empty_inbox))
    return lines


def format_morning(snap: dict | None = None) -> str:
    snap = snap or gather_ops_snapshot()
    day = snap["day"]
    active = snap.get("active_game")
    active_l = GAME_LABELS.get(active, "все квоты закрыты") if active else "все квоты закрыты"
    remain = sum(snap["remaining"].values())
    mode = []
    if snap.get("montage_on"):
        mode.append("MLBB склейки 3–4 куска")
    else:
        mode.append("MLBB синглы")
    if snap.get("post_quota"):
        mode.append("после квот — +1 склейка/игра")
    if snap.get("montage_only"):
        mode.append("MONTAGE_ONLY")

    problems = _problem_lines(snap)
    inbox_bits = " · ".join(f"{GAME_LABELS[g]}:{snap['inbox'][g]}" for g in GAME_ORDER)

    focus = []
    if active:
        left = snap["remaining"][active]
        focus.append(f"Сейчас в работе: {GAME_LABELS[active]} — осталось {left} из {snap['quotas'][active]}")
    if remain == 0:
        focus.append("Квоты закрыты — жду post-quota склейки / полночь МСК")
    else:
        focus.append(f"До конца дня закрыть ещё {remain} слотов квоты")
    focus.append("Оценивай 👍/👎 под каждым роликом — это главный сигнал обучения")

    body = [
        f"🌅 Утро {day} · {snap['hm']} МСК",
        "",
        f"Квоты: {snap['total_sent']}/{snap['total_quota']}",
        _quota_line(snap),
        f"Активная игра: {active_l}",
        f"Режим: {', '.join(mode)}",
        f"Inbox VOD: {inbox_bits}",
        "",
        "План",
        *[f"• {x}" for x in focus],
    ]
    if problems:
        body += ["", "Риски", *[f"• {x}" for x in problems[:6]]]
    if snap.get("catchup_games"):
        games = ", ".join(GAME_LABELS.get(g, g) for g in snap["catchup_games"])
        body += ["", f"Catch-up сегодня: {games}" + (f" ({snap.get('catchup_at')})" if snap.get("catchup_at") else "")]
    return "\n".join(body)


def format_evening(snap: dict | None = None) -> str:
    snap = snap or gather_ops_snapshot()
    day = snap["day"]
    remain = sum(snap["remaining"].values())
    done_pct = int(100 * snap["total_sent"] / max(1, snap["total_quota"]))
    fb = _feedback_line(snap)
    reasons = _top_dislike_reasons(snap)
    montages = snap.get("montages") or []
    problems = _problem_lines(snap)

    verdict = (
        "✅ День закрыт по квотам"
        if remain == 0
        else f"⚠️ Квоты не добиты: осталось {remain}"
    )

    mont_lines = []
    if montages:
        for m in montages:
            mont_lines.append(
                f"• {GAME_LABELS.get(m['game'], m['game'])}: {m.get('montage_id') or m.get('vod_id')} "
                f"({len(m.get('peaks') or [])} пиков)"
            )
    else:
        mont_lines.append("• отдельных post-quota склеек в реестре нет")

    tomorrow = []
    if remain > 0:
        tomorrow.append(f"Добить квоты ({remain} слотов) — сейчас active={GAME_LABELS.get(snap.get('active_game') or '', '—')}")
    if reasons:
        tomorrow.append("Смотреть 👎 причины: " + ", ".join(reasons))
    tomorrow.append("Утром снова план по факту цикла, не шаблон")

    body = [
        f"🌙 Вечер {day} · {snap['hm']} МСК",
        "",
        verdict,
        f"Отправлено: {snap['total_sent']}/{snap['total_quota']} ({done_pct}%)",
        _quota_line(snap),
        "",
        f"Оценки сегодня: {fb}",
        f"Итого 👍{snap['total_yes']} / 👎{snap['total_no']}",
    ]
    if reasons:
        body.append("Топ 👎: " + ", ".join(reasons))
    body += ["", "Склейки / catch-up", *mont_lines]
    if snap.get("catchup_games"):
        body.append(
            "Catch-up: "
            + ", ".join(GAME_LABELS.get(g, g) for g in snap["catchup_games"])
            + (f" @ {snap.get('catchup_at')}" if snap.get("catchup_at") else "")
        )
    if problems:
        body += ["", "Проблемы", *[f"• {x}" for x in problems[:8]]]
    else:
        body += ["", "Проблемы", "• критичных нет"]
    body += ["", "На завтра", *[f"• {x}" for x in tomorrow]]
    return "\n".join(body)
