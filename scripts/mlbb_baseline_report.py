#!/usr/bin/env python3
"""Phase-0 baseline: vod_segment_labels stats + optional Telegram report to owner."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from collections import Counter
from pathlib import Path

DATA_MLBB = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))
LABELS_PATH = Path(os.environ.get("MLBB_VOD_SEGMENT_LABELS", str(DATA_MLBB / "vod_segment_labels.json")))


def analyze_labels() -> dict:
    if not LABELS_PATH.exists():
        return {"error": "no_labels_file", "path": str(LABELS_PATH)}

    data = json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    good = data.get("good", [])
    bad = data.get("bad", [])
    fb = data.get("feedback", [])
    yes = sum(1 for f in fb if f.get("owner_label") in ("yes", "good"))
    no = sum(1 for f in fb if f.get("owner_label") in ("no", "bad"))
    total = yes + no
    precision = yes / total if total else 0.0

    bad_hook = sum(1 for b in bad if float(b.get("hook_score") or 0) > 0.27)
    bad_score = sum(1 for b in bad if float(b.get("score") or 0) > 0.05)

    fail_reasons = Counter()
    for b in bad:
        gate = str(b.get("gate_reason") or b.get("pass_reason") or "").lower()
        reason = str(b.get("reason") or "").lower()
        if "freeze" in gate or "freeze" in reason:
            fail_reasons["freeze"] += 1
        elif "spawn" in gate or "spawn" in reason or "base" in gate:
            fail_reasons["spawn/base"] += 1
        elif "idle" in gate or "idle" in reason or "lane" in gate or "farm" in gate:
            fail_reasons["idle/lane"] += 1
        elif gate:
            fail_reasons[gate.split(":")[0][:32]] += 1
        elif reason and reason not in ("—", "button_dislike"):
            fail_reasons[reason[:32]] += 1
        else:
            fail_reasons["owner_dislike_no_gate"] += 1

    return {
        "good": len(good),
        "bad": len(bad),
        "feedback_total": len(fb),
        "precision": round(precision, 4),
        "yes": yes,
        "no": no,
        "bad_hook_gt_0_27": bad_hook,
        "bad_score_gt_0_05": bad_score,
        "top_bad_reasons": fail_reasons.most_common(5),
    }


def format_report(stats: dict) -> str:
    if stats.get("error"):
        return f"MLBB baseline: {stats['error']} ({stats.get('path', '')})"

    lines = [
        "📊 MLBB Highlight Bot — baseline (Phase 0)",
        f"Метки VOD: 👍{stats['yes']} 👎{stats['no']} (всего {stats['feedback_total']})",
        f"Precision 👍/(👍+👎): {stats['precision']:.0%}",
        f"Bad с hook>0.27: {stats['bad_hook_gt_0_27']}/{stats['bad']}",
        f"Bad со score>0.05: {stats['bad_score_gt_0_05']}/{stats['bad']}",
        "",
        "Топ причин 👎:",
    ]
    for reason, count in stats.get("top_bad_reasons", [])[:3]:
        lines.append(f"  • {reason}: {count}")
    lines.extend(
        [
            "",
            "Диагноз: 👎 не блокировали rescan (исправлено Phase 1).",
            "Следующий шаг: замкнутый loop 👎→block ±90с + VOD rescan.",
        ]
    )
    return "\n".join(lines)


def send_telegram(text: str) -> bool:
    token = (
        os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        or os.environ.get("TG_BOT_TOKEN", "").strip()
    )
    chat_id = (
        os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        or os.environ.get("TG_CHAT_ID", "").strip()
    )
    if not token or not chat_id:
        print("telegram_skip: missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID", file=sys.stderr)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=30) as resp:
            body = json.loads(resp.read().decode())
            return bool(body.get("ok"))
    except Exception as exc:
        print(f"telegram_error: {exc}", file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--telegram", action="store_true", help="Send report to owner via Telegram")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    stats = analyze_labels()
    report = format_report(stats)

    if args.json:
        print(json.dumps(stats, indent=2, ensure_ascii=False))
    else:
        print(report)

    if args.telegram:
        ok = send_telegram(report)
        if not ok:
            return 1
        print("telegram_sent=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
