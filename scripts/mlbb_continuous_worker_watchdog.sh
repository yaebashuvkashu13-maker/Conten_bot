#!/usr/bin/env bash
# Restart mlbb_continuous_worker if dead or heartbeat stale (>10 min).
set -euo pipefail

WORKER="/usr/local/bin/mlbb_continuous_worker.py"
LOG="/root/data/mlbb/mlbb_continuous_worker.log"
WLOG="/root/data/mlbb/logs/mlbb_continuous_watchdog.log"
PIDFILE="/root/data/mlbb/mlbb_continuous_worker.pid"
STATE="/root/data/mlbb/mlbb_continuous_state.json"
STALE_SEC="${MLBB_WORKER_STALE_SEC:-1800}"

mkdir -p "$(dirname "$WLOG")"

worker_pid() {
  local pid=""
  if [[ -f "$PIDFILE" ]]; then
    pid="$(tr -d ' \n' < "$PIDFILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      local cmd
      cmd="$(ps -p "$pid" -o args= 2>/dev/null || true)"
      if [[ "$cmd" == *"mlbb_continuous_worker.py"* ]]; then
        echo "$pid"
        return 0
      fi
    fi
  fi
  pgrep -fo "python3 ${WORKER}" 2>/dev/null || pgrep -fo "python3 /usr/local/bin/mlbb_continuous_worker.py" 2>/dev/null || true
}

state_stale() {
  [[ ! -f "$STATE" ]] && return 0
  python3 - <<PY
import json, time
from datetime import datetime
from pathlib import Path
stale = int("${STALE_SEC}")
try:
    d = json.loads(Path("${STATE}").read_text(encoding="utf-8"))
    ts = datetime.strptime(d["updated_at"], "%Y-%m-%d %H:%M:%S").timestamp()
    raise SystemExit(0 if time.time() - ts > stale else 1)
except Exception:
    raise SystemExit(0)
PY
}

restart_worker() {
  local reason="$1"
  echo "[$(date -Is)] restart continuous_worker reason=${reason}" >> "$WLOG"
  pkill -f "python3 /usr/local/bin/mlbb_continuous_worker.py" 2>/dev/null || true
  sleep 1
  nohup python3 "$WORKER" >> "$LOG" 2>&1 &
  echo $! > "$PIDFILE"
  echo "[$(date -Is)] started pid=$(cat "$PIDFILE")" >> "$WLOG"
  if [[ -f /root/.video_bot.env ]]; then
    # shellcheck disable=SC1091
    source /root/.video_bot.env
    if [[ -n "${TG_BOT_TOKEN:-}" && -n "${TG_CHAT_ID:-}" ]]; then
      curl -sS --noproxy '*' \
        -F "chat_id=${TG_CHAT_ID}" \
        -F "text=⚠️ MLBB worker restarted: ${reason}" \
        "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" >/dev/null 2>&1 || true
    fi
  fi
}

pid="$(worker_pid || true)"
if [[ -n "$pid" ]]; then
  if state_stale; then
    if pgrep -f 'mlbb_youtube_shorts_ingest.py' >/dev/null \
      || pgrep -f 'mlbb_calibration_feed.py' >/dev/null \
      || pgrep -f 'mlbb_purge_bad_shorts_queue.py' >/dev/null; then
      echo "[$(date -Is)] skip restart: heavy MLBB job still running" >> "$WLOG"
      exit 0
    fi
    kill "$pid" 2>/dev/null || true
    sleep 1
    restart_worker "stale_state"
  fi
else
  restart_worker "not_running"
fi
