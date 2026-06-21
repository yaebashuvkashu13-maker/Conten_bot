#!/usr/bin/env bash
# Post-install checks for MLBB VOD-only mode. Exit 1 on any hard failure.
set -Eeuo pipefail

ENV_FILE="${ENV_FILE:-/root/.video_bot.env}"
FAIL=0
warn() { echo "WARN: $*"; }
die() { echo "FAIL: $*"; FAIL=1; }

[[ -f "$ENV_FILE" ]] || die "missing $ENV_FILE"

# shellcheck disable=SC1091
source "$ENV_FILE" 2>/dev/null || die "cannot source env"

check_env() {
  local key="$1" expected="$2"
  local val="${!key:-}"
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
check_env MLBB_VOD_OWNER_EXEMPLARS 1

forbidden=(
  mlbb_continuous_worker.py
  mlbb_calibration_feed.py
  mlbb_youtube_shorts_ingest.py
  mlbb_hero_shorts_montage.py
)
for pat in "${forbidden[@]}"; do
  if pgrep -f "$pat" >/dev/null 2>&1; then
    die "forbidden process still running: $pat"
  fi
done

vod_pids=($(pgrep -f 'mlbb_vod_segment_feed.py' 2>/dev/null || true))
if [[ ${#vod_pids[@]} -gt 1 ]]; then
  die "duplicate vod_segment_feed pids: ${vod_pids[*]}"
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

if crontab -l 2>/dev/null | grep -qE 'mlbb_calibration_feed|mlbb_youtube_shorts_ingest|mlbb_continuous_worker\.py'; then
  die "cron still references Shorts calibration or continuous worker"
fi

if grep -q 'DISABLED: MLBB VOD-only' /usr/local/bin/mlbb_calibration_feed.sh 2>/dev/null; then
  echo "calibration_feed.sh: disabled stub OK"
else
  die "mlbb_calibration_feed.sh is not VOD-only stub"
fi

echo "===== VOD-only verify $(date -Is) ====="
echo "vod_feed_pids=${vod_pids[*]:-(none)}"
grep -E 'MLBB_VOD_|MLBB_CALIBRATION_FEED|HIGHLIGHT_USE_OWNER' "$ENV_FILE" | sort || true

if [[ "$FAIL" -ne 0 ]]; then
  echo "VERIFY FAILED"
  exit 1
fi
echo "VERIFY OK"
exit 0
