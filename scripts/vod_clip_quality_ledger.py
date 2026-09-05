#!/usr/bin/env python3
"""Post-send clip quality ledger: metrics + admit reason + 👍/👎 linked to source VOD."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def ledger_path(game: str = "pubg") -> Path:
    root = Path(os.environ.get("VOD_QUALITY_LEDGER_DIR", "/root/data/vod_quality_ledger"))
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{game}_clip_ledger.jsonl"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def append_event(game: str, event: dict[str, Any]) -> None:
    row = {"ts": _now(), "game": game, **event}
    path = ledger_path(game)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def record_decision(
    game: str,
    *,
    clip_id: str,
    vod_id: str,
    vod_path: str = "",
    decision: str,
    reason: str,
    metrics: dict[str, Any] | None = None,
    peak_sec: float | None = None,
) -> None:
    """decision: admit | reject | sent | feedback."""
    append_event(
        game,
        {
            "clip_id": clip_id,
            "vod_id": vod_id,
            "vod_path": vod_path,
            "decision": decision,
            "reason": reason,
            "peak_sec": peak_sec,
            "metrics": metrics or {},
        },
    )


def record_send(
    game: str,
    *,
    clip_id: str,
    vod_id: str,
    rendered_path: str,
    metrics: dict[str, Any] | None = None,
    admit_reason: str = "sent",
    peak_sec: float | None = None,
    message_id: str | int | None = None,
) -> None:
    record_decision(
        game,
        clip_id=clip_id,
        vod_id=vod_id,
        vod_path=str(metrics.get("vod_path") if metrics else "") if metrics else "",
        decision="sent",
        reason=admit_reason,
        metrics={
            **(metrics or {}),
            "rendered_path": rendered_path,
            "message_id": message_id,
        },
        peak_sec=peak_sec,
    )


def record_feedback(
    game: str,
    *,
    clip_id: str,
    label: str,
    reason: str = "",
    vod_id: str = "",
) -> None:
    """label: good|bad / like|dislike."""
    norm = "good" if label in {"good", "like", "yes", "1", "👍"} else "bad"
    append_event(
        game,
        {
            "clip_id": clip_id,
            "vod_id": vod_id,
            "decision": "feedback",
            "reason": reason or norm,
            "label": norm,
        },
    )


def iter_events(game: str) -> list[dict[str, Any]]:
    path = ledger_path(game)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def feedback_stats_for_vod(game: str, vod_id: str) -> dict[str, int]:
    good = bad = sent = 0
    for row in iter_events(game):
        if str(row.get("vod_id") or "") != str(vod_id):
            continue
        if row.get("decision") == "sent":
            sent += 1
        if row.get("decision") == "feedback":
            if row.get("label") == "good":
                good += 1
            elif row.get("label") == "bad":
                bad += 1
    return {"sent": sent, "good": good, "bad": bad}


def record_heartbeat(game: str, *, reason: str = "feed_tick", metrics: dict[str, Any] | None = None) -> None:
    """Ops pulse so silence monitors know the feed path is writing the ledger."""
    append_event(
        game,
        {
            "clip_id": "",
            "vod_id": "",
            "decision": "heartbeat",
            "reason": reason,
            "metrics": metrics or {},
        },
    )


def latest_gate_event_age_sec(game: str, *, limit: int = 800) -> float | None:
    """Seconds since newest reject/sent/heartbeat, or None if ledger empty of gates."""
    import calendar

    now = time.time()
    newest: float | None = None
    for row in iter_events(game)[-max(1, int(limit)) :]:
        if str(row.get("decision") or "") not in {"reject", "sent", "heartbeat"}:
            continue
        ts = str(row.get("ts") or "")
        try:
            age = now - calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
        except ValueError:
            continue
        if newest is None or age < newest:
            newest = age
    return newest


def reject_reason_summary(game: str, *, limit: int = 500) -> dict[str, Any]:
    """Aggregate reject reasons + gun-bypass admit markers for drought tuning."""
    counts: dict[str, int] = {}
    gun_bypass = 0
    early_payoff = 0
    payoff_low = 0
    sent = 0
    rejected = 0
    heartbeats = 0
    for row in iter_events(game)[-max(1, int(limit)) :]:
        decision = str(row.get("decision") or "")
        reason = str(row.get("reason") or "")
        metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
        if decision == "heartbeat":
            heartbeats += 1
            continue
        if decision == "sent":
            sent += 1
            if (
                metrics.get("singles_gun_early_payoff_rescue")
                or metrics.get("singles_gun_payoff_bypass")
                or metrics.get("singles_gun_quality_bypass")
            ):
                gun_bypass += 1
            continue
        if decision != "reject":
            continue
        rejected += 1
        key = reason.split("=")[0].split(":")[0].strip() or "unknown"
        counts[key] = counts.get(key, 0) + 1
        if key.startswith("early_payoff") or "early_payoff" in reason:
            early_payoff += 1
        if key.startswith("payoff_low") or reason.startswith("payoff_low"):
            payoff_low += 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:20]
    return {
        "sent": sent,
        "rejected": rejected,
        "heartbeats": heartbeats,
        "early_payoff_low": early_payoff,
        "payoff_low": payoff_low,
        "gun_bypass_admits": gun_bypass,
        "top_rejects": top,
    }
