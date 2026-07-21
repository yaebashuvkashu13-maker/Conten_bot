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
for pat in highlight_train.py mlbb_continuous_worker mlbb_calibration_feed \
  mlbb_youtube_shorts_ingest mlbb_hero_shorts_montage; do
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

# Crash-loop detector: repeated ValueError in banner discover kills every VOD scan.
FEED_LOG=/root/data/mlbb/mlbb_vod_segment_feed.log

# Daily cycle handoff / zero-send loop / hung runner (every 5 min with this watchdog).
if [[ "$(env_val DAILY_GAME_CYCLE_ENABLED)" == "1" ]] && [[ -x "$BIN/vod_cycle_watchdog.py" ]]; then
  python3 "$BIN/vod_cycle_watchdog.py" >>"$LOG" 2>&1 || true
fi

if [[ -f "$FEED_LOG" ]]; then
  CRASH_N="$(grep -c 'ValueError: The truth value of an array' "$FEED_LOG" 2>/dev/null || echo 0)"
  if [[ "$CRASH_N" -gt 3 ]]; then
    log "detected banner discover crash loop (n=$CRASH_N) — need git pull + install"
  fi
  LOG_AGE_SEC=$(( $(date +%s) - $(stat -c %Y "$FEED_LOG" 2>/dev/null || echo 0) ))
  STUCK_SEC="${MLBB_VOD_FEED_STUCK_SEC:-1800}"
  if [[ "$LOG_AGE_SEC" -gt "$STUCK_SEC" ]] && \
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
    last = 0.0
    for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-8000:]:
        m = re.search(r"sent=(\d+) vod=", line)
        if m and int(m.group(1)) > 0:
            last = now
    return now - last if last else silence + 1

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
