#!/usr/bin/env bash
# Watchdog: restart mlbb_continuous_worker if dead/stale; keep Telegram bot alive.
set -euo pipefail

WORKER="/usr/local/bin/mlbb_continuous_worker.py"
LOG="/root/data/mlbb/mlbb_continuous_worker.log"
WLOG="/root/data/mlbb/logs/mlbb_continuous_watchdog.log"
PIDFILE="/root/data/mlbb/mlbb_continuous_worker.pid"
STATE="/root/data/mlbb/mlbb_continuous_state.json"
STALE_SEC="${MLBB_WORKER_STALE_SEC:-300}"
TELEGRAM_BOT="/usr/local/bin/telegram_upload_bot.py"
TELEGRAM_LOG="/root/data/mlbb/telegram_upload_bot.log"
ENV_FILE="/root/.video_bot.env"

mkdir -p "$(dirname "$WLOG")"
if [[ -f "$ENV_FILE" ]]; then
  set +u
  set -a
  # shellcheck disable=SC1091
  source "$ENV_FILE" 2>/dev/null || true
  set +a
  set -u
fi

log() {
  echo "[$(date -Is)] $*" >> "$WLOG"
}

telegram_heartbeat_stale() {
  local hb="/root/data/mlbb/telegram_bot_heartbeat.json"
  local max_age="${TELEGRAM_BOT_HEARTBEAT_STALE_SEC:-600}"
  [[ -f "$hb" ]] || return 1
  python3 - <<PY
import json, sys, time
from pathlib import Path
max_age = int("${max_age}")
try:
    data = json.loads(Path("${hb}").read_text(encoding="utf-8"))
    age = time.time() - float(data.get("ts") or 0)
except Exception:
    sys.exit(0)
sys.exit(0 if age > max_age else 1)
PY
}

restart_telegram_bot() {
  log "restart telegram_upload_bot"
  pkill -f "telegram_upload_bot.py" 2>/dev/null || true
  sleep 1
  nohup python3 "$TELEGRAM_BOT" >> "$TELEGRAM_LOG" 2>&1 &
}

if [[ -f /root/data/mlbb/OWNER_BATCH_RUNNING ]]; then
  log "owner batch lock — skip vod supervisor"
  exit 0
fi

# VOD-only wins — never restart Shorts worker when cutting long VODs.
if [[ "${MLBB_VOD_ONLY:-0}" == "1" && "${MLBB_VOD_DISABLED:-1}" == "0" ]]; then
  log "vod_only=1: kill Shorts worker/feed/ingest; ensure vod supervisor"
  pkill -f "mlbb_continuous_worker.py" 2>/dev/null || true
  pkill -f "mlbb_calibration_feed.py" 2>/dev/null || true
  pkill -f "mlbb_youtube_shorts_ingest.py" 2>/dev/null || true
  pkill -f "mlbb_hero_shorts_montage.py" 2>/dev/null || true
  rm -f /tmp/mlbb_calibration_feed.lock /tmp/mlbb_youtube_shorts_ingest.lock 2>/dev/null || true
  WATCHDOG_PY="/usr/local/bin/mlbb_job_watchdog.py"
  if [[ -f "$WATCHDOG_PY" ]]; then
    python3 "$WATCHDOG_PY" --nudge >> "$WLOG" 2>&1 || true
  fi
  if [[ -f "$TELEGRAM_BOT" ]] && ! pgrep -f "telegram_upload_bot.py" >/dev/null 2>&1; then
    restart_telegram_bot
  elif [[ -f "$TELEGRAM_BOT" ]] && pgrep -f "telegram_upload_bot.py" >/dev/null 2>&1 && telegram_heartbeat_stale; then
    log "telegram bot heartbeat stale — restart"
    restart_telegram_bot
  fi
  VOD_WRAPPER="/usr/local/bin/mlbb_vod_segment_feed.sh"
  if ! pgrep -f "mlbb_vod_segment_feed.sh" >/dev/null 2>&1 \
    && ! pgrep -f "daily_cycle_runner.py" >/dev/null 2>&1 \
    && ! pgrep -f "shooter_vod_segment_feed.py" >/dev/null 2>&1 \
    && ! pgrep -f "mlbb_vod_segment_feed.py" >/dev/null 2>&1; then
    if [[ -x "$VOD_WRAPPER" ]]; then
      nohup "$VOD_WRAPPER" >>/root/data/mlbb/vod_only_watchdog.log 2>&1 &
      log "started vod supervisor via wrapper pid=$!"
    else
      nohup env PYTHONPATH="/usr/local/bin" flock -n /tmp/mlbb_vod_segment_feed.lock \
        python3 -u /usr/local/bin/mlbb_vod_segment_feed.py \
        >>/root/data/mlbb/vod_only.log 2>&1 &
      log "started vod_segment_feed direct pid=$!"
    fi
  fi
  exit 0
