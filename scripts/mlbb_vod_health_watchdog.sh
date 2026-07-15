#!/usr/bin/env bash
# Keep VPS in VOD-only shape: kill Shorts/zombies, ensure one VOD feed alive.
set -Eeuo pipefail
ENV_FILE="${ENV_FILE:-/root/.video_bot.env}"
LOG=/root/data/mlbb/logs/mlbb_vod_health.log
BIN=/usr/local/bin
mkdir -p "$(dirname "$LOG")"

log() { echo "[$(date -Is)] $*" >>"$LOG"; }

env_val() {
  local key="$1"
  grep "^${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'"
}

[[ -f "$ENV_FILE" ]] || exit 0
if [[ -f /root/data/mlbb/OWNER_BATCH_RUNNING ]]; then
  log "owner batch lock — skip health watchdog"
  exit 0
fi
if [[ "$(env_val MLBB_VOD_ONLY)" != "1" || "$(env_val MLBB_VOD_DISABLED)" == "1" ]]; then
  exit 0
fi

# Zombies that have repeatedly killed the box.
FORBIDDEN_PATTERNS="mlbb_continuous_worker mlbb_calibration_feed mlbb_youtube_shorts_ingest mlbb_hero_shorts_montage"
if [[ "$(env_val MLBB_LEARN_APPLY_TRAIN)" != "1" ]]; then
  FORBIDDEN_PATTERNS="highlight_train.py $FORBIDDEN_PATTERNS"
fi
for pat in $FORBIDDEN_PATTERNS; do
  if pgrep -f "$pat" >/dev/null 2>&1; then
    log "kill forbidden $pat"
    pkill -9 -f "$pat" 2>/dev/null || true
  fi
done

# Stuck owner sync one-liners from manual debugging.
pgrep -af 'sync_owner_learning' 2>/dev/null | grep -v mlbb_vod_segment_feed | while read -r line; do
  pid=$(echo "$line" | awk '{print $1}')
  [[ -n "$pid" ]] && kill -9 "$pid" 2>/dev/null && log "kill stuck sync pid=$pid"
done

if ! pgrep -f 'telegram_upload_bot.py' >/dev/null 2>&1; then
  log "restart telegram_upload_bot"
  nohup python3 "$BIN/telegram_upload_bot.py" >>/root/data/mlbb/telegram_upload_bot.log 2>&1 &
fi

if ! pgrep -f 'mlbb_vod_segment_feed.sh' >/dev/null 2>&1 \
  && ! pgrep -f 'daily_cycle_runner.py' >/dev/null 2>&1 \
  && ! pgrep -f 'shooter_vod_segment_feed.py' >/dev/null 2>&1 \
  && ! pgrep -f 'mlbb_vod_segment_feed.py' >/dev/null 2>&1; then
  log "restart vod supervisor"
  pkill -f 'mlbb_vod_segment_feed.sh' 2>/dev/null || true
  sleep 1
  rm -f /tmp/mlbb_vod_segment_feed.lock
  nohup "$BIN/mlbb_vod_segment_feed.sh" >>/root/data/mlbb/vod_only_supervisor.log 2>&1 &
fi

# Disk emergency — inbox cleanup when root is nearly full (common silence cause).
DISK_PCT="$(df / | awk 'NR==2 {gsub(/%/,""); print $5}')"
if [[ -n "$DISK_PCT" && "$DISK_PCT" -ge 95 ]]; then
  log "disk critical ${DISK_PCT}% — run vps_disk_cleanup"
  REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
  if [[ -x "$REPO/scripts/vps_disk_cleanup.sh" ]]; then
    bash "$REPO/scripts/vps_disk_cleanup.sh" >>"$LOG" 2>&1 || true
  fi
fi

# Daily cycle: when MLBB is active, shooter feeds must not block the pipeline.
if [[ "$(env_val DAILY_GAME_CYCLE_ENABLED)" == "1" ]]; then
  REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
  ACTIVE_GAME="$(python3 - <<'PY' 2>/dev/null || true
import sys
sys.path.insert(0, "/root/content_bot_ml/scripts")
try:
    from daily_game_cycle import active_game, reset_if_new_day
    reset_if_new_day()
    print(active_game() or "")
except Exception:
    print("")
