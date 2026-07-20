#!/usr/bin/env python3
"""
Improve highlight system from viral Shorts WITHOUT sending videos to Telegram.

1. Load viral silver features (downloaded Shorts analysis)
2. Load current owner-good VOD segments + existing good exemplars
3. Keep only viral clips similar to current good style (feature-space)
4. Refresh exemplars with those similar-but-stronger clips
5. Suggest / optionally apply threshold nudges toward viral patterns
6. Write improve report (text JSON) — never sends Shorts to chat
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from highlight_scorer import EXEMPLAR_ROOT, clear_exemplar_cache, normalize_profile
from viral_reference_ingest import ALL_PROFILES, DATA_ROOT, copy_exemplar

REPO = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml"))
REPORT_PATH = DATA_ROOT / "improve_report.json"

PROFILE_TO_DATA = {
    "mobile_legends": Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb")),
    "pubg": Path(os.environ.get("PUBG_DATA_ROOT", "/root/data/pubg")),
    "standoff": Path(os.environ.get("STANDOFF_DATA_ROOT", "/root/data/standoff")),
    "genshin": Path(os.environ.get("GENSHIN_DATA_ROOT", "/root/data/genshin")),
    "wot": Path(os.environ.get("WOT_DATA_ROOT", "/root/data/wot")),
}

FEATURE_KEYS = ("hook_score", "combat_score", "center_motion", "clip_score", "panns_gun_max")


def _f(row: dict, key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _avg(rows: list[dict], key: str) -> float:
    if not rows:
        return 0.0
    return sum(_f(r, key) for r in rows) / len(rows)


def _median(vals: list[float]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def _feat_vec(row: dict) -> list[float]:
    return [_f(row, k) for k in FEATURE_KEYS]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return dot / (na * nb)


def load_viral_features(profile: str) -> list[dict]:
    path = DATA_ROOT / f"{profile}_features.csv"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def load_owner_good(profile: str) -> list[dict]:
    """Owner 👍 VOD segments — the current taste baseline."""
    root = PROFILE_TO_DATA.get(profile)
    if root is None:
        return []
    path = root / "vod_segment_labels.json"
    if not path.exists() and profile == "mobile_legends":
        path = Path("/root/data/mlbb/vod_segment_labels.json")
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    out: list[dict] = []
    for row in data.get("good", []):
        out.append(
            {
                "source": "owner_vseg",
                "hook_score": row.get("hook_score", 0),
                "combat_score": float(row.get("panns_gun_max") or 0)
                + float(row.get("clip_score") or 0) * 0.4,
                "center_motion": row.get("center_motion", 0),
                "clip_score": row.get("clip_score", 0),
                "panns_gun_max": row.get("panns_gun_max", 0),
                "title": row.get("segment_id") or row.get("video_id") or "",
            }
        )
    return out


def load_existing_exemplar_stats(profile: str) -> dict:
    good_dir = EXEMPLAR_ROOT / profile / "good"
    bad_dir = EXEMPLAR_ROOT / profile / "bad"
    good = list(good_dir.glob("*.mp4")) if good_dir.exists() else []
    bad = list(bad_dir.glob("*.mp4")) if bad_dir.exists() else []
    viral_good = [p for p in good if p.name.startswith("viral_")]
    owner_good = [p for p in good if not p.name.startswith("viral_")]
    return {
        "good_total": len(good),
        "bad_total": len(bad),
        "viral_good": len(viral_good),
        "owner_or_cal_good": len(owner_good),
    }


def select_similar_viral(
    viral: list[dict],
    owner_good: list[dict],
    *,
    top_k: int = 5,
    min_sim: float = 0.55,
) -> list[dict]:
    """Pick viral clips close to current owner-good style (not random high-views)."""
    if not viral:
        return []
    if not owner_good:
        # No owner baseline yet — take high combat+hook viral as seed
        ranked = sorted(
            viral,
            key=lambda r: _f(r, "hook_score") * 0.5 + _f(r, "combat_score") * 0.5,
            reverse=True,
        )
        return ranked[:top_k]

    centroid = [0.0] * len(FEATURE_KEYS)
    for row in owner_good:
        vec = _feat_vec(row)
        for i, v in enumerate(vec):
            centroid[i] += v
    n = len(owner_good)
    centroid = [v / n for v in centroid]

    scored: list[tuple[float, dict]] = []
    for row in viral:
        sim = _cosine(_feat_vec(row), centroid)
        # Prefer similar + slightly stronger combat/hook than owner avg
        boost = 0.0
        if _f(row, "hook_score") >= _avg(owner_good, "hook_score"):
            boost += 0.05
        if _f(row, "combat_score") >= _avg(owner_good, "combat_score"):
            boost += 0.05
        views = _f(row, "view_count")
        view_bonus = min(0.1, math.log10(max(views, 1)) / 50.0)
        scored.append((sim + boost + view_bonus, {**row, "similarity": round(sim, 4)}))

    scored.sort(key=lambda x: x[0], reverse=True)
    above = [row for _, row in scored if float(row.get("similarity") or 0) >= min_sim]
    if len(above) >= max(1, top_k // 2):
        return above[:top_k]
    return [row for _, row in scored][:top_k]


def promote_exemplars(profile: str, selected: list[dict], *, max_promote: int = 4) -> int:
    """Copy similar viral clips into good exemplars (CLIP uses these)."""
    added = 0
    good_dir = EXEMPLAR_ROOT / profile / "good"
    good_dir.mkdir(parents=True, exist_ok=True)
    for row in selected[:max_promote]:
        src = Path(row.get("path") or "")
        if not src.exists():
            continue
        dest = good_dir / f"viral_{src.stem}.mp4"
        if dest.exists():
            added += 1
            continue
        if copy_exemplar(src, dest):
            added += 1
    clear_exemplar_cache()
    return added


def suggest_thresholds(profile: str, viral: list[dict], owner_good: list[dict]) -> dict:
    """Nudge hook/combat floors toward viral patterns that overlap owner taste."""
    if len(viral) < 3:
        return {"status": "insufficient_viral", "apply": {}}

    viral_hooks = [_f(r, "hook_score") for r in viral if _f(r, "hook_score") > 0]
    viral_combat = [_f(r, "combat_score") for r in viral if _f(r, "combat_score") > 0]
    owner_hooks = [_f(r, "hook_score") for r in owner_good if _f(r, "hook_score") > 0]

    v_hook = _median(viral_hooks)
    o_hook = _median(owner_hooks) if owner_hooks else v_hook
    # Stay between owner taste and viral — never jump fully to viral alone
    blended_hook = 0.6 * o_hook + 0.4 * v_hook if owner_hooks else v_hook * 0.7

    apply: dict[str, str] = {}
    notes: list[str] = []

    if profile == "mobile_legends":
        # Small nudge only — never jump above 0.10 or below current soft floor
        hook_min = max(0.06, min(0.10, blended_hook * 0.55))
        apply["VIRAL_MLBB_HOOK_MIN"] = f"{hook_min:.4f}"
        apply["VIRAL_SEGMENT_HOOK_MIN"] = f"{hook_min:.4f}"
        notes.append(f"MLBB hook floor → {hook_min:.3f} (owner={o_hook:.3f} viral={v_hook:.3f})")
    elif profile in ("pubg", "standoff"):
        # Exemplars carry most of the learning; keep combat hook soft
        hook_min = max(0.04, min(0.08, blended_hook * 0.35))
        apply["VIRAL_COMBAT_HOOK_MIN"] = f"{hook_min:.4f}"
        notes.append(f"{profile} combat hook soft → {hook_min:.3f} (learning via exemplars)")
    elif profile == "genshin":
        notes.append(f"Genshin viral combat_avg={_avg(viral, 'combat_score'):.3f} — improve via exemplars, keep fight bounds")
    elif profile == "wot":
        notes.append(f"WoT viral gun_avg={_avg(viral, 'panns_gun_max'):.3f} — improve via exemplars")

    return {
        "status": "ok",
        "viral_hook_median": round(v_hook, 4),
        "owner_hook_median": round(o_hook, 4),
        "viral_combat_median": round(_median(viral_combat), 4),
        "apply": apply,
        "notes": notes,
    }


def patch_env(env_path: Path, updates: dict[str, str]) -> list[str]:
    if not updates or not env_path.exists():
        return []
    text = env_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    changed: list[str] = []
    for key, value in updates.items():
        found = False
        for i, line in enumerate(lines):
            if line.startswith(f"{key}=") or line.startswith(f"export {key}="):
                prefix = "export " if line.startswith("export ") else ""
                lines[i] = f"{prefix}{key}={value}"
                found = True
                changed.append(f"{key}={value}")
                break
        if not found:
            lines.append(f"{key}={value}")
            changed.append(f"{key}={value} (new)")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


def improve_profile(profile: str, *, apply_env: bool, top_k: int) -> dict:
    profile = normalize_profile(profile)
    viral = load_viral_features(profile)
    owner = load_owner_good(profile)
    exemplars = load_existing_exemplar_stats(profile)

    selected = select_similar_viral(viral, owner, top_k=top_k)
    promoted = promote_exemplars(profile, selected, max_promote=min(4, top_k))
    thresholds = suggest_thresholds(profile, viral, owner)

    env_changed: list[str] = []
    if apply_env and thresholds.get("apply"):
        env_path = Path(os.environ.get("VIDEO_BOT_ENV", "/root/.video_bot.env"))
        env_changed = patch_env(env_path, thresholds["apply"])

    gap: list[str] = []
    if owner and viral:
        oh, vh = _avg(owner, "hook_score"), _avg(viral, "hook_score")
        oc, vc = _avg(owner, "combat_score"), _avg(viral, "combat_score")
        if vh > oh + 0.08:
            gap.append(f"Viral hook выше вашего ({vh:.2f} vs {oh:.2f}) — бот будет сильнее стартовать клип")
        if vc > oc + 0.08:
            gap.append(f"Viral combat выше ({vc:.2f} vs {oc:.2f}) — больше веса на боевой звук/движение")
        if vh < oh and vc < oc:
            gap.append("Ваш вкус уже жёстче viral — viral только расширяет exemplars, пороги не снижаем")
    elif not owner:
        gap.append("Мало ваших 👍 — viral exemplars как стартовый ориентир; оцените клипы бота")

    return {
        "profile": profile,
        "viral_clips": len(viral),
        "owner_good": len(owner),
        "exemplars_before": exemplars,
        "selected_similar": [
            {
                "file": Path(r.get("path") or "").name,
                "views": int(_f(r, "view_count")),
                "hook": round(_f(r, "hook_score"), 3),
                "combat": round(_f(r, "combat_score"), 3),
                "similarity": r.get("similarity"),
                "title": (r.get("title") or "")[:60],
            }
            for r in selected
        ],
        "exemplars_promoted": promoted,
        "thresholds": thresholds,
        "env_changed": env_changed,
        "gap_insights": gap,
    }


def build_improve_report(profiles: tuple[str, ...], *, apply_env: bool, top_k: int) -> dict:
    games = [improve_profile(p, apply_env=apply_env, top_k=top_k) for p in profiles]
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": "analyze_and_improve_only",
        "sends_videos": False,
        "games": games,
        "summary": [
            f"{g['profile']}: viral={g['viral_clips']} owner👍={g['owner_good']} "
            f"promoted={g['exemplars_promoted']}"
            for g in games
        ],
    }


def format_telegram(report: dict) -> str:
    lines = [
        "🧠 Viral → improve (без рассылки Shorts)",
        f"{report['generated_at']} | только анализ + exemplars/пороги",
        "",
    ]
    for g in report.get("games", []):
        lines.append(
            f"🎮 {g['profile']}: viral={g['viral_clips']} | ваши👍={g['owner_good']} "
            f"| +exemplars={g['exemplars_promoted']}"
        )
        for row in g.get("selected_similar", [])[:2]:
            lines.append(
                f"   ≈ sim={row.get('similarity')} hook={row['hook']} "
                f"combat={row['combat']} — {row.get('title') or row.get('file')}"
            )
        for note in (g.get("thresholds") or {}).get("notes", [])[:2]:
            lines.append(f"   • {note}")
        for gap in g.get("gap_insights", [])[:2]:
            lines.append(f"   • {gap}")
        if g.get("env_changed"):
            lines.append(f"   env: {', '.join(g['env_changed'][:3])}")
        lines.append("")
    lines.append("Видео в чат не шлём. Отчёт: data/viral_reference/improve_report.json")
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
    parser = argparse.ArgumentParser(description="Improve scoring from viral analysis (no video spam)")
    parser.add_argument("--profile", default="all", choices=["all", *ALL_PROFILES, "mlbb"])
    parser.add_argument("--top-k", type=int, default=5, help="Similar viral clips to promote per game")
    parser.add_argument("--apply-env", action="store_true", help="Write threshold nudges to .video_bot.env")
    parser.add_argument("--telegram", action="store_true", help="Send TEXT improve report only (never videos)")
    args = parser.parse_args()

    profiles = ALL_PROFILES if args.profile == "all" else (normalize_profile(args.profile),)
    report = build_improve_report(profiles, apply_env=args.apply_env, top_k=max(1, args.top_k))
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