fi

# Shorts calibration watchdog (legacy).
if [[ "${MLBB_VOD_DISABLED:-1}" == "1" || "${MLBB_CALIBRATION_FEED_ENABLED:-1}" == "1" ]]; then
  :
else
  exit 0
fi

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
  pgrep -f "python3.*mlbb_continuous_worker.py" | head -1 || true
}

state_stale() {
  [[ ! -f "$STATE" ]] && return 0
  if pgrep -f "mlbb_youtube_shorts_ingest.py" >/dev/null 2>&1 \
    || pgrep -f "mlbb_calibration_feed.py" >/dev/null 2>&1; then
    return 1
  fi
  python3 - <<PY
import json, os, time
from datetime import datetime
from pathlib import Path
stale = int("${STALE_SEC}")
pid = int("${pid}")
# Grace period for a freshly started worker — avoid restart thrash.
try:
    stat = Path(f"/proc/{pid}/stat").read_text().split()
    start_ticks = int(stat[21])
    uptime = float(Path("/proc/uptime").read_text().split()[0])
    hz = os.sysconf("SC_CLK_TCK")
    process_age = uptime - start_ticks / hz
    if process_age < 60:
        raise SystemExit(1)
except (OSError, ValueError, IndexError):
    raise SystemExit(0)
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
  log "restart continuous_worker reason=${reason}"
  python3 - <<'PY' 2>/dev/null || true
import sys
sys.path.insert(0, "/usr/local/bin")
from mlbb_calibration_store import release_stale_claims
print("released_claims", release_stale_claims(max_age_sec=120))
PY
  pkill -f "mlbb_continuous_worker.py" 2>/dev/null || true
  sleep 2
  if pgrep -f "mlbb_continuous_worker.py" >/dev/null 2>&1; then
    log "skip restart — worker already running"
    return 0
  fi
  set -a
  # shellcheck disable=SC1091
  source "$ENV_FILE" 2>/dev/null || true
  set +a
  nohup python3 "$WORKER" >> "$LOG" 2>&1 &
  echo $! > "$PIDFILE"
  log "started pid=$(cat "$PIDFILE")"
}

WATCHDOG_PY="/usr/local/bin/mlbb_job_watchdog.py"
if [[ -f "$WATCHDOG_PY" ]]; then
  python3 "$WATCHDOG_PY" --nudge >> "$WLOG" 2>&1 || true
fi

if [[ -f "$TELEGRAM_BOT" ]] && ! pgrep -f "telegram_upload_bot.py" >/dev/null 2>&1; then
  restart_telegram_bot
elif [[ -f "$TELEGRAM_BOT" ]] && pgrep -f "telegram_upload_bot.py" >/dev/null 2>&1 && telegram_heartbeat_stale; then
  log "telegram bot heartbeat stale — restart"
  restart_telegram_bot
fi

pid="$(worker_pid || true)"
if [[ -n "$pid" ]]; then
  if state_stale; then
    log "worker stale pid=${pid} — restart"
    kill "$pid" 2>/dev/null || true
    sleep 1
    restart_worker "stale_state"
  fi
else
  restart_worker "not_running"
fi
