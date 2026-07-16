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

kill_feed_tree() {
  # Kill supervisor + children. Prefer process kill over rm of flock inodes —
  # kernel releases flock when PID dies; unlinking while FD held creates dual feeds.
  pkill -9 -f 'mlbb_vod_segment_feed.py' 2>/dev/null || true
  pkill -9 -f 'shooter_vod_segment_feed.py' 2>/dev/null || true
  pkill -9 -f 'daily_cycle_runner.py' 2>/dev/null || true
  pkill -9 -f 'mlbb_vod_segment_feed.sh' 2>/dev/null || true
  sleep 2
  # Only remove locks when no feed process remains.
  if ! pgrep -f 'mlbb_vod_segment_feed\.py|shooter_vod_segment_feed\.py|daily_cycle_runner\.py' >/dev/null 2>&1; then
    rm -f /tmp/mlbb_vod_segment_feed.lock \
      /tmp/pubg_vod_segment_feed.lock \
      /tmp/standoff_vod_segment_feed.lock \
      /tmp/genshin_vod_segment_feed.lock \
      /tmp/wot_vod_segment_feed.lock \
      /tmp/shooter_vod_segment_feed.lock 2>/dev/null || true
  fi
}

feed_processes_running() {
  pgrep -f 'mlbb_vod_segment_feed.py' >/dev/null 2>&1 \
    || pgrep -f 'daily_cycle_runner.py' >/dev/null 2>&1 \
    || pgrep -f 'shooter_vod_segment_feed.py' >/dev/null 2>&1
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
# Match .py / explicit script names — NOT *watchdog.sh which also contains these stems.
FORBIDDEN_PATTERNS="highlight_train.py mlbb_continuous_worker.py mlbb_calibration_feed.py mlbb_youtube_shorts_ingest.py mlbb_hero_shorts_montage.py"
if [[ "$(env_val MLBB_LEARN_APPLY_TRAIN)" == "1" ]]; then
  FORBIDDEN_PATTERNS="mlbb_continuous_worker.py mlbb_calibration_feed.py mlbb_youtube_shorts_ingest.py mlbb_hero_shorts_montage.py"
fi
for pat in $FORBIDDEN_PATTERNS; do
  if pgrep -f "$pat" >/dev/null 2>&1; then
    log "kill forbidden $pat"
    pkill -9 -f "$pat" 2>/dev/null || true
  fi
done

# Stuck owner sync one-liners from manual debugging.
while read -r line; do
  pid=$(echo "$line" | awk '{print $1}')
  [[ -n "$pid" ]] && kill -9 "$pid" 2>/dev/null && log "kill stuck sync pid=$pid"
done < <(pgrep -af 'sync_owner_learning' 2>/dev/null | grep -v mlbb_vod_segment_feed || true)

if ! pgrep -f 'telegram_upload_bot.py' >/dev/null 2>&1; then
  log "restart telegram_upload_bot"
  nohup python3 "$BIN/telegram_upload_bot.py" >>/root/data/mlbb/telegram_upload_bot.log 2>&1 &
fi

if ! pgrep -f 'mlbb_vod_segment_feed.sh' >/dev/null 2>&1 \
  && ! pgrep -f 'daily_cycle_runner.py' >/dev/null 2>&1 \
  && ! pgrep -f 'shooter_vod_segment_feed.py' >/dev/null 2>&1 \
  && ! pgrep -f 'mlbb_vod_segment_feed.py' >/dev/null 2>&1; then
  log "restart vod supervisor"
  kill_feed_tree
  nohup "$BIN/mlbb_vod_segment_feed.sh" >>/root/data/mlbb/vod_only_supervisor.log 2>&1 &
fi

# Disk emergency — inbox cleanup when root is nearly full (common silence cause).
DISK_PCT="$(df / | awk 'NR==2 {gsub(/%/,""); print $5}')"
if [[ -n "$DISK_PCT" && "$DISK_PCT" -ge 90 ]]; then
  log "disk high ${DISK_PCT}% — run vps_disk_cleanup"
  REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
  if [[ -x "$REPO/scripts/vps_disk_cleanup.sh" ]]; then
    bash "$REPO/scripts/vps_disk_cleanup.sh" >>"$LOG" 2>&1 || true
  fi
fi

# Daily cycle: when MLBB is active, shooter feeds must not block the pipeline.
if [[ "$(env_val DAILY_GAME_CYCLE_ENABLED)" == "1" ]]; then
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
    rm -f /tmp/pubg_vod_segment_feed.lock \
      /tmp/standoff_vod_segment_feed.lock \
      /tmp/genshin_vod_segment_feed.lock \
      /tmp/wot_vod_segment_feed.lock \
      /tmp/shooter_vod_segment_feed.lock 2>/dev/null || true
  fi
fi

# Daily cycle: MLBB feed must not run when another game is active (quota done).
if [[ "$(env_val DAILY_GAME_CYCLE_ENABLED)" == "1" ]] && pgrep -f 'mlbb_vod_segment_feed.py' >/dev/null 2>&1; then
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
    if ! pgrep -f 'mlbb_vod_segment_feed\.py' >/dev/null 2>&1; then
      rm -f /tmp/mlbb_vod_segment_feed.lock
    fi
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
    if ! pgrep -f 'mlbb_vod_segment_feed\.py' >/dev/null 2>&1; then
      rm -f /tmp/mlbb_vod_segment_feed.lock
    fi
  fi
fi

# No-send / OCR-grind watchdog: heartbeat can stay fresh while zero clips go out.
if feed_processes_running; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
  NO_SEND_OUT="$(python3 - <<'PY' 2>/dev/null || true
import json, os, re, sys, time
from pathlib import Path

sys.path.insert(0, "/root/content_bot_ml/scripts")
now = time.time()
no_send_sec = int(os.environ.get("MLBB_VOD_NO_SEND_KILL_SEC", "3600"))
stall_sec = int(os.environ.get("MLBB_VOD_OCR_STALL_KILL_SEC", "2700"))
hb_path = Path("/root/data/mlbb/vod_pipeline_heartbeat.json")
watch_path = Path("/root/data/mlbb/vod_health_hb_watch.json")
sent_path = Path("/root/data/mlbb/vod_segment_feed_sent.json")
log_path = Path("/root/data/mlbb/mlbb_vod_segment_feed.log")

def last_sent_age() -> float:
    if sent_path.exists():
        try:
            data = json.loads(sent_path.read_text(encoding="utf-8"))
            ts = data.get("updated_at", "")
            if ts:
                return now - time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
        except Exception:
            pass
    if not log_path.exists():
        return no_send_sec + 1
    last_ts = 0.0
    ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
    sent_re = re.compile(r"sent=(\d+) vod=")
    for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-12000:]:
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
    return now - last_ts if last_ts else no_send_sec + 1

