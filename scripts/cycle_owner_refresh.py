#!/usr/bin/env python3
"""Owner Telegram «обновить» / CLI — clear stalls, stop thrash VODs, restart feed."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from daily_game_cycle import GAME_ORDER, clear_stall, status_summary


_HARD_EXHAUST_PREFIXES = (
    "banner_probe_0",
    "banner_fast_ship_gated",
    "banner_fast_discover_0",
    "yield_banner_miss",
    "no_combat_peaks",
    "fast_panns_0",
    "combat_probe_0",
)


def _data_root(game: str) -> Path:
    g = game.upper()
    for key in (f"VOD_{g}_DATA_ROOT", f"SHOOTER_{g}_DATA_ROOT", f"{g}_DATA_ROOT"):
        raw = os.environ.get(key)
        if raw:
            return Path(raw)
    base = Path(os.environ.get("CONTENT_BOT_DATA", "/root/data"))
    return base / game


def _collapse_duplicates(vods: list[dict]) -> list[dict]:
    """One row per youtube id — prefer exhausted+reject over empty twin."""
    by_id: dict[str, dict] = {}
    no_id: list[dict] = []
    for row in vods:
        if not isinstance(row, dict):
            continue
        vid = str(row.get("id") or "").strip()
        if not vid:
            no_id.append(row)
            continue
        prev = by_id.get(vid)
        if prev is None:
            by_id[vid] = row
            continue
        prev_ex = bool(prev.get("exhausted"))
        row_ex = bool(row.get("exhausted"))
        prev_reason = str(prev.get("reject_reason") or "")
        row_reason = str(row.get("reject_reason") or "")
        if row_ex and not prev_ex:
            by_id[vid] = row
        elif prev_ex and not row_ex:
            continue
        elif row_reason and not prev_reason:
            by_id[vid] = row
        else:
            # Keep newer scan stamp when both similar.
            if float(row.get("last_scan_at") or 0) >= float(prev.get("last_scan_at") or 0):
                by_id[vid] = row
    return list(by_id.values()) + no_id


def harden_dead_vods(game: str = "mlbb") -> int:
    """Collapse duplicate registry rows; keep hard rejects exhausted."""
    path = _data_root(game) / "vod_segment_state.json"
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    vods = list(data.get("vods") or [])
    before = len(vods)
    vods = _collapse_duplicates(vods)
    touched = 0
    for row in vods:
        reason = str(row.get("reject_reason") or "")
        if any(reason.startswith(p) for p in _HARD_EXHAUST_PREFIXES):
            changed = False
            if not row.get("exhausted"):
                row["exhausted"] = True
                changed = True
            if row.get("last_scan_blocked"):
                row["last_scan_blocked"] = False
                changed = True
            need = max(
                int(row.get("soft_reopen_count") or 0),
                int(os.environ.get("MLBB_VOD_SOFT_REOPEN_MAX", "3")),
            )
            if int(row.get("soft_reopen_count") or 0) < need:
                row["soft_reopen_count"] = need
                changed = True
            if changed:
                touched += 1
    data["vods"] = vods
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return (before - len(vods)) + touched


def kick_feed_processes() -> int:
    """Stop runner/feeds so the shell wrapper respawns a clean cycle."""
    patterns = (
        "daily_cycle_runner.py",
        "mlbb_vod_segment_feed.py",
        "shooter_vod_segment_feed.py",
    )
    killed = 0
    for pat in patterns:
        proc = subprocess.run(["pkill", "-f", pat], check=False)
        if proc.returncode == 0:
            killed += 1
    # Stale locks after kill.
    for lock in Path("/tmp").glob("*_vod_segment_feed.lock"):
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass
    return killed


def ensure_wrapper_alive() -> bool:
    alive = (
        subprocess.run(
            ["pgrep", "-f", "mlbb_vod_segment_feed.sh"],
            capture_output=True,
        ).returncode
        == 0
    )
    if alive:
        return False
    wrapper = Path("/usr/local/bin/mlbb_vod_segment_feed.sh")
    if wrapper.exists():
        subprocess.Popen(["bash", str(wrapper)], start_new_session=True)
        return True
    return False


def owner_refresh(*, kick: bool = True) -> dict:
    """
    Manual recovery path for the owner Telegram command.

    - clear all game stalls (including sticky banner_dead via manual_*)
    - harden dead MLBB VODs / collapse duplicate registry rows
    - kill stuck feed workers so the wrapper starts a fresh run
    """
    cleared = clear_stall(None, reason="manual_owner_refresh")
    hardened = harden_dead_vods("mlbb")
    for g in GAME_ORDER:
        if g == "mlbb":
            continue
        # Shooter states may also carry thrash exhaust — collapse only.
        sp = _data_root(g) / "vod_segment_state.json"
        if not sp.exists():
            continue
        try:
            data = json.loads(sp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        before = len(data.get("vods") or [])
        data["vods"] = _collapse_duplicates(list(data.get("vods") or []))
        if len(data["vods"]) != before:
            data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            sp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    kicked = kick_feed_processes() if kick else 0
    time.sleep(1.0)
    restarted = ensure_wrapper_alive()
    summary = status_summary()
    return {
        "cleared_stalls": cleared,
        "hardened_mlbb": hardened,
        "kicked": kicked,
        "wrapper_restarted": restarted,
        "summary": summary,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def format_refresh_reply(report: dict) -> str:
    summary = report.get("summary") or {}
    sends = summary.get("sends") or {}
    rem = summary.get("remaining") or {}
    active = summary.get("active_game") or "—"
    lines = [
        "✅ Обновление запущено",
        f"снял stall: {', '.join(report.get('cleared_stalls') or ['—'])}",
        f"мертвые VOD: {report.get('hardened_mlbb', 0)}",
        f"перезапуск feed: {'да' if report.get('kicked') else 'нет'}"
        + (", wrapper ↑" if report.get("wrapper_restarted") else ""),
        f"сейчас: {active}",
        "осталось: "
        + " ".join(f"{g}={rem.get(g, 0)}" for g in GAME_ORDER),
        "сегодня: "
        + " ".join(f"{g}={sends.get(g, 0)}" for g in GAME_ORDER),
    ]
    return "\n".join(lines)


def main() -> int:
    report = owner_refresh(kick=True)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(format_refresh_reply(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