PY
)"
  if [[ "$ACTIVE_GAME" == "mlbb" ]] && pgrep -f 'shooter_vod_segment_feed.py' >/dev/null 2>&1; then
    log "active_game=mlbb but shooter feed running — kill to unblock MLBB"
    pkill -9 -f 'shooter_vod_segment_feed.py' 2>/dev/null || true
    sleep 2
    rm -f /tmp/shooter_vod_segment_feed.lock 2>/dev/null || true
  fi
fi

# Daily cycle: MLBB feed must not run when another game is active (quota done).
if [[ "$(env_val DAILY_GAME_CYCLE_ENABLED)" == "1" ]] && pgrep -f 'mlbb_vod_segment_feed.py' >/dev/null 2>&1; then
  REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
  WRONG_GAME="$(python3 - <<'PY' 2>/dev/null || true
import sys
sys.path.insert(0, "/root/content_bot_ml/scripts")
try:
    from daily_game_cycle import active_game, reset_if_new_day
    reset_if_new_day()
    active = active_game()
    print("" if active == "mlbb" else (active or "done"))
except Exception:
    print("")
PY
)"
  if [[ -n "$WRONG_GAME" ]]; then
    log "mlbb feed running but active_game=$WRONG_GAME — kill to unblock cycle"
    pkill -9 -f 'mlbb_vod_segment_feed.py' 2>/dev/null || true
    sleep 2
    rm -f /tmp/mlbb_vod_segment_feed.lock
  fi
fi

# Zero-send streak: reset state when feed scans but nothing reaches Telegram.
if pgrep -f 'mlbb_vod_segment_feed.py' >/dev/null 2>&1; then
  CB_OUT="$(python3 - <<'PY' 2>/dev/null || true
import json, os, re, sys, time
from pathlib import Path

sys.path.insert(0, "/root/content_bot_ml/scripts")
state_path = Path("/root/data/mlbb/vod_segment_state.json")
if not state_path.exists():
    raise SystemExit(0)
try:
    from mlbb_vod_adaptive_gate import apply_circuit_breaker, streak_circuit_max, streak_from_state
    from vod_scan_state import invalidate_pool_cache
except Exception:
    raise SystemExit(0)

state = json.loads(state_path.read_text(encoding="utf-8"))
streak = streak_from_state(state)
if streak < streak_circuit_max():
    raise SystemExit(0)

log_path = Path("/root/data/mlbb/mlbb_vod_segment_feed.log")
now = time.time()
last_send = 0.0
ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
if log_path.exists():
    for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-6000:]:
        m = ts_re.match(line)
        if m and "sent=1 vod=" in line:
            try:
                last_send = max(last_send, time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")))
            except ValueError:
                pass
silence = int(os.environ.get("MLBB_VOD_CIRCUIT_SILENCE_SEC", "7200"))
if last_send and (now - last_send) < silence:
    raise SystemExit(0)

if not apply_circuit_breaker(state):
    raise SystemExit(0)
for row in state.get("vods") or []:
    invalidate_pool_cache(row)
state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"circuit_breaker_reset streak={streak}")
PY
)"
  if [[ -n "$CB_OUT" ]]; then
    log "$CB_OUT — kill feed for clean restart"
    pkill -9 -f 'mlbb_vod_segment_feed.py' 2>/dev/null || true
    sleep 2
    rm -f /tmp/mlbb_vod_segment_feed.lock
  fi
fi