send_age = last_sent_age()
hb = {}
if hb_path.exists():
    try:
        hb = json.loads(hb_path.read_text(encoding="utf-8"))
    except Exception:
        hb = {}
stage = str(hb.get("stage") or "")
vod_id = str(hb.get("vod_id") or "")
progress = float(hb.get("progress") or 0.0)
hb_ts = float(hb.get("timestamp") or 0.0)
ocr_stage = any(k in stage for k in ("banner", "highlight", "candidate_scan", "fast_probe"))

watch = {}
if watch_path.exists():
    try:
        watch = json.loads(watch_path.read_text(encoding="utf-8"))
    except Exception:
        watch = {}

same = (
    watch.get("vod_id") == vod_id
    and watch.get("stage") == stage
    and abs(float(watch.get("progress") or 0.0) - progress) < 0.02
)
if same and watch.get("since_ts"):
    stall_age = now - float(watch["since_ts"])
else:
    stall_age = 0.0
    watch = {"vod_id": vod_id, "stage": stage, "progress": progress, "since_ts": now}
watch_path.write_text(json.dumps(watch), encoding="utf-8")

# no_send_kill should only trigger when there is no active VOD progress signal.
# If heartbeat is moving across stages/progress, allow long scans to continue.
if send_age >= no_send_sec and (not stage or not vod_id):
    print(f"no_send_kill age_sec={int(send_age)} stage={stage} vod={vod_id}")
    raise SystemExit(0)
if send_age >= no_send_sec and stage and vod_id and stall_age >= max(1800, no_send_sec * 0.5):
    print(f"no_send_stall_kill age_sec={int(send_age)} stall_sec={int(stall_age)} stage={stage} vod={vod_id}")
    raise SystemExit(0)
if ocr_stage and stall_age >= stall_sec and send_age >= min(stall_sec, no_send_sec):
    print(f"ocr_stall_kill stall_sec={int(stall_age)} send_age={int(send_age)} stage={stage} vod={vod_id}")
PY
)"
  if [[ -n "$NO_SEND_OUT" ]]; then
    log "$NO_SEND_OUT — kill full tree and restart"
    kill_feed_tree
    nohup "$BIN/mlbb_vod_segment_feed.sh" >>/root/data/mlbb/vod_only_supervisor.log 2>&1 &
  fi
fi

# Crash-loop detector: repeated ValueError in banner discover kills every VOD scan.
FEED_LOG=/root/data/mlbb/mlbb_vod_segment_feed.log
if [[ -f "$FEED_LOG" ]]; then
  CRASH_N="$(grep -c 'ValueError: The truth value of an array' "$FEED_LOG" 2>/dev/null || true)"
  CRASH_N="${CRASH_N:-0}"
  if [[ "$CRASH_N" -gt 3 ]]; then
    log "detected banner discover crash loop (n=$CRASH_N) — need git pull + install"
  fi
  LOG_MTIME="$(stat -c %Y "$FEED_LOG" 2>/dev/null || echo 0)"
  NOW_SEC="$(date +%s)"
  LOG_AGE_SEC=$(( NOW_SEC - LOG_MTIME ))
  # Long OCR / dense scans can be quiet on the log but keep heartbeat — require both stale.
  STUCK_SEC="${MLBB_VOD_FEED_STUCK_SEC:-2400}"
  HEARTBEAT=/root/data/mlbb/vod_pipeline_heartbeat.json
  HB_MTIME="$(stat -c %Y "$HEARTBEAT" 2>/dev/null || echo 0)"
  HEARTBEAT_AGE_SEC=$(( NOW_SEC - HB_MTIME ))
  HEARTBEAT_FRESH_SEC="$(env_val VOD_HEARTBEAT_FRESH_SEC)"
  HEARTBEAT_FRESH_SEC="${HEARTBEAT_FRESH_SEC:-900}"
  if [[ "$LOG_AGE_SEC" -gt "$STUCK_SEC" ]] && \
    [[ "$HEARTBEAT_AGE_SEC" -gt "$HEARTBEAT_FRESH_SEC" ]] && \
    feed_processes_running; then
    log "feed stuck log_age=${LOG_AGE_SEC}s hb_age=${HEARTBEAT_AGE_SEC}s — kill full tree and restart"
    kill_feed_tree
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
