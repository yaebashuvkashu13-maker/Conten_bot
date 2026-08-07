#!/usr/bin/env python3
"""
Autonomous cycle self-heal — run from cron every 5 minutes.

Goals (owner SLA):
1) Never idle for hours with remaining quota while local VODs exist
2) Kill hung feeds / yt-dlp / orphan ffmpeg
3) Unstall + recycle parked VODs without human pings
4) Hourly progress heartbeat: alert if rem>0 and no send in ≥1h
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml"))
sys.path.insert(0, str(REPO / "scripts"))

from daily_game_cycle import (  # noqa: E402
    GAME_ORDER,
    active_game,
    clear_stall,
    load_state,
    quota_remaining,
    reset_if_new_day,
    save_state,
    status_summary,
    unstall_games_with_inbox,
)


def _log(msg: str) -> None:
    print(msg, flush=True)


def _data_root(game: str) -> Path:
    g = game.upper()
    for key in (f"VOD_{g}_DATA_ROOT", f"SHOOTER_{g}_DATA_ROOT", f"{g}_DATA_ROOT"):
        raw = os.environ.get(key)
        if raw:
            return Path(raw)
    return Path(f"/root/data/{game}")


def _inbox_and_parked(game: str) -> tuple[Path, Path]:
    root = _data_root(game) / "youtube_nightly"
    return root / "inbox", root / "parked"


def _count_mp4(path: Path) -> int:
    if not path.is_dir():
        return 0
    try:
        return sum(1 for _ in path.glob("yt_*.mp4"))
    except OSError:
        return 0


def _game_has_local_media(game: str) -> bool:
    inbox, parked = _inbox_and_parked(game)
    if _count_mp4(inbox) > 0 or _count_mp4(parked) > 0:
        return True
    if game == "genshin":
        remount = Path("/root/data/genshin/remount")
        if _count_mp4(remount) > 0:
            return True
    return False


def recycle_parked_batch(game: str, *, limit: int = 8) -> int:
    """Move longest parked VODs back to inbox so unstall/feed can use them."""
    inbox, parked = _inbox_and_parked(game)
    if not parked.is_dir():
        return 0
    inbox.mkdir(parents=True, exist_ok=True)
    files = sorted(
        (p for p in parked.glob("yt_*.mp4")),
        key=lambda p: p.stat().st_size if p.exists() else 0,
        reverse=True,
    )
    moved = 0
    for src in files:
        if moved >= limit:
            break
        dest = inbox / src.name
        if dest.exists():
            continue
        try:
            src.rename(dest)
            moved += 1
            _log(f"recycle {game}: {src.name} → inbox")
        except OSError as exc:
            _log(f"recycle fail {game} {src.name}: {exc}")
    # Clear discovery pause / recycle caps in state so feed retries.
    for sp in (
        _data_root(game) / "vod_segment_state.json",
        _data_root(game) / "youtube_nightly" / "state.json",
    ):
        if not sp.exists():
            continue
        try:
            data = json.loads(sp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        data["discovery_pause_until"] = 0
        for row in data.get("vods") or []:
            if not isinstance(row, dict):
                continue
            if int(row.get("recycle_count") or 0) > 0 or row.get("exhausted"):
                row["recycle_count"] = 0
                row["exhausted"] = False
                row["reject_reason"] = ""
                row.pop("last_recycle_at", None)
        try:
            sp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
    return moved


def kill_hung_processes(*, max_age_sec: float) -> int:
    """Kill yt-dlp / feed / orphan ffmpeg older than max_age_sec."""
    now = time.time()
    patterns = (
        "yt-dlp",
        "shooter_vod_segment_feed.py",
        "mlbb_vod_segment_feed.py",
        "daily_cycle_runner.py",
    )
    killed = 0
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            cmdline = (proc / "cmdline").read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        hit = any(p in cmdline for p in patterns)
        # Orphan ffmpeg decoding VODs without a young parent feed is rare; only age-kill ffmpeg
        # when clearly attached to yt_ path and very old.
        if not hit:
            if "ffmpeg" in cmdline and "yt_" in cmdline:
                hit = True
            else:
                continue
        try:
            age = now - proc.stat().st_ctime
        except OSError:
            continue
        if age < max_age_sec:
            continue
        # Never kill the self-heal / watchdog python itself.
        if "cycle_self_heal" in cmdline or "cycle_stall_watchdog" in cmdline:
            continue
        try:
            os.kill(int(proc.name), signal.SIGTERM)
            killed += 1
            _log(f"killed pid={proc.name} age={age:.0f}s cmd={cmdline[:140]!r}")
        except OSError as exc:
            _log(f"kill fail {proc.name}: {exc}")
    return killed


def clear_stale_locks() -> int:
    """Remove feed locks whose holders are dead."""
    cleared = 0
    for lock in Path("/tmp").glob("*_vod_segment_feed.lock"):
        try:
            # If no process has the file open via flock, safe to truncate/recreate.
            # Use lsof if available; else check no matching feed process.
            name = lock.name.replace("_vod_segment_feed.lock", "")
            if name == "mlbb":
                pat = "mlbb_vod_segment_feed.py"
            else:
                pat = f"shooter_vod_segment_feed.py {name}"
            alive = subprocess.run(
                ["pgrep", "-f", pat],
                capture_output=True,
            ).returncode == 0
            if alive:
                continue
            lock.unlink(missing_ok=True)
            lock.touch()
            cleared += 1
            _log(f"cleared stale lock {lock}")
        except OSError as exc:
            _log(f"lock clear fail {lock}: {exc}")
    ytdlp = Path("/tmp/mlbb_vod_ytdlp.lock")
    if ytdlp.exists():
        alive = subprocess.run(["pgrep", "-f", "yt-dlp"], capture_output=True).returncode == 0
        if not alive:
            try:
                ytdlp.unlink(missing_ok=True)
                ytdlp.touch()
                cleared += 1
            except OSError:
                pass
    return cleared


def ensure_feed_alive(need: bool) -> None:
    if not need:
        return
    alive = subprocess.run(
        ["pgrep", "-f", "mlbb_vod_segment_feed.sh|daily_cycle_runner.py"],
        capture_output=True,
    ).returncode == 0
    if alive:
        return
    wrapper = Path("/usr/local/bin/mlbb_vod_segment_feed.sh")
    if wrapper.exists():
        subprocess.Popen(["bash", str(wrapper)], start_new_session=True)
        _log(f"restarted {wrapper}")
    else:
        loop = REPO / "scripts/daily_cycle_loop.sh"
        subprocess.Popen(["bash", str(loop)], start_new_session=True)
        _log(f"restarted {loop}")


def _tg_notify(text: str, *, key: str) -> None:
    """At-most-once-per-key-hour Telegram notify."""
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat = os.environ.get("TG_CHAT_ID", "").strip()
    if not token or not chat:
        return
    state = load_state()
    notified = state.setdefault("notified", {})
    prev = str(notified.get(key) or "")
    # Rate-limit: same key within 55 minutes → skip.
    if prev:
        try:
            prev_ts = time.mktime(time.strptime(prev, "%Y-%m-%d %H:%M:%S"))
            if time.time() - prev_ts < 55 * 60:
                return
        except ValueError:
            pass
    try:
        import urllib.parse
        import urllib.request

        data = urllib.parse.urlencode(
            {"chat_id": chat, "text": text[:3500]}
        ).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data,
            method="POST",
        )
        urllib.request.urlopen(req, timeout=20).read()
        notified[key] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_state(state)
    except Exception as exc:  # noqa: BLE001
        _log(f"tg notify fail: {exc}")


def _parse_send_ts(entry: dict) -> float:
    raw = str(entry.get("last_send_at") or "")
    if not raw:
        return 0.0
    try:
        return time.mktime(time.strptime(raw, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return 0.0


def hourly_sla_check() -> None:
    """Owner SLA: with rem>0, expect ≥1 send/hour across remaining games."""
    if os.environ.get("DAILY_CYCLE_HOURLY_SLA", "1") != "1":
        return
    summary = status_summary()
    rem = summary.get("remaining") or {}
    if not any(int(v) > 0 for v in rem.values()):
        return
    state = load_state()
    stall = state.get("stall") or {}
    now = time.time()
    last_any = 0.0
    for g in GAME_ORDER:
        last_any = max(last_any, _parse_send_ts(stall.get(g) or {}))
    # Also treat day start if no sends yet today for remaining games.
    age = now - last_any if last_any > 0 else 10**9
    sla_sec = float(os.environ.get("DAILY_CYCLE_SLA_SEC", "3600"))
    if age < sla_sec:
        _log(f"sla ok last_send_age={age:.0f}s rem={rem}")
        return
    # Self-heal first, then alert.
    for g in GAME_ORDER:
        if int(rem.get(g, 0) or 0) <= 0:
            continue
        if _game_has_local_media(g):
            recycle_parked_batch(g, limit=6)
            clear_stall(g, reason="hourly_sla_local_media")
    media = {g: _game_has_local_media(g) for g in GAME_ORDER if int(rem.get(g, 0) or 0) > 0}
    _log(f"sla BREACH age={age:.0f}s rem={rem} media={media}")
    _tg_notify(
        f"⚠️ SLA: нет отправок {int(age // 60)} мин при остатке квот {rem}.\n"
        f"Локальные VOD: {media}. Самолечение: recycle+unstall запущено.",
        key=f"sla_breach:{summary.get('day')}",
    )


def heal_once() -> dict:
    reset_if_new_day()
    report: dict = {"ts": time.strftime("%Y-%m-%d %H:%M:%S")}

    max_age = float(os.environ.get("STALL_PROC_MAX_SEC", "900"))
    report["killed"] = kill_hung_processes(max_age_sec=max_age)
    report["locks_cleared"] = clear_stale_locks()

    # Recycle parked for every game with remaining quota.
    recycled = {}
    for g in GAME_ORDER:
        if quota_remaining(g) <= 0:
            continue
        recycled[g] = recycle_parked_batch(g, limit=int(os.environ.get("SELF_HEAL_RECYCLE_LIMIT", "6")))
    report["recycled"] = recycled

    cleared = unstall_games_with_inbox()
    # Also unstall any rem>0 game that has local media (inbox OR parked just recycled).
    for g in GAME_ORDER:
        if quota_remaining(g) <= 0:
            continue
        if _game_has_local_media(g):
            more = clear_stall(g, reason="self_heal_local_media")
            for x in more:
                if x not in cleared:
                    cleared.append(x)
    report["unstalled"] = cleared

    summary = status_summary()
    report["summary"] = summary
    game = active_game()
    report["active"] = game
    ensure_feed_alive(any(int(v) > 0 for v in (summary.get("remaining") or {}).values()))
    hourly_sla_check()
    return report


def main() -> int:
    if Path("/root/.video_bot.env").exists():
        # Env already sourced by shell wrapper usually; keep import-side defaults.
        pass
    report = heal_once()
    _log(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
