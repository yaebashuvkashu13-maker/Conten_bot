#!/usr/bin/env python3
"""Multi-game viral Shorts learning report — views vs audio/visual features vs clusters."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from highlight_scorer import normalize_profile
from viral_reference_ingest import ALL_PROFILES, DATA_ROOT

REPO = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml"))
REPORT_PATH = DATA_ROOT / "learning_report.json"

GAME_LABELS = {
    "mobile_legends": "MLBB",
    "pubg": "PUBG",
    "standoff": "Standoff 2",
    "genshin": "Genshin",
    "wot": "WoT",
}

TITLE_THEMES: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    "mobile_legends": (
        ("teamfight", re.compile(r"team\s*fight|teamfight|clash|5v5|wipe", re.I)),
        ("savage", re.compile(r"savage|maniac|legendary|pentakill", re.I)),
        ("clutch", re.compile(r"clutch|1v\d|solo|outplay|insane|crazy", re.I)),
        ("rank", re.compile(r"mythic|legend|epic|rank|mmr", re.I)),
    ),
    "pubg": (
        ("metro", re.compile(r"метро|metro\s*royale|metro", re.I)),
        ("clutch", re.compile(r"clutch|1v\d|solo|squad|против", re.I)),
        ("gunfight", re.compile(r"gunfight|перестрел|shoot|frag", re.I)),
        ("loot", re.compile(r"loot|добыч|фул\s*6|богат", re.I)),
    ),
    "standoff": (
        ("clutch", re.compile(r"clutch|клатч|1v\d|ace|эйс", re.I)),
        ("ranked", re.compile(r"ranked|rank|ранг", re.I)),
        ("headshot", re.compile(r"headshot|хедшот|awp|снайп", re.I)),
    ),
    "genshin": (
        ("boss", re.compile(r"boss|босс|raid|рейд", re.I)),
        ("domain", re.compile(r"domain|домен|abyss|бездн", re.I)),
        ("combo", re.compile(r"combo|burst|one\s*shot|ваншот", re.I)),
    ),
    "wot": (
        ("frag", re.compile(r"frag|фраг|kill|убий", re.I)),
        ("shot", re.compile(r"shot|выстрел|penetration|пробит", re.I)),
        ("brawl", re.compile(r"brawl|melee|вблиз|танк", re.I)),
    ),
}


def _avg(rows: list[dict], key: str) -> float:
    vals = [float(r.get(key) or 0) for r in rows]
    return sum(vals) / len(vals) if vals else 0.0


def _theme_hits(profile: str, title: str) -> list[str]:
    themes = TITLE_THEMES.get(profile, ())
    return [name for name, pat in themes if pat.search(title or "")]


def _load_features(profile: str) -> list[dict]:
    path = DATA_ROOT / f"{profile}_features.csv"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _load_clusters(profile: str) -> dict:
    path = DATA_ROOT / f"{profile}_clusters.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def analyze_profile(profile: str) -> dict:
    profile = normalize_profile(profile)
    rows = _load_features(profile)
    clusters = _load_clusters(profile)

    for row in rows:
        row["themes"] = _theme_hits(profile, str(row.get("title", "")))
        row["interest_proxy"] = round(
            float(row.get("hook_score") or 0) * 0.45
            + float(row.get("combat_score") or 0) * 0.35
            + float(row.get("center_motion") or 0) * 0.20,
            4,
        )

    by_views = sorted(rows, key=lambda r: int(float(r.get("view_count") or 0)), reverse=True)
    top_n = max(1, min(3, len(by_views)))
    bottom_n = max(1, min(3, len(by_views)))
    top = by_views[:top_n]
    bottom = by_views[-bottom_n:] if len(by_views) > bottom_n else []

    insights: list[str] = []
    if top and bottom:
        if _avg(top, "hook_score") > _avg(bottom, "hook_score") + 0.04:
            insights.append("Сильный hook в первые секунды коррелирует с просмотрами")
        if _avg(top, "combat_score") > _avg(bottom, "combat_score") + 0.05:
            insights.append("Больше боевого звука/визуала в топе по views")
        if _avg(top, "center_motion") > _avg(bottom, "center_motion") + 0.03:
            insights.append("В топе выше движение в центре кадра (экшен в фокусе)")
        if _avg(top, "interest_proxy") < _avg(bottom, "interest_proxy"):
            insights.append("Просмотры ≠ наш proxy-score — нужны ваши 👍/👎 для калибровки")

    theme_stats: dict[str, dict] = {}
    for row in rows:
        for theme in row.get("themes") or ["other"]:
            bucket = theme_stats.setdefault(theme, {"count": 0, "views_sum": 0})
            bucket["count"] += 1
            bucket["views_sum"] += int(float(row.get("view_count") or 0))
    for theme, bucket in theme_stats.items():
        n = max(1, bucket["count"])
        bucket["avg_views"] = round(bucket["views_sum"] / n)

    moment_patterns: list[str] = []
    if _avg(rows, "panns_gun_max") >= 0.12:
        moment_patterns.append("громкий gunshot/перестрелка")
    if _avg(rows, "hook_score") >= 0.35:
        moment_patterns.append("сильный старт без меню")
    if _avg(rows, "center_motion") >= 0.08:
        moment_patterns.append("динамика в центре HUD")
    if profile == "genshin" and _avg(rows, "combat_score") >= 0.1:
        moment_patterns.append("босс/комбат-сцена")
    if profile == "wot" and _avg(rows, "panns_gun_max") >= 0.1:
        moment_patterns.append("взрыв/фраг-момент")

    archetypes: list[dict] = []
    if clusters.get("status") == "ok":
        by_name = {r.get("file_name"): r for r in rows}
        for cluster in clusters.get("clusters", [])[:3]:
            clips = []
            for fname in cluster.get("top_clips", [])[:2]:
                row = by_name.get(fname)
                if row:
                    clips.append(
                        {
                            "title": (row.get("title") or "")[:70],
                            "views": int(float(row.get("view_count") or 0)),
                            "hook": float(row.get("hook_score") or 0),
                            "combat": float(row.get("combat_score") or 0),
                        }
                    )
            archetypes.append(
                {
                    "cluster_id": cluster.get("cluster_id"),
                    "size": cluster.get("size"),
                    "top_views": cluster.get("archetype_views"),
                    "clips": clips,
                }
            )

    return {
        "profile": profile,
        "label": GAME_LABELS.get(profile, profile),
        "clips_analyzed": len(rows),
        "averages": {
            "views": round(_avg(rows, "view_count")),
            "hook_score": round(_avg(rows, "hook_score"), 3),
            "combat_score": round(_avg(rows, "combat_score"), 3),
            "center_motion": round(_avg(rows, "center_motion"), 3),
            "interest_proxy": round(_avg(rows, "interest_proxy"), 3),
        },
        "top_by_views": [
            {
                "video_id": r.get("video_id"),
                "views": int(float(r.get("view_count") or 0)),
                "hook": float(r.get("hook_score") or 0),
                "combat": float(r.get("combat_score") or 0),
                "themes": r.get("themes"),
                "title": (r.get("title") or "")[:80],
            }
            for r in top
        ],
        "moment_patterns": moment_patterns,
        "theme_stats": theme_stats,
        "archetypes": archetypes,
        "insights": insights,
    }


def build_report(profiles: tuple[str, ...] | None = None) -> dict:
    profiles = profiles or ALL_PROFILES
    games = [analyze_profile(p) for p in profiles]
    total_clips = sum(g["clips_analyzed"] for g in games)

    global_insights: list[str] = []
    if total_clips >= 10:
        global_insights.append(
            f"Собрано {total_clips} viral Shorts — CLIP-кластеры и exemplars обновлены автоматически"
        )
    with_data = [g for g in games if g["clips_analyzed"] >= 3]
    if with_data:
        best_hook = max(with_data, key=lambda g: g["averages"]["hook_score"])
        global_insights.append(
            f"Сильнейший hook в среднем: {best_hook['label']} "
            f"({best_hook['averages']['hook_score']:.2f})"
        )

    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_clips": total_clips,
        "games": games,
        "global_insights": global_insights,
        "usefulness": {
            "realistic": True,
            "notes": [
                "Полезно как silver-обучение: hook/combat/motion + CLIP-кластеры → exemplars good/",
                "Просмотры — слабый прокси качества (мемы, clickbait, не-геймплей)",
                "Лучший сигнал: ваши 👍/👎 + viral silver вместе (owner anchors + exemplars)",
                "Автоанализ не заменяет монтаж/титры — только паттерны момента в кадре/звуке",
            ],
        },
    }


def format_telegram(report: dict) -> str:
    lines = [
        "🎓 Viral learning — отчёт по Shorts",
        f"Клипов: {report['total_clips']} | {report['generated_at']}",
        "",
    ]
    for game in report.get("games", []):
        if game["clips_analyzed"] == 0:
            lines.append(f"⏭ {game['label']}: нет данных")
            continue
        avg = game["averages"]
        lines.append(
            f"🎮 {game['label']} ({game['clips_analyzed']} шт) "
            f"avg views={avg['views']:,} hook={avg['hook_score']} combat={avg['combat_score']}"
        )
        if game.get("moment_patterns"):
            lines.append(f"   паттерны: {', '.join(game['moment_patterns'][:3])}")
        for row in game.get("top_by_views", [])[:1]:
            lines.append(f"   🔥 {row['views']:,} views — {(row.get('title') or '')[:55]}")
        for item in game.get("insights", [])[:2]:
            lines.append(f"   • {item}")
        lines.append("")

    if report.get("global_insights"):
        lines.append("Общее:")
        for item in report["global_insights"]:
            lines.append(f"• {item}")

    lines.append("")
    lines.append("Exemplars: data/highlight_exemplars/{game}/good/viral_*")
    lines.append("Отчёт: data/viral_reference/learning_report.json")
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
        check=False,
        timeout=30,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="all", choices=["all", *ALL_PROFILES, "mlbb"])
    parser.add_argument("--telegram", action="store_true")
    args = parser.parse_args()

    profiles: tuple[str, ...]
    if args.profile == "all":
        profiles = ALL_PROFILES
    else:
        profiles = (normalize_profile(args.profile),)

    report = build_report(profiles)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    text = format_telegram(report)
    print(text)

    if args.telegram:
        from youtube_download import load_env

        env = load_env()
        token = env.get("TG_BOT_TOKEN", "")
        chat_id = env.get("TG_CHAT_ID", "")
        if token and chat_id:
            send_message(token, chat_id, text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
