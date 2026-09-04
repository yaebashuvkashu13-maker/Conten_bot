#!/usr/bin/env python3
"""Kimi (Moonshot) ops assistant for Conten_bot PUBG pipeline.

Read-only by default. Helps the owner with status, feedback triage and
explanations. Does NOT download VODs, weaken gates, deploy, or post to socials.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ENV_FILE = Path(os.environ.get("VIDEO_BOT_ENV", "/root/.video_bot.env"))

# Owner-facing brief injected into every Kimi call.
PUBG_SYSTEM_BRIEF = """
Ты — Kimi, ops-помощник владельца бота Conten_bot (PUBG VOD → Telegram).

## Что делает бот
1. Ищет/скачивает PUBG VOD (YouTube).
2. Находит моменты перестрелок (DSP gunfire → PANNs → ranker → kill notification OCR/CLIP).
3. Режет клипы так, чтобы внутри была полноценная перестрелка с подтверждённым убийством/payoff.
4. Шлёт владельцу в Telegram на оценку 👍/👎.
5. Учится на оценках владельца (labels + nightly ranker), но НЕ ослабляет production-гейты без явного решения.

## Что владелец ожидает на выходе
- ТОЛЬКО перестрелки с payoff (kill/knock/решающий исход).
- НЕ слать: беготню, лут, полёты, просто выстрелы в воздух, death автора без kill, teammate kill как «свой».
- Клип: contact −1–2с … kill +2–4с, без длинного loot-хвоста.
- Качество важнее скорости. Лучше 0 клипов, чем мусор.

## Что можно советовать / делать тебе
- Объяснять статус feed, reject reasons, конфиг гейтов.
- Разбирать 👎: loot_run / no_kill / weak_fight / death / teammate.
- Предлагать, какие пороги или детекторы проверить.
- Коротко отвечать по-русски, конкретно, без воды.

## Что ЗАПРЕЩЕНО
- Ослаблять гейты (SOFTEN, SCORE_MODE=0, gun bypass, require kill=off ради объёма).
- Деплоить, останавливать сервисы, менять .env, публиковать в соцсети.
- Выдумывать метрики. Если данных нет — скажи «нет данных».
- Включать PUBG_REQUIRE_KILL_NOTIFICATION=required без измеренных precision/recall.

