#!/usr/bin/env python3
"""Weekly quality report from the clip ledger: 👍/👎, dislike reasons, metric medians."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text.replace("+00:00", "Z").rstrip("Z") + "Z", "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _median(vals: list[float]) -> float | None:
    clean = [float(v) for v in vals if isinstance(v, (int, float))]
    if not clean:
        return None
    return float(statistics.median(clean))


def _metric(row: dict[str, Any], *keys: str) -> float | None:
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    for key in keys:
        if key in metrics and isinstance(metrics[key], (int, float)):
            return float(metrics[key])
        if key in row and isinstance(row[key], (int, float)):
            return float(row[key])
    return None


def build_weekly_report(
    games: list[str] | None = None,
    *,
    days: int = 7,
    now: datetime | None = None,
) -> dict[str, Any]:
    from vod_clip_quality_ledger import iter_events

    games = games or ["pubg", "standoff", "wot", "mlbb"]
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=max(1, int(days)))
    report: dict[str, Any] = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_days": days,
        "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "games": {},
    }

    for game in games:
        events = []
        for row in iter_events(game):
            ts = _parse_ts(str(row.get("ts") or ""))
            if ts is None or ts < since:
                continue
            events.append(row)

        sent = [r for r in events if r.get("decision") == "sent"]
        rejects = [r for r in events if r.get("decision") == "reject"]
        feedback = [r for r in events if r.get("decision") == "feedback"]
        good = [r for r in feedback if r.get("label") == "good"]
        bad = [r for r in feedback if r.get("label") == "bad"]
        reasons = Counter(str(r.get("reason") or "unspecified") for r in bad)
        reject_reasons = Counter(str(r.get("reason") or "unspecified") for r in rejects)

        metric_bags: dict[str, list[float]] = defaultdict(list)
        for r in sent + rejects:
            for name, keys in (
                ("panns_gun", ("panns_gun", "panns_gun_max", "gun_panns", "gunshot_score")),
                ("gun_density", ("gun_density", "gunfire_density", "gunshot_density")),
                ("burst_ratio", ("burst_ratio", "gun_burst_ratio")),
                ("killfeed", ("killfeed", "killfeed_score", "killfeed_rank")),
                ("visual", ("visual", "visual_score", "clip_score", "motion_score")),
                ("center_motion", ("center_motion", "motion")),
            ):
                val = _metric(r, *keys)
                if val is not None:
                    metric_bags[name].append(val)

        total_fb = len(good) + len(bad)
        report["games"][game] = {
            "sent": len(sent),
            "rejects": len(rejects),
            "feedback_good": len(good),
            "feedback_bad": len(bad),
            "like_share": (len(good) / total_fb) if total_fb else None,
            "dislike_share": (len(bad) / total_fb) if total_fb else None,
            "top_dislike_reasons": reasons.most_common(12),
            "top_reject_reasons": reject_reasons.most_common(12),
            "metric_medians": {k: _median(v) for k, v in sorted(metric_bags.items())},
        }

        try:
            from vod_clip_quality_ledger import reject_reason_summary
            rs = reject_reason_summary(game, limit=2000)
            report["games"][game]["gun_bypass_admits"] = rs.get("gun_bypass_admits", 0)
            report["games"][game]["early_payoff_low"] = rs.get("early_payoff_low", 0)
            report["games"][game]["payoff_low"] = rs.get("payoff_low", 0)
        except Exception:
            report["games"][game].setdefault("gun_bypass_admits", 0)

    return report


def format_report_text(report: dict[str, Any]) -> str:
    lines = [
        f"VOD quality weekly ({report.get('window_days')}d)",
        f"since {report.get('since')} → {report.get('generated_at')}",
        "",
    ]
    for game, block in (report.get("games") or {}).items():
        like = block.get("like_share")
        dislike = block.get("dislike_share")
        like_s = f"{like:.0%}" if isinstance(like, float) else "—"
        dislike_s = f"{dislike:.0%}" if isinstance(dislike, float) else "—"
        lines.append(
            f"## {game.upper()}  sent={block.get('sent')}  "
            f"👍{block.get('feedback_good')} ({like_s})  "
            f"👎{block.get('feedback_bad')} ({dislike_s})  "
            f"rejects={block.get('rejects')}"
        )
        gb = block.get("gun_bypass_admits")
        if gb is not None:
            sent_n = int(block.get("sent") or 0)
            share = (gb / sent_n) if sent_n else 0.0
            lines.append(
                f"  gun_bypass={gb}/{sent_n} ({share:.0%})"
                f"  early_payoff={block.get('early_payoff_low', 0)}"
                f"  payoff_low={block.get('payoff_low', 0)}"
            )
        top = block.get("top_dislike_reasons") or []
        if top:
            lines.append("  dislike reasons: " + ", ".join(f"{k}×{v}" for k, v in top[:6]))
        rej = block.get("top_reject_reasons") or []
        if rej:
            lines.append("  reject gates: " + ", ".join(f"{k}×{v}" for k, v in rej[:6]))
        med = block.get("metric_medians") or {}
        if any(v is not None for v in med.values()):
            parts = [f"{k}={v:.3f}" for k, v in med.items() if isinstance(v, float)]
            if parts:
                lines.append("  medians: " + ", ".join(parts))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_report(
    report: dict[str, Any],
    *,
    out_dir: Path | None = None,
) -> tuple[Path, Path]:
    root = Path(out_dir or os.environ.get("VOD_QUALITY_REPORT_DIR", "/root/data/vod_quality_reports"))
    root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d", time.gmtime())
    json_path = root / f"weekly_{stamp}.json"
    text_path = root / f"weekly_{stamp}.txt"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    text_path.write_text(format_report_text(report), encoding="utf-8")
    latest = root / "weekly_latest.txt"
    latest.write_text(format_report_text(report), encoding="utf-8")
    return json_path, text_path


def _telegram_send(text: str) -> bool:
    if os.environ.get("VOD_QUALITY_REPORT_TELEGRAM", "1") != "1":
        return False
    try:
        from vod_telegram_env import send_message

        return send_message(text)
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--games", default="pubg,standoff,wot,mlbb")
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--telegram", action="store_true", help="force Telegram send")
    args = ap.parse_args(argv)
    games = [g.strip() for g in str(args.games).split(",") if g.strip()]
    report = build_weekly_report(games, days=args.days)
    out_dir = Path(args.out_dir) if args.out_dir else None
    json_path, text_path = write_report(report, out_dir=out_dir)
    text = format_report_text(report)
    print(text)
    print(f"wrote {json_path}")
    print(f"wrote {text_path}")
    if args.telegram or os.environ.get("VOD_QUALITY_REPORT_TELEGRAM", "1") == "1":
        ok = _telegram_send(text)
        print(f"telegram_sent={ok}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
