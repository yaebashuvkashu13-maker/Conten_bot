#!/usr/bin/env python3
"""Alert when PUBG/shooter feed sends nothing for too long; optionally recover inbox."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()

def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = val.strip().strip('"').strip("'")

def last_send_age_sec(game: str = "pubg") -> float | None:
    try:
        from vod_hang_detector import last_send_age_sec as _age

        return float(_age(game) or 0) or None
    except Exception:
        pass
    stamp = Path(f"/root/data/{game}/last_send_ts")
    if stamp.exists():
        try:
            return max(0.0, time.time() - float(stamp.read_text().strip()))
        except (OSError, ValueError):
            return None
    return None

def telegram_send(text: str) -> bool:
    try:
        from vod_telegram_env import send_message

        return send_message(text)
    except Exception:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode(
        {"chat_id": chat, "text": text[:3500], "disable_web_page_preview": "1"}
    ).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=body, method="POST"), timeout=20)
        return True
    except Exception:
        return False

def _reject_ops_line(game: str) -> tuple[dict, str]:
    try:
        from vod_clip_quality_ledger import reject_reason_summary

        summary = reject_reason_summary(game, limit=int(_env("VOD_DROUGHT_LEDGER_LIMIT", "500") or 500))
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}, ""
    sent = int(summary.get("sent") or 0)
    rejected = int(summary.get("rejected") or 0)
    bypass = int(summary.get("gun_bypass_admits") or 0)
    denom = sent + rejected
    reject_pct = (100.0 * rejected / denom) if denom else 0.0
    bypass_pct = (100.0 * bypass / sent) if sent else 0.0
    top = summary.get("top_rejects") or []
    top_s = ",".join(f"{k}×{v}" for k, v in top[:3]) or "-"
    line = (
        f"rejects={rejected}/{denom} ({reject_pct:.0f}%) "
        f"gun_bypass={bypass}/{sent} ({bypass_pct:.0f}%) top={top_s}"
    )
    return summary, line

def maybe_recover(game: str) -> dict:
    if _env("VOD_DROUGHT_AUTO_RECOVER", "1") != "1":
        return {"skipped": True}
    try:
        from vod_inbox_recover import clear_exhausted, drop_live_stubs, unpark_recent

        removed = drop_live_stubs(game)
        moved = unpark_recent(game, limit=int(_env("VOD_DROUGHT_UNPARK", "5") or 5))
        cleared = clear_exhausted(game, moved or None)
        return {"removed_stubs": removed, "unparked": moved, "cleared": cleared}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--game", default="pubg")
    ap.add_argument("--hours", type=float, default=float(_env("VOD_DROUGHT_HOURS", "2") or 2))
    ap.add_argument("--env-file", default="/root/.video_bot.env")
    args = ap.parse_args(argv)
    _load_env_file(Path(args.env_file))

    age = last_send_age_sec(args.game)
    limit = max(0.25, float(args.hours)) * 3600.0
    state_path = Path(_env("VOD_DROUGHT_STATE", f"/root/data/{args.game}/drought_watch.json"))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    prev = {}
    if state_path.exists():
        try:
            prev = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            prev = {}

    report = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "game": args.game,
        "age_sec": age,
        "limit_sec": limit,
    }

    try:
        from vod_clip_quality_ledger import latest_gate_event_age_sec

        gate_age = latest_gate_event_age_sec(args.game)
    except Exception:
        gate_age = None
    report["ledger_gate_age_sec"] = gate_age
    silence_h = float(_env("VOD_LEDGER_SILENCE_HOURS", "3") or 3)
    ledger_silent = gate_age is None or gate_age >= silence_h * 3600.0
    report["ledger_silent"] = bool(ledger_silent)

    drought = age is not None and age >= limit
    unknown = age is None
    if unknown:
        report["status"] = "unknown_last_send"
    elif drought:
        report["status"] = "drought"
    else:
        report["status"] = "ok"

    summary, ops_line = _reject_ops_line(args.game)
    report["reject_summary"] = summary

    recover = {}
    if drought and not unknown:
        recover = maybe_recover(args.game)
        report["recover"] = recover

    last_alert = float(prev.get("last_alert_age") or 0)
    last_ledger_alert = float(prev.get("last_ledger_alert_ts") or 0)
    should_alert = False
    parts: list[str] = []

    if drought:
        should_alert = (age - last_alert) >= (limit * 0.5)
        parts = [
            f"⚠️ VOD drought [{args.game}]",
            f"no sends for {age / 3600.0:.1f}h (limit {args.hours:.1f}h)",
            ops_line or "rejects=n/a",
            f"recover={json.dumps(recover, ensure_ascii=False)}",
        ]
    elif unknown:
        should_alert = (time.time() - float(prev.get("last_unknown_alert_ts") or 0)) > 1800
        parts = [
            f"⚠️ VOD drought watch [{args.game}]",
            "last_send unknown (no stamp / hang detector miss)",
            ops_line or "rejects=n/a",
        ]
    elif ledger_silent:
        should_alert = (time.time() - last_ledger_alert) > 1800
        parts = [
            f"⚠️ VOD ledger silence [{args.game}]",
            f"sends ok (age={age/3600:.1f}h) but ledger gate silent",
            ops_line or "rejects=n/a",
        ]

    if ledger_silent and parts:
        age_s = "none" if gate_age is None else f"{gate_age/3600:.1f}h"
        parts.append(f"ledger_gate_age={age_s} (silence>{silence_h:.0f}h)")

    if should_alert and parts:
        report["alerted"] = telegram_send("\n".join(parts))
        if drought and age is not None:
            report["last_alert_age"] = age
        if unknown:
            report["last_unknown_alert_ts"] = time.time()
        if ledger_silent:
            report["last_ledger_alert_ts"] = time.time()
    else:
        report["alerted"] = False
        report["last_alert_age"] = last_alert
        report["last_ledger_alert_ts"] = prev.get("last_ledger_alert_ts")
        report["last_unknown_alert_ts"] = prev.get("last_unknown_alert_ts")

    state_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