## Полезные факты
- Kill notification mode сейчас: prefer (не required).
- Singles раньше пропускали мусор через GUN_PAYOFF_BYPASS — это исправлено; bypass должен быть 0.
- Owner 👍/👎 пишутся в /root/data/pubg/vod_segment_labels.json и pubg_owner_labels.json.
- Главная метрика скорости: approved clips / wall-clock minute; главная качества: bad accept rate вниз, accepted recall вверх.
""".strip()


def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    try:
        from vod_env import load_env as _load

        return _load(path)
    except Exception:
        out: dict[str, str] = {}
        if not path.is_file():
            return out
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
        return out


def api_key(env: dict[str, str] | None = None) -> str:
    env = env or {**os.environ, **load_env()}
    return (
        env.get("MOONSHOT_API_KEY")
        or env.get("KIMI_API_KEY")
        or os.environ.get("MOONSHOT_API_KEY")
        or os.environ.get("KIMI_API_KEY")
        or ""
    ).strip()


def api_base(env: dict[str, str] | None = None) -> str:
    env = env or {**os.environ, **load_env()}
    return (
        env.get("MOONSHOT_API_BASE")
        or env.get("KIMI_API_BASE")
        or "https://api.moonshot.ai/v1"
    ).rstrip("/")


def model_name(env: dict[str, str] | None = None) -> str:
    env = env or {**os.environ, **load_env()}
    return env.get("KIMI_MODEL") or env.get("MOONSHOT_MODEL") or "moonshot-v1-auto"


def collect_context() -> dict[str, Any]:
    """Safe read-only snapshot for the model."""
    ctx: dict[str, Any] = {"ok": True}
    try:
        from vod_config import config_status

        ctx["config"] = config_status()
    except Exception as exc:
        ctx["config_error"] = str(exc)[:200]

    labels = Path("/root/data/pubg/vod_segment_labels.json")
    if labels.is_file():
        try:
            data = json.loads(labels.read_text(encoding="utf-8"))
            good = data.get("good") or []
            bad = data.get("bad") or []
            ctx["feedback"] = {
                "good": len(good),
                "bad": len(bad),
                "recent_bad": [
                    {
                        "segment_id": r.get("segment_id"),
                        "reason": r.get("reason"),
                        "at": r.get("at"),
                        "start": r.get("start"),
                    }
                    for r in sorted(bad, key=lambda x: str(x.get("at") or ""), reverse=True)[:8]
                ],
            }
        except Exception as exc:
            ctx["feedback_error"] = str(exc)[:200]

    env = {**os.environ, **load_env()}
    ctx["gates"] = {
        k: env.get(k)
        for k in (
            "PUBG_KILL_NOTIFICATION_MODE",
            "PUBG_REQUIRE_KILL_NOTIFICATION",
            "PUBG_SINGLES_GUN_PAYOFF_BYPASS",
            "PUBG_SINGLES_GUN_QUALITY_BYPASS",
            "PUBG_FIGHT_SCORE_MIN",
            "PUBG_PAYOFF_SCORE_MIN",
            "PUBG_QUALITY_SCORE_MIN",
            "PUBG_PAYOFF_SCORE_MIN_SINGLES",
            "PUBG_QUALITY_SCORE_MIN_SINGLES",
            "PUBG_EARLY_PAYOFF_REJECT_SINGLES",
            "PUBG_REJECT_LOOT_WALK",
        )
        if env.get(k) is not None
    }

    feed_log = Path("/root/data/mlbb/mlbb_vod_segment_feed.log")
    if feed_log.is_file():
        try:
            lines = feed_log.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
            interesting = [
                ln
                for ln in lines
                if any(
                    tok in ln.lower()
                    for tok in ("sent", "reject", "fight_low", "payoff", "loot", "singles", "presend")
                )
            ][-12:]
            ctx["recent_feed_lines"] = interesting
        except Exception:
            pass

    heartbeat = Path("/root/data/mlbb/vod_feed_heartbeat.json")
    if heartbeat.is_file():
        try:
            ctx["heartbeat"] = json.loads(heartbeat.read_text(encoding="utf-8"))
        except Exception:
            pass
    return ctx


def chat(
    user_text: str,
    *,
    env: dict[str, str] | None = None,
    with_context: bool = True,
    timeout: float = 90.0,
) -> str:
    if env is None:
        env = {**os.environ, **load_env()}
    key = api_key(env)
    if not key:
        return (
            "Kimi не настроена: нет MOONSHOT_API_KEY / KIMI_API_KEY.\n"
            "Добавьте ключ Moonshot в /root/.video_bot.env и перезапустите telegram-бот.\n"
            "После этого: /kimi что сейчас с качеством?"
        )

    messages: list[dict[str, str]] = [{"role": "system", "content": PUBG_SYSTEM_BRIEF}]
    if with_context:
        ctx = collect_context()
        messages.append(
            {
                "role": "system",
                "content": "Актуальный read-only контекст JSON:\n"
                + json.dumps(ctx, ensure_ascii=False)[:12000],
            }
        )
    messages.append({"role": "user", "content": user_text.strip() or "Краткий статус бота."})

    payload = {
        "model": model_name(env),
        "messages": messages,
        "temperature": float(env.get("KIMI_TEMPERATURE", "0.2")),
    }
    req = urllib.request.Request(
        f"{api_base(env)}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace")[:500]
        return f"Kimi HTTP {exc.code}: {err}"
    except Exception as exc:
        return f"Kimi error: {exc}"

    try:
        return str(body["choices"][0]["message"]["content"]).strip()
    except Exception:
        return f"Kimi bad response: {json.dumps(body, ensure_ascii=False)[:800]}"


def configured() -> bool:
    return bool(api_key())


def main() -> int:
    ap = argparse.ArgumentParser(description="Ask Kimi ops assistant")
    ap.add_argument("prompt", nargs="*", help="Question for Kimi")
    ap.add_argument("--no-context", action="store_true")
    ap.add_argument("--show-brief", action="store_true")
    ap.add_argument("--check", action="store_true", help="Print whether API key is set")
    args = ap.parse_args()
    if args.show_brief:
        print(PUBG_SYSTEM_BRIEF)
        return 0
    if args.check:
        print("configured" if configured() else "missing_api_key")
        return 0 if configured() else 2
    text = " ".join(args.prompt).strip() or "Дай краткий статус качества PUBG-бота и что проверить."
    print(chat(text, with_context=not args.no_context))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
