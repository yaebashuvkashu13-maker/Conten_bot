#!/usr/bin/env bash
# DEPRECATED Shorts continuous-worker installer.
# Production VOD ownership is systemd via deploy_unified_production.sh.
# This script must not re-add watchdog crons or nohup workers on a VOD box.
set -Eeuo pipefail

REPO="${REPO:-/root/content_bot_ml}"
BIN=/usr/local/bin
ENV_FILE="${ENV_FILE:-/root/.video_bot.env}"

if [[ -f "$ENV_FILE" ]]; then
  set +u
  set -a
  # shellcheck disable=SC1091
  source "$ENV_FILE" 2>/dev/null || true
  set +a
  set -u
fi

if [[ "${MLBB_VOD_ONLY:-0}" == "1" ]] \
  || systemctl cat content-bot-vod-feed.service >/dev/null 2>&1; then
  echo "REFUSED: install_mlbb_continuous_worker.sh fights VOD ownership." >&2
  echo "Use: CONTENT_BOT_REPO=$REPO bash $REPO/scripts/deploy_unified_production.sh" >&2
  exit 2
fi

if [[ "${VOD_FEED_ALLOW_NOHUP:-0}" != "1" ]]; then
  echo "REFUSED: Shorts continuous worker requires VOD_FEED_ALLOW_NOHUP=1 (lab only)." >&2
  exit 2
fi

MARK="# mlbb-continuous-worker"

install -m 755 \
  "$REPO/scripts/mlbb_continuous_worker.py" \
  "$REPO/scripts/mlbb_continuous_worker_watchdog.sh" \
  "$REPO/scripts/mlbb_job_watchdog.py" \
  "$BIN/"

# Do not install sparse VOD feed cron — VOD is systemd-owned elsewhere.
TMP=$(mktemp)
crontab -l 2>/dev/null | grep -v "$MARK" \
  | grep -v 'mlbb-calibration-cron' \
  | grep -v 'mlbb_calibration_feed.sh' \
  | grep -v 'mlbb_youtube_shorts_ingest.sh' \
  | grep -v 'mlbb-vod-segment-cron' \
  | grep -v 'mlbb_vod_segment_feed.sh' \
  | grep -v 'mlbb_continuous_worker_watchdog' \
  >"$TMP" || true
echo "*/2 * * * * $BIN/mlbb_continuous_worker_watchdog.sh >>/root/data/mlbb/logs/mlbb_continuous_watchdog.log 2>&1 $MARK watchdog2" >>"$TMP"
crontab "$TMP"
rm -f "$TMP"

pkill -f mlbb_continuous_worker.py 2>/dev/null || true
sleep 1
nohup python3 "$BIN/mlbb_continuous_worker.py" >>/root/data/mlbb/mlbb_continuous_worker.log 2>&1 &

echo "OK mlbb continuous worker started (lab Shorts only)"
pgrep -af mlbb_continuous_worker || true
