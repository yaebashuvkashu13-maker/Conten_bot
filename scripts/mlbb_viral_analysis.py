#!/usr/bin/env python3
"""Analyze MLBB YouTube Shorts: views vs model score vs owner labels — learn what goes viral."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_calibration_store import DATA_MLBB, labeled_ids, load_index, load_labels, stats
from youtube_download import load_env

REPORT_PATH = DATA_MLBB / "mlbb_viral_analysis.json"
ENV_PATH = Path("/root/.video_bot.env")

TITLE_THEMES = (
    ("teamfight", re.compile(r"team\s*fight|teamfight|clash|war|5v5|team\s*kill", re.I)),
    ("savage", re.compile(r"savage|maniac|legendary|pentakill|wipe", re.I)),
    ("hero", re.compile(r"chou|gusion|fanny|ling|hayabusa|lancelot|karrie|beatrix", re.I)),
    ("rank", re.compile(r"mythic|legend|epic|rank|mmr|grandmaster", re.I)),
    ("edit", re.compile(r"edit|montage|compilation|amv|capcut", re.I)),
    ("short_hook", re.compile(r"insane|crazy|clutch|1v\d|solo|outplay|highlight", re.I)),
)


def _views_per_day(row: dict) -> float:
    views = int(row.get("view_count") or 0)
    upload = str(row.get("upload_date") or "")
    if not upload or not upload.isdigit() or len(upload) != 8:
        return float(views)
    try:
        uploaded = datetime.strptime(upload, "%Y%m%d").replace(tzinfo=timezone.utc)
        age_days = max(1.0, (datetime.now(timezone.utc) - uploaded).total_seconds() / 86400.0)
        return views / age_days
    except ValueError:
        return float(views)


def _theme_hits(title: str) -> list[str]:
    return [name for name, pat in TITLE_THEMES if pat.search(title or "")]


def _avg(rows: list[dict], key: str) -> float:
    vals = [float(r.get(key) or 0) for r in rows]
    return sum(vals) / len(vals) if vals else 0.0


def _median(rows: list[dict], key: str) -> float:
    vals = sorted(float(r.get(key) or 0) for r in rows)
    if not vals:
        return 0.0
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def build_analysis() -> dict:
    rows = list(load_index().get("candidates", []))
    for row in rows:
        row["views_per_day"] = round(_views_per_day(row), 1)
        row["themes"] = _theme_hits(str(row.get("title", "")))

    labeled = labeled_ids()
    owner_good = [r for r in rows if labeled.get(str(r.get("video_id", ""))) == "good"]
    owner_bad = [r for r in rows if labeled.get(str(r.get("video_id", ""))) == "bad"]
    unlabeled = [r for r in rows if str(r.get("video_id", "")) not in labeled]

    by_views = sorted(rows, key=lambda r: int(r.get("view_count") or 0), reverse=True)
    by_vpd = sorted(rows, key=lambda r: float(r.get("views_per_day") or 0), reverse=True)
    top_n = max(1, min(5, len(by_vpd)))
    bottom_n = max(1, min(5, len(by_vpd)))

    top = by_vpd[:top_n]
    bottom = by_vpd[-bottom_n:] if len(by_vpd) > bottom_n else []

    theme_stats: dict[str, dict] = {}
    for row in rows:
        for theme in row.get("themes") or ["other"]:
            bucket = theme_stats.setdefault(theme, {"count": 0, "views_sum": 0, "vpd_sum": 0.0})
            bucket["count"] += 1
            bucket["views_sum"] += int(row.get("view_count") or 0)
            bucket["vpd_sum"] += float(row.get("views_per_day") or 0)
    for theme, bucket in theme_stats.items():
        n = max(1, bucket["count"])
        bucket["avg_views"] = round(bucket["views_sum"] / n)
        bucket["avg_vpd"] = round(bucket["vpd_sum"] / n, 1)

    insights: list[str] = []
    if len(top) >= 2 and len(bottom) >= 1:
        top_themes: dict[str, int] = {}
        bot_themes: dict[str, int] = {}
        for r in top:
            for t in r.get("themes") or ["other"]:
                top_themes[t] = top_themes.get(t, 0) + 1
        for r in bottom:
            for t in r.get("themes") or ["other"]:
                bot_themes[t] = bot_themes.get(t, 0) + 1
        for theme, cnt in sorted(top_themes.items(), key=lambda x: -x[1]):
            if cnt >= 2 and bot_themes.get(theme, 0) == 0:
                insights.append(f"Тема «{theme}» чаще в топе по просмотрам/день")
        if _avg(top, "hook_score") > _avg(bottom, "hook_score") + 0.05:
            insights.append("У залетевших выше hook_score — сильный старт кадра")
        if _avg(top, "score") > _avg(bottom, "score") + 0.08:
            insights.append("Модель combat/highlight коррелирует с просмотрами")
        elif _avg(top, "score") < _avg(bottom, "score"):
            insights.append("Просмотры не совпадают с нашим score — учимся на 👍/👎")

    if owner_good and owner_bad:
        if _avg(owner_good, "view_count") > _avg(owner_bad, "view_count") * 1.3:
            insights.append("Ваши 👍 в среднем с большими просмотрами — ориентир на viral-паттерн")
        if _avg(owner_good, "hook_score") > _avg(owner_bad, "hook_score") + 0.05:
            insights.append("В 👍 чаще сильный hook в первые секунды")

    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_shorts": len(rows),
        "labeled_good": len(owner_good),
        "labeled_bad": len(owner_bad),
        "pending": len(unlabeled),
        "calibration": stats(),
        "averages": {
            "all_views": round(_avg(rows, "view_count")),
            "all_vpd": round(_avg(rows, "views_per_day"), 1),
            "all_score": round(_avg(rows, "score"), 3),
            "all_hook": round(_avg(rows, "hook_score"), 3),
            "good_views": round(_avg(owner_good, "view_count")),
            "bad_views": round(_avg(owner_bad, "view_count")),
            "good_score": round(_avg(owner_good, "score"), 3),
            "bad_score": round(_avg(owner_bad, "score"), 3),
        },
        "top_by_views_per_day": [
            {
                "video_id": r.get("video_id"),
                "views": int(r.get("view_count") or 0),
                "vpd": r.get("views_per_day"),
                "score": r.get("score"),
                "hook": r.get("hook_score"),
                "themes": r.get("themes"),
                "title": (r.get("title") or "")[:80],
            }
            for r in top
        ],
        "bottom_by_views_per_day": [
            {
                "video_id": r.get("video_id"),
                "views": int(r.get("view_count") or 0),
                "vpd": r.get("views_per_day"),
                "score": r.get("score"),
                "hook": r.get("hook_score"),
                "themes": r.get("themes"),
                "title": (r.get("title") or "")[:80],
            }
            for r in bottom
        ],
        "theme_stats": theme_stats,
        "insights": insights,
        "bad_reasons": [
            {"video_id": r.get("video_id"), "reason": r.get("reason", "")}
            for r in load_labels().get("bad", [])
            if r.get("reason")
        ][-10:],
    }


def format_telegram(report: dict) -> str:
    s = report["calibration"]
    lines = [
        "📈 MLBB — анализ Shorts (просмотры vs качество)",
        f"Shorts в индексе: {report['total_shorts']} | ждут оценки: {report['pending']}",
        f"👍 {s['feedback_yes']} / 👎 {s['feedback_no']} | согласие модели: {s['accuracy']:.0%}",
        "",
        "Средние:",
        f"• views={report['averages']['all_views']} | views/день={report['averages']['all_vpd']}",
        f"• score={report['averages']['all_score']} | hook={report['averages']['all_hook']}",
    ]
    if report["labeled_good"] or report["labeled_bad"]:
        lines.append(
            f"• ваши 👍 views={report['averages']['good_views']} score={report['averages']['good_score']}"
        )
        lines.append(
            f"• ваши 👎 views={report['averages']['bad_views']} score={report['averages']['bad_score']}"
        )

    lines.append("")
    lines.append("🔥 Топ по просмотрам/день:")
    for row in report.get("top_by_views_per_day", [])[:3]:
        themes = ",".join(row.get("themes") or []) or "—"
        lines.append(
            f"• {row['views']} ({row['vpd']}/д) score={row['score']} [{themes}]"
            f"\n  {(row.get('title') or '')[:70]}"
        )

    if report.get("bottom_by_views_per_day"):
        lines.append("")
        lines.append("📉 Слабые по просмотрам/день:")
        for row in report["bottom_by_views_per_day"][:2]:
            lines.append(f"• {row['views']} ({row['vpd']}/д) — {(row.get('title') or '')[:60]}")

    if report.get("insights"):
        lines.append("")
        lines.append("Выводы:")
        for item in report["insights"][:5]:
            lines.append(f"• {item}")

    need_yes = max(0, 30 - s["feedback_yes"])
    need_no = max(0, 20 - s["feedback_no"])
    lines.append("")
    lines.append(f"До eval: ещё 👍{need_yes} / 👎{need_no} (/mlbb_yes /mlbb_no)")
    lines.append("Режим: только MLBB Shorts — другие игры отключены.")
    return "\n".join(lines)


def send_message(token: str, chat_id: str, text: str) -> None:
    subprocess.run(
        [
            "curl",
            "-sS",
            "-F",
            f"chat_id={chat_id}",
            "-F",
            f"text={text[:3900]}",
            f"https://api.telegram.org/bot{token}/sendMessage",
        ],
        env={k: v for k, v in os.environ.items() if "proxy" not in k.lower()},
        check=False,
        timeout=30,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--telegram", action="store_true", help="Send report to owner")
    args = parser.parse_args()

    report = build_analysis()
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    text = format_telegram(report)
    print(text)

    if args.telegram:
        env = {**os.environ, **load_env(ENV_PATH)}
        token = env.get("TG_BOT_TOKEN", "")
        chat_id = env.get("TG_CHAT_ID", "")
        if token and chat_id:
            send_message(token, chat_id, text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
