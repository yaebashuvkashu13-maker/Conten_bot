#!/usr/bin/env bash
# Post-install checks for MLBB VOD-only mode. Exit 1 on any hard failure.
set -Eeuo pipefail

ENV_FILE="${ENV_FILE:-/root/.video_bot.env}"
FAIL=0
warn() { echo "WARN: $*"; }
die() { echo "FAIL: $*"; FAIL=1; }

[[ -f "$ENV_FILE" ]] || die "missing $ENV_FILE"

env_val() {
  local key="$1"
  grep "^${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'"
}

check_env() {
  local key="$1" expected="$2"
  local val
  val="$(env_val "$key")"
  if [[ "$val" != "$expected" ]]; then
    die "env $key=$val (want $expected)"
  fi
}

check_env MLBB_VOD_ONLY 1
check_env MLBB_VOD_DISABLED 0
check_env MLBB_CALIBRATION_FEED_ENABLED 0
check_env MLBB_VOD_NO_CROP 1
check_env MLBB_VOD_LANDSCAPE 1
check_env MLBB_VOD_VARIABLE_LENGTH 1
check_env MLBB_VOD_SEND_ONE 1
check_env MLBB_KILL_BANNER_MIN_TIER double
check_env HIGHLIGHT_PARALLEL_WORKERS 1
check_env MLBB_VOD_BANNER_PRESEND 0
check_env MLBB_VOD_BANNER_DISCOVER 0
check_env MLBB_VOD_BANNER_PREFILTER 0
check_env MLBB_VOD_OWNER_EXEMPLARS 1

forbidden=(
  mlbb_continuous_worker.py
  mlbb_calibration_feed.py
  mlbb_youtube_shorts_ingest.py
  mlbb_hero_shorts_montage.py
  highlight_train.py
)
for pat in "${forbidden[@]}"; do
  if pgrep -f "$pat" >/dev/null 2>&1; then
    die "forbidden process still running: $pat"
  fi
done

vod_pids=($(pgrep -f 'mlbb_vod_segment_feed.py' 2>/dev/null || true))
cycle_pids=($(pgrep -f 'daily_cycle_runner.py' 2>/dev/null || true))
supervisor_pids=($(pgrep -f 'mlbb_vod_segment_feed.sh' 2>/dev/null || true))
if [[ ${#vod_pids[@]} -eq 0 && ${#cycle_pids[@]} -eq 0 && ${#supervisor_pids[@]} -eq 0 ]]; then
  die "mlbb_vod_segment_feed / daily_cycle_runner not running"
fi
if [[ ${#vod_pids[@]} -gt 1 || ${#cycle_pids[@]} -gt 1 ]]; then
  die "duplicate vod feed pids: vod=${vod_pids[*]} cycle=${cycle_pids[*]}"
fi

if ! pgrep -f 'telegram_upload_bot.py' >/dev/null 2>&1; then
  warn "telegram_upload_bot not running"
fi

if [[ ! -x /usr/local/bin/mlbb_vod_segment_feed.sh ]]; then
  die "missing /usr/local/bin/mlbb_vod_segment_feed.sh"
fi

GOOD=$(find /root/content_bot_ml/data/highlight_exemplars/mobile_legends/good -name '*.mp4' 2>/dev/null | wc -l | tr -d ' ')
BAD=$(find /root/content_bot_ml/data/highlight_exemplars/mobile_legends/bad -name '*.mp4' 2>/dev/null | wc -l | tr -d ' ')
echo "exemplars good=$GOOD bad=$BAD"
if [[ "$GOOD" -lt 50 ]]; then
  warn "good exemplars < 50 — owner scoring weak"
fi

if crontab -l 2>/dev/null | grep -qE 'mlbb_calibration_feed|mlbb_youtube_shorts_ingest|mlbb_continuous_worker\.py|install_mlbb_only_mode|vps_auto_update'; then
  die "cron still references Shorts mode or old auto_update"
fi

if grep -q 'DISABLED: MLBB VOD-only' /usr/local/bin/mlbb_calibration_feed.sh 2>/dev/null; then
  echo "calibration_feed.sh: disabled stub OK"
else
  die "mlbb_calibration_feed.sh is not VOD-only stub"
fi

# --- VOD discovery smoke (catch partial deploy / API mismatch before claiming OK) ---
for dep in nightly_youtube_montage.py youtube_mlbb_vod_prefs.py youtube_download.py; do
  if [[ ! -f "/usr/local/bin/$dep" ]]; then
    die "missing /usr/local/bin/$dep — partial install"
  fi
done

python3 <<'PY' || die "VOD discovery API smoke failed"
import inspect
import sys

sys.path.insert(0, "/usr/local/bin")
import nightly_youtube_montage as n  # noqa: WPS433
import youtube_mlbb_vod_prefs as p  # noqa: WPS433

params = inspect.signature(n.discover_candidates).parameters
for name in ("youtube_duration_sp", "youtube_search_date", "youtube_freshness_sp", "max_age_days"):
    if name not in params:
        raise SystemExit(f"discover_candidates missing {name}")
if p.vod_youtube_freshness_sp({}) != "EgQIBBAB":
    raise SystemExit("freshness sp mismatch")
PY
echo "discovery API smoke: OK"

if command -v yt-dlp >/dev/null; then
  if ! timeout 50 yt-dlp --flat-playlist --playlist-end 1 \
    "https://www.youtube.com/results?search_query=MLBB+mythic+ranked&sp=EgQIBBAB" \
    --print "%(id)s" >/dev/null 2>&1; then
    die "YouTube fresh-search yt-dlp smoke failed"
  fi
  echo "yt-dlp fresh search smoke: OK"
fi

LOG=/root/data/mlbb/mlbb_vod_segment_feed.log
if [[ -f "$LOG" ]]; then
  last_start=$(grep -n "vod owner exemplars" "$LOG" | tail -1 | cut -d: -f1)
  if [[ -n "$last_start" ]]; then
    if tail -n +"$last_start" "$LOG" | grep -qE 'TypeError: discover_candidates|Unsupported url scheme: "ytsearchdate'; then
      die "VOD log has discovery errors since last feed start"
    fi
  fi
fi

echo "===== VOD-only verify $(date -Is) ====="
echo "vod_feed_pids=${vod_pids[*]:-} cycle_pids=${cycle_pids[*]:-} supervisor=${supervisor_pids[*]:-}"
echo "load: $(uptime | sed 's/.*load average: //')"
grep -E '^MLBB_VOD_ONLY=|^MLBB_VOD_DISABLED=|^MLBB_CALIBRATION_FEED_ENABLED=' "$ENV_FILE" || true

if [[ "$FAIL" -ne 0 ]]; then
  echo "VERIFY FAILED"
  exit 1
fi
echo "VERIFY OK"
exit 0
