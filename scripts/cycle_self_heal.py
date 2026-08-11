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


def _ffprobe_duration(path: Path) -> float:
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        return float(out.decode().strip() or 0)
    except (subprocess.SubprocessError, ValueError, OSError):
        return 0.0


def _min_vod_sec(game: str) -> float:
    """Match shooter montage floor — never recycle 3–4min junk into inbox."""
    g = game.strip().lower()
    if g == "mlbb":
        try:
            return float(os.environ.get("MLBB_VOD_MIN_SEC", "480"))
        except ValueError:
            return 480.0
    try:
        base = float(os.environ.get("SHOOTER_VOD_MIN_SEC") or os.environ.get("MLBB_VOD_MIN_SEC") or "120")
    except ValueError:
        base = 120.0
    if os.environ.get("SHOOTER_VOD_MONTAGE", "1") == "1":
        try:
            floor = float(os.environ.get("SHOOTER_VOD_MONTAGE_MIN_VOD_SEC", "120"))
        except ValueError:
            floor = 120.0
        return max(base, floor)
    return base


def recycle_parked_batch(game: str, *, limit: int = 8) -> int:
    """Move longest *usable* parked VODs back to inbox so unstall/feed can use them.

    Size-only ranking previously flooded inbox with short high-bitrate clips
    (~3–4 min) that fail SHOOTER_VOD_MONTAGE_MIN_VOD_SEC and look like inbox_dead.
    """
    inbox, parked = _inbox_and_parked(game)
    if not parked.is_dir():
        return 0
    inbox.mkdir(parents=True, exist_ok=True)
    min_sec = _min_vod_sec(game)
    scored: list[tuple[float, Path]] = []
    for p in parked.glob("yt_*.mp4"):
        if not p.exists():
            continue
        dur = _ffprobe_duration(p)
        if dur < min_sec:
            continue
        scored.append((dur, p))
    scored.sort(key=lambda t: -t[0])
    moved = 0
    moved_names: set[str] = set()
    for dur, src in scored:
        if moved >= limit:
            break
        dest = inbox / src.name
        if dest.exists():
            continue
        try:
            src.rename(dest)
            moved += 1
            moved_names.add(src.name)
            _log(f"recycle {game}: {src.name} dur={dur:.0f}s → inbox")
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
        # Only reset recycle caps. Never blank-reopen every exhausted row —
        # that resurrected dead MLBB banner misses every watchdog tick.
        for row in data.get("vods") or []:
            if not isinstance(row, dict):
                continue
            if int(row.get("recycle_count") or 0) > 0:
                row["recycle_count"] = 0
                row.pop("last_recycle_at", None)
            path_name = Path(str(row.get("path") or "")).name
            if path_name and path_name in moved_names:
                row["exhausted"] = False
                row["reject_reason"] = ""
                row["last_scan_blocked"] = False
                row["last_scan_at"] = 0
        try:
            sp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
    if moved == 0 and scored:
        _log(f"recycle {game}: candidates={len(scored)} but inbox already has them")
    elif moved == 0:
        _log(f"recycle {game}: no parked VOD ≥{min_sec:.0f}s")
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
    """At-most-once-per-key Telegram notify (race-safe against duplicate cron)."""
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat = os.environ.get("TG_CHAT_ID", "").strip()
    if not token or not chat:
        return
    # Serialize duplicate watchdog crons so two */5 entries cannot both send.
    lock_path = Path(os.environ.get("DAILY_CYCLE_NOTIFY_LOCK", "/tmp/cycle_tg_notify.lock"))
    lock_fh = None
    try:
        lock_fh = open(lock_path, "a+", encoding="utf-8")
        import fcntl

        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
    except OSError:
        lock_fh = None

    try:
        state = load_state()
        notified = state.setdefault("notified", {})
        prev = str(notified.get(key) or "")
        # Default: once per calendar day for sla_breach:* ; 6h for other keys.
        if key.startswith("sla_breach:"):
            cooldown = float(os.environ.get("DAILY_CYCLE_SLA_NOTIFY_COOLDOWN_SEC", str(20 * 3600)))
        else:
            cooldown = float(os.environ.get("DAILY_CYCLE_NOTIFY_COOLDOWN_SEC", str(6 * 3600)))
        if prev:
            try:
                prev_ts = time.mktime(time.strptime(prev, "%Y-%m-%d %H:%M:%S"))
                if time.time() - prev_ts < cooldown:
                    return
            except ValueError:
                pass
        # Claim the slot before network I/O so a twin cron cannot double-send.
        notified[key] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_state(state)
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
        except Exception as exc:  # noqa: BLE001
            # Allow a retry later if Telegram itself failed.
            notified.pop(key, None)
            save_state(state)
            _log(f"tg notify fail: {exc}")
    finally:
        if lock_fh is not None:
            try:
                import fcntl

                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
                lock_fh.close()
            except OSError:
                pass


