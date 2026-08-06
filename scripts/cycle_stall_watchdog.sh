#!/usr/bin/env bash
# Anti-hang for daily multi-game cycle.
# - Kill live yt-dlp / hung feeds older than STALL_PROC_MAX_SEC
# - Force-skip a game stuck at 0 sends for too long
# - Ensure feed loop is alive while quotas remain
set -euo pipefail

LOG=/root/data/mlbb/cycle_stall_watchdog.log
mkdir -p "$(dirname "$LOG")"
exec >>"$LOG" 2>&1

if [[ -f /root/.video_bot.env ]]; then set -a; # shellcheck disable=SC1091
source /root/.video_bot.env; set +a; fi

REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
export PYTHONPATH="${REPO}/scripts:${PYTHONPATH:-}"
export DAILY_GAME_STALL_ZERO_RUNS="${DAILY_GAME_STALL_ZERO_RUNS:-4}"
export DAILY_GAME_STALL_MAX_SEC="${DAILY_GAME_STALL_MAX_SEC:-1200}"
STALL_PROC_MAX_SEC="${STALL_PROC_MAX_SEC:-1500}"

echo "===== cycle_stall_watchdog $(date -Is) ====="

# Kill yt-dlp / ffmpeg stuck on live HLS longer than budget.
python3 - <<'PY' || true
import os, signal, time
from pathlib import Path

max_age = float(os.environ.get("STALL_PROC_MAX_SEC", "1500"))
now = time.time()
killed = 0
for proc in Path("/proc").iterdir():
    if not proc.name.isdigit():
        continue
    try:
        cmdline = (proc / "cmdline").read_bytes().decode("utf-8", "replace")
    except OSError:
        continue
    if "yt-dlp" not in cmdline and "shooter_vod_segment_feed.py" not in cmdline:
        continue
    # Live HLS / googlevideo hang signatures appear in children; age on feed/yt-dlp itself.
    try:
        start = (proc / "stat").read_text().split()
        # field 22 = starttime ticks; use mtime of cmdline as proxy
        age = now - (proc / "cmdline").stat().st_mtime
    except OSError:
        continue
    # Prefer /proc/<pid> create time via stat st_ctime of the dir
    try:
        age = now - proc.stat().st_ctime
    except OSError:
        pass
    if age < max_age:
        continue
    try:
        os.kill(int(proc.name), signal.SIGTERM)
        killed += 1
        print(f"killed pid={proc.name} age={age:.0f}s cmd={cmdline[:120]!r}")
    except OSError as exc:
        print(f"kill fail {proc.name}: {exc}")
print(f"killed_hung={killed}")
PY

# Stall-skip + heartbeat via Python cycle state.
python3 - <<'PY'
import json, os, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml") + "/scripts")
from daily_game_cycle import (
    active_game,
    force_skip_game,
    is_game_stalled,
    load_state,
    note_feed_iteration,
    reset_if_new_day,
    status_summary,
)

reset_if_new_day()
state = load_state()
summary = status_summary()
print("cycle", json.dumps(summary, ensure_ascii=False))
print("stall", json.dumps(state.get("stall") or {}, ensure_ascii=False))

# If active game has had zero sends and process keeps spinning discovery, force skip
# once wall-clock stall exceeds limit (even if zero_runs not yet bumped by runner).
game = summary.get("active_game")
if game:
    stall = (state.get("stall") or {}).get(game) or {}
    since = float(stall.get("since") or 0)
    max_sec = float(os.environ.get("DAILY_GAME_STALL_MAX_SEC", "1200"))
    # Also use updated_at of cycle if stall.since missing but sends for game still 0
    # and day has been active a long time with only mlbb done.
    sends = summary.get("sends") or {}
    if int(sends.get(game, 0) or 0) == 0 and not since:
        # Bootstrap stall clock from cycle updated_at if present.
        try:
            from datetime import datetime
            ua = state.get("updated_at") or ""
            if ua:
                since = datetime.strptime(ua, "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:
            since = 0
    if int(sends.get(game, 0) or 0) == 0 and since and (time.time() - since) >= max_sec:
        if not is_game_stalled(game):
            print(f"force_skip {game} wall_clock_stall={(time.time()-since):.0f}s")
            force_skip_game(game, reason="watchdog_wall_clock_stall")
            # bump zero runs so runner path stays consistent
            note_feed_iteration(game, 0)
        else:
            print(f"already stalled {game}")

nxt = active_game()
print("next_active", nxt)

# Ensure daily feed loop is running while quotas remain.
need = nxt is not None
feed_alive = subprocess.run(
    ["pgrep", "-f", "mlbb_vod_segment_feed.sh|daily_cycle_loop.sh|daily_cycle_runner.py"],
    capture_output=True,
).returncode == 0
print("feed_alive", feed_alive, "need", need)
if need and not feed_alive:
    # Prefer install wrapper which already loops daily_cycle_runner.
    wrapper = "/usr/local/bin/mlbb_vod_segment_feed.sh"
    if Path(wrapper).exists():
        subprocess.Popen(["bash", wrapper], start_new_session=True)
        print("restarted", wrapper)
    else:
        loop = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml")) / "scripts/daily_cycle_loop.sh"
        subprocess.Popen(["bash", str(loop)], start_new_session=True)
        print("restarted", loop)
PY

echo "===== done $(date -Is) ====="
