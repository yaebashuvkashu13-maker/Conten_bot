#!/usr/bin/env python3
"""Analyze delivery click research xlsx and post summary to Telegram owner."""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

try:
    from openpyxl import load_workbook
except ImportError:
    print("pip install openpyxl", file=sys.stderr)
    raise SystemExit(1)

INBOX = Path("/root/research/inbox")
OUT = Path("/root/data/mlbb/research_reports")
ENV_FILE = Path("/root/.video_bot.env")

# Known export layout (исследование клика)
STAGE_SPECS = [
    ("ожидание_до_сборки", 3, 4),  # создание → начало сборки
    ("сборка", 4, 6),  # начало сборки → завершение
    ("собран_до_поиска_курьера", 6, 7),  # завершение сборки → старт поиска
    ("поиск_курьера", 7, 8),  # поиск → назначение
    ("назначение_до_расхолда", 8, 9),
    ("расхолд_до_прибытия", 9, 10),
    ("прибытие_до_старта_доставки", 10, 11),
    ("собран_до_прибытия_курьера", 6, 10),  # ключевая «собран, курьер едет/ждёт»
]


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key.startswith("TG_"):
            env[key] = value
    if not ENV_FILE.exists():
        return env
    for raw in ENV_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key:
            env[key] = value
    return env


def tg_send(text: str, env: dict[str, str] | None = None) -> None:
    env = env or load_env()
    token = env.get("TG_BOT_TOKEN", "")
    chat = env.get("TG_CHAT_ID", "")
    if not token or not chat:
        print("TG_BOT_TOKEN/TG_CHAT_ID missing", file=sys.stderr)
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:3900]}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode())
    if not body.get("ok"):
        print("TG send failed:", body, file=sys.stderr)


def parse_dt(v) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        s = v.strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M:%S"):
            try:
                return datetime.strptime(s[:19], fmt)
            except ValueError:
                continue
    return None