def _parse_send_ts(entry: dict) -> float:
    raw = str(entry.get("last_send_at") or "")
    if not raw:
        return 0.0
    try:
        return time.mktime(time.strptime(raw, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return 0.0


def _feed_busy() -> bool:
    """True when a cycle feed/ffmpeg is actively working — don't SLA-spam mid-run."""
    try:
        out = subprocess.check_output(["pgrep", "-af", "daily_cycle_runner|shooter_vod_segment_feed|mlbb_vod_segment_feed.py"], text=True)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False
    lines = [ln for ln in out.splitlines() if "pgrep" not in ln]
    return bool(lines)


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
    # Feed is mid-timeout (e.g. PUBG encode) — heal quietly, no Telegram spam.
    if _feed_busy() and os.environ.get("DAILY_CYCLE_SLA_SKIP_IF_BUSY", "1") == "1":
        _log(f"sla quiet (feed busy) age={age:.0f}s rem={rem}")
        for g in GAME_ORDER:
            if int(rem.get(g, 0) or 0) <= 0:
                continue
            if _game_has_local_media(g):
                recycle_parked_batch(g, limit=6)
                clear_stall(g, reason="hourly_sla_local_media")
        return
    # Self-heal first, then alert (at most once per day by default).
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


def heal_once(*, exclude_unstall: set[str] | frozenset[str] | None = None) -> dict:
    reset_if_new_day()
    report: dict = {"ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    exclude = {str(x).strip().lower() for x in (exclude_unstall or set()) if x}

    # Must outlive a healthy feed iteration (runner timeout) — never kill mid-scan.
    try:
        runner_to = float(os.environ.get("DAILY_CYCLE_RUN_TIMEOUT_SEC", "600"))
    except ValueError:
        runner_to = 600.0
    max_age = float(os.environ.get("STALL_PROC_MAX_SEC", "900"))
    max_age = max(max_age, runner_to + 120.0)
    report["killed"] = kill_hung_processes(max_age_sec=max_age)
    report["locks_cleared"] = clear_stale_locks()

    # Recycle parked for every game with remaining quota.
    recycled = {}
    for g in GAME_ORDER:
        if quota_remaining(g) <= 0:
            continue
        if g in exclude:
            continue
        recycled[g] = recycle_parked_batch(g, limit=int(os.environ.get("SELF_HEAL_RECYCLE_LIMIT", "6")))
    report["recycled"] = recycled

    cleared = [g for g in unstall_games_with_inbox() if g not in exclude]
    # Also unstall any rem>0 game that has local media (inbox OR parked just recycled).
    for g in GAME_ORDER:
        if g in exclude:
            continue
        if quota_remaining(g) <= 0:
            continue
        if _game_has_local_media(g):
            more = clear_stall(g, reason="self_heal_local_media")
            for x in more:
                if x not in cleared:
                    cleared.append(x)
    report["unstalled"] = cleared
    report["exclude_unstall"] = sorted(exclude)

    summary = status_summary()
    report["summary"] = summary
    game = active_game()
    report["active"] = game
    ensure_feed_alive(any(int(v) > 0 for v in (summary.get("remaining") or {}).values()))
    hourly_sla_check()
    return report


def run_self_heal(*, notify: bool = True) -> dict:
    """Alias used by runner / ops — notify flag reserved for future TG gating."""
    _ = notify
    return heal_once()


def main() -> int:
    if Path("/root/.video_bot.env").exists():
        # Env already sourced by shell wrapper usually; keep import-side defaults.
        pass
    report = heal_once()
    _log(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