# Crash-loop detector: repeated ValueError in banner discover kills every VOD scan.
FEED_LOG=/root/data/mlbb/mlbb_vod_segment_feed.log
if [[ -f "$FEED_LOG" ]]; then
  CRASH_N="$(grep -c 'ValueError: The truth value of an array' "$FEED_LOG" 2>/dev/null || echo 0)"
  if [[ "$CRASH_N" -gt 3 ]]; then
    log "detected banner discover crash loop (n=$CRASH_N) — need git pull + install"
  fi
  LOG_AGE_SEC=$(( $(date +%s) - $(stat -c %Y "$FEED_LOG" 2>/dev/null || echo 0) ))
  STUCK_SEC="${MLBB_VOD_FEED_STUCK_SEC:-1800}"
  HEARTBEAT=/root/data/mlbb/vod_pipeline_heartbeat.json
  HEARTBEAT_AGE_SEC=$(( $(date +%s) - $(stat -c %Y "$HEARTBEAT" 2>/dev/null || echo 0) ))
  HEARTBEAT_FRESH_SEC="$(env_val VOD_HEARTBEAT_FRESH_SEC)"
  HEARTBEAT_FRESH_SEC="${HEARTBEAT_FRESH_SEC:-600}"
  if [[ "$LOG_AGE_SEC" -gt "$STUCK_SEC" ]] && \
    [[ "$HEARTBEAT_AGE_SEC" -gt "$HEARTBEAT_FRESH_SEC" ]] && \
    { pgrep -f 'mlbb_vod_segment_feed.py' >/dev/null 2>&1 \
      || pgrep -f 'daily_cycle_runner.py' >/dev/null 2>&1 \
      || pgrep -f 'shooter_vod_segment_feed.py' >/dev/null 2>&1; }; then
    log "feed stuck log_age=${LOG_AGE_SEC}s — kill and restart"
    pkill -9 -f 'mlbb_vod_segment_feed.py' 2>/dev/null || true
    pkill -9 -f 'mlbb_vod_segment_feed.sh' 2>/dev/null || true
    sleep 2
    rm -f /tmp/mlbb_vod_segment_feed.lock
    nohup "$BIN/mlbb_vod_segment_feed.sh" >>/root/data/mlbb/vod_only_supervisor.log 2>&1 &
  fi
fi

# Silence alert — notify if no clip sent in N hours (pattern from stream-clip ops tools).
SILENCE_SEC="${MLBB_VOD_SILENCE_ALERT_SEC:-43200}"
if [[ "$SILENCE_SEC" -gt 0 ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
  TOKEN="${TG_BOT_TOKEN:-}"
  CHAT="${TG_CHAT_ID:-}"
  if [[ -n "$TOKEN" && -n "$CHAT" ]]; then
    python3 - "$SILENCE_SEC" <<'PY' >>"$LOG" 2>&1 || true
import json, os, re, sys, time, urllib.parse, urllib.request
from pathlib import Path

silence = int(sys.argv[1])
token = os.environ.get("TG_BOT_TOKEN", "")
chat = os.environ.get("TG_CHAT_ID", "")
if not token or not chat:
    raise SystemExit(0)
stamp_path = Path("/root/data/mlbb/vod_silence_alert.json")
now = time.time()

def last_sent_age() -> float:
    sent_path = Path("/root/data/mlbb/vod_segment_feed_sent.json")
    if sent_path.exists():
        try:
            data = json.loads(sent_path.read_text(encoding="utf-8"))
            ts = data.get("updated_at", "")
            if ts:
                return now - time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
        except Exception:
            pass
    log_path = Path("/root/data/mlbb/mlbb_vod_segment_feed.log")
    if not log_path.exists():
        return silence + 1
    last_ts = 0.0
    ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
    sent_re = re.compile(r"sent=(\d+) vod=")
    for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-8000:]:
        tm = ts_re.match(line)
        if not tm:
            continue
        try:
            line_ts = time.mktime(time.strptime(tm.group(1), "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue
        sm = sent_re.search(line)
        if sm and int(sm.group(1)) > 0:
            last_ts = max(last_ts, line_ts)
    return now - last_ts if last_ts else silence + 1

age = last_sent_age()
if age < silence:
    raise SystemExit(0)
state = {}
if stamp_path.exists():
    try:
        state = json.loads(stamp_path.read_text(encoding="utf-8"))
    except Exception:
        state = {}
last_alert = float(state.get("last_alert_ts") or 0)
if now - last_alert < silence:
    raise SystemExit(0)
hours = int(age // 3600)
msg = (
    f"⚠️ MLBB VOD: тишина ~{hours}ч — клипов не было.\n"
    f"Проверь: feed, диск, лог mlbb_vod_segment_feed.log"
)
data = urllib.parse.urlencode({"chat_id": chat, "text": msg}).encode()
urllib.request.urlopen(
    f"https://api.telegram.org/bot{token}/sendMessage",
    data=data,
    timeout=20,
)
stamp_path.write_text(json.dumps({"last_alert_ts": now}), encoding="utf-8")
print(f"silence alert sent age_sec={int(age)}")
PY
  fi
fi

exit 0
