#!/usr/bin/env bash
# Anti-hang + self-heal for daily multi-game cycle.
# Runs every 5 minutes via cron — must recover without a human ping.
set -euo pipefail

LOG=/root/data/mlbb/cycle_stall_watchdog.log
mkdir -p "$(dirname "$LOG")"

# Single-flight: two cron entries used to race and double-send SLA Telegram.
LOCK=/tmp/cycle_stall_watchdog.lock
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "===== cycle_stall_watchdog $(date -Is) skipped (lock busy) =====" >>"$LOG"
  exit 0
fi

exec >>"$LOG" 2>&1

if [[ -f /root/.video_bot.env ]]; then set -a; # shellcheck disable=SC1091
source /root/.video_bot.env; set +a; fi

REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
export PYTHONPATH="${REPO}/scripts:${PYTHONPATH:-}"
export DAILY_GAME_STALL_ZERO_RUNS="${DAILY_GAME_STALL_ZERO_RUNS:-4}"
export DAILY_GAME_STALL_MAX_SEC="${DAILY_GAME_STALL_MAX_SEC:-1200}"
export STALL_PROC_MAX_SEC="${STALL_PROC_MAX_SEC:-900}"
export DAILY_CYCLE_HOURLY_SLA="${DAILY_CYCLE_HOURLY_SLA:-1}"
export DAILY_CYCLE_SLA_SEC="${DAILY_CYCLE_SLA_SEC:-3600}"
export DAILY_GAME_UNSTALL_ON_INBOX="${DAILY_GAME_UNSTALL_ON_INBOX:-1}"
export SELF_HEAL_RECYCLE_LIMIT="${SELF_HEAL_RECYCLE_LIMIT:-6}"

echo "===== cycle_stall_watchdog $(date -Is) ====="

# Primary autonomous healer (unstall, recycle parked, kill hangs, SLA).
python3 "${REPO}/scripts/cycle_self_heal.py" || true

# Keep force-skip path for games with ZERO local media that spin discovery forever.
python3 - <<'PY'
import json, os, sys, time
sys.path.insert(0, os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml") + "/scripts")
from daily_game_cycle import (
    active_game,
    force_skip_game,
    is_game_stalled,
    load_state,
    note_feed_iteration,
    reset_if_new_day,
    status_summary,
    _game_inbox_ready,
    quota_remaining,
)

reset_if_new_day()
state = load_state()
summary = status_summary()
print("cycle", json.dumps(summary, ensure_ascii=False))
game = summary.get("active_game")
if game and quota_remaining(game) > 0 and not _game_inbox_ready(game):
    stall = (state.get("stall") or {}).get(game) or {}
    since = float(stall.get("since") or 0)
    max_sec = float(os.environ.get("DAILY_GAME_STALL_MAX_SEC", "1200"))
    sends = summary.get("sends") or {}
    if int(sends.get(game, 0) or 0) == 0 and not since:
        try:
            from datetime import datetime
            ua = state.get("updated_at") or ""
            if ua:
                since = datetime.strptime(ua, "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:
            since = 0
    # Only force-skip when there is truly nothing local to work on.
    if since and (time.time() - since) >= max_sec and not is_game_stalled(game):
        print(f"force_skip {game} no_local_media wall={(time.time()-since):.0f}s")
        force_skip_game(game, reason="watchdog_no_local_media")
        note_feed_iteration(game, 0)

print("next_active", active_game())
PY

echo "===== done $(date -Is) ====="