def delta_minutes(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    sec = (end - start).total_seconds()
    if sec < 0:
        return None
    return sec / 60.0


def minutes_list(vals: list[float]) -> dict[str, float]:
    if not vals:
        return {"count": 0, "p50_min": 0, "p90_min": 0, "p95_min": 0, "avg_min": 0}
    vals_sorted = sorted(vals)
    n = len(vals_sorted)
    return {
        "count": n,
        "p50_min": vals_sorted[int(n * 0.5)],
        "p90_min": vals_sorted[min(int(n * 0.9), n - 1)],
        "p95_min": vals_sorted[min(int(n * 0.95), n - 1)],
        "avg_min": sum(vals_sorted) / n,
    }


def parse_cte_minutes(v) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        return None


def analyze_file(path: Path) -> dict:
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.active

    header_row = 1
    max_rows = int(os.environ.get("RESEARCH_MAX_ROWS", "0"))
    last_row = ws.max_row or header_row
    if max_rows > 0:
        last_row = min(last_row, header_row + max_rows)

    stage_durations: dict[str, list[float]] = {name: [] for name, _, _ in STAGE_SPECS}
    cte_values: list[float] = []
    problems = {
        "поиск_курьера_>15м": [],
        "собран_ждёт_курьера_>20м": [],  # col6→col10
        "сборка_>25м": [],
        "прибытие_до_старта_>10м": [],
        "отрицательные_цепочки": 0,
    }

    rows_analyzed = 0
    row_iter = ws.iter_rows(
        min_row=header_row + 1,
        max_row=last_row,
        min_col=1,
        max_col=11,
        values_only=True,
    )
    for row in row_iter:
        if not row:
            continue
        order_id = row[0]
        if order_id is None or str(order_id).strip() == "":
            continue

        cte = parse_cte_minutes(row[1] if len(row) > 1 else None)
        if cte is not None and cte > 0:
            cte_values.append(cte)

        # cols 3–11 (даты этапов)
        times = [parse_dt(row[i]) if len(row) > i else None for i in range(2, 11)]

        for name, col_a, col_b in STAGE_SPECS:
            ia, ib = col_a - 3, col_b - 3
            if 0 <= ia < len(times) and 0 <= ib < len(times):
                dm = delta_minutes(times[ia], times[ib])
                if dm is not None:
                    stage_durations[name].append(dm)

        search_m = delta_minutes(times[4], times[5]) if len(times) > 5 else None
        assembled_wait = delta_minutes(times[3], times[7]) if len(times) > 7 else None
        pick_m = delta_minutes(times[1], times[3]) if len(times) > 3 else None
        arrive_m = delta_minutes(times[7], times[8]) if len(times) > 8 else None

        if search_m is not None and search_m > 15:
            problems["поиск_курьера_>15м"].append(search_m)
        if assembled_wait is not None and assembled_wait > 20:
            problems["собран_ждёт_курьера_>20м"].append(assembled_wait)
        if pick_m is not None and pick_m > 25:
            problems["сборка_>25м"].append(pick_m)
        if arrive_m is not None and arrive_m > 10:
            problems["прибытие_до_старта_>10м"].append(arrive_m)

        rows_analyzed += 1
        if rows_analyzed % 50000 == 0:
            print(f"  … {rows_analyzed} rows", file=sys.stderr, flush=True)

    sheet_title = ws.title
    wb.close()

    stage_stats = {k: minutes_list(v) for k, v in stage_durations.items() if v}
    problem_stats = {k: minutes_list(v) if isinstance(v, list) else v for k, v in problems.items()}

    return {
        "file": str(path),
        "sheet": sheet_title,
        "rows_analyzed": rows_analyzed,
        "cte_minutes": minutes_list(cte_values),
        "stage_stats_minutes": stage_stats,
        "problem_orders": {k: v for k, v in problem_stats.items() if isinstance(v, dict) and v.get("count")},
        "problem_skip_count": problems["отрицательные_цепочки"],
    }


def main() -> int:
    env = load_env()
    for key, value in env.items():
        os.environ.setdefault(key, value)

    files = sorted(INBOX.glob("*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        print("no xlsx in", INBOX)
        tg_send("В inbox нет .xlsx для анализа.", env=env)
        return 0

    OUT.mkdir(parents=True, exist_ok=True)
    target = files[0]
    print("analyzing", target.name, flush=True)
    try:
        summary = analyze_file(target)
    except Exception as exc:
        tg_send(f"Ошибка анализа {target.name}: {exc}", env=env)
        raise

    lines = [
        "📊 Исследование клика — доставка",
        f"Файл: {target.name}",
        f"Строк с заказом: {summary['rows_analyzed']:,}".replace(",", " "),
        "",
        "CTE факт (мин.) — по колонке B:",
    ]
    cte = summary["cte_minutes"]
    lines.append(f"• P50={cte['p50_min']:.1f}  P90={cte['p90_min']:.1f}  avg={cte['avg_min']:.1f}  n={cte['count']}")

    lines.append("")
    lines.append("Этапы (минуты, P50 / P90):")
    for name, st in summary["stage_stats_minutes"].items():
        lines.append(f"• {name}: P50={st['p50_min']:.1f} P90={st['p90_min']:.1f} avg={st['avg_min']:.1f}")

    lines.append("")
    lines.append("Проблемные заказы (доля от всех):")
    total = summary["rows_analyzed"] or 1
    for name, st in sorted(
        summary["problem_orders"].items(),
        key=lambda x: -x[1].get("count", 0),
    ):
        cnt = int(st["count"])
        pct = 100.0 * cnt / total
        lines.append(f"• {name}: {cnt:,} ({pct:.1f}%) P50={st['p50_min']:.0f}м".replace(",", " "))

    lines.extend(
        [
            "",
            "💡 Идеи (по данным):",
            "1) Автозаявка курьера в момент «завершение сборки» (сейчас зазор собран→поиск).",
            "2) SLA на «поиск курьера» >15м — алерт диспетчеру.",
            "3) Приоритет очереди сборщика, если собран→прибытие курьера >20м растёт.",
            "4) Ускорить handoff на точке: прибытие→старт доставки >10м.",
        ]
    )

    report_path = OUT / f"delivery_report_{int(time.time())}.json"
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    msg = "\n".join(lines) + f"\n\nJSON: {report_path}"
    tg_send(msg, env=env)
    print("done", report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
