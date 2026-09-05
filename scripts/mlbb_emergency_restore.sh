#!/usr/bin/env bash
# DEPRECATED Shorts emergency restore.
# Production VOD path is deploy_unified_production.sh — this script must not
# dual-start continuous_worker / telegram against the systemd VOD owner.
# Run on VPS: bash /root/content_bot_ml/scripts/mlbb_emergency_restore.sh
set -Eeuo pipefail

REPO="${REPO:-/root/content_bot_ml}"
BIN=/usr/local/bin
LOG=/root/data/mlbb/mlbb_continuous_worker.log
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
  echo "REFUSED: mlbb_emergency_restore.sh is Shorts-era and fights VOD ownership." >&2
  echo "Use: CONTENT_BOT_REPO=$REPO bash $REPO/scripts/deploy_unified_production.sh" >&2
  exit 2
fi

cd "$REPO"
git fetch origin
git checkout cursor/mlbb-video-pipeline-e712 2>/dev/null || git checkout main
git pull --ff-only

bash scripts/install_mlbb_only_mode.sh
bash scripts/mlbb_deploy_sync.sh

# Drop duplicate workers / orphan heavy jobs before restart.
pkill -f 'mlbb_continuous_worker.py' 2>/dev/null || true
pkill -f 'mlbb_calibration_feed.py' 2>/dev/null || true
sleep 2

set -a
# shellcheck disable=SC1091
source /root/.video_bot.env 2>/dev/null || true
set +a
export PYTHONPATH="${BIN}:${REPO}/scripts"

python3 - <<'PY'
import sys
sys.path.insert(0, "/usr/local/bin")
from mlbb_calibration_store import (
    backfill_ever_delivered,
    backfill_gameplay_flags,
    rebuild_index_from_disk,
    refill_pending_emergency,
    release_stale_claims,
    stats,
)
rebuild_index_from_disk()
released = release_stale_claims(max_age_sec=120)
delivered = backfill_ever_delivered()
refilled = refill_pending_emergency(limit=10)
backfill = backfill_gameplay_flags(limit=8)
s = stats()
print(f"released={released} delivered_sync={delivered} refilled={refilled} backfill={backfill} pending={s['pending']}")
PY

rm -f /tmp/mlbb_calibration_feed.lock
MLBB_FEED_RE_GATE=1 MLBB_FEED_TRY_INGEST=0 MLBB_FEED_SKIP_REBUILD=1 MLBB_OWNER_EMERGENCY=0 \
  timeout 90 python3 "$BIN/mlbb_calibration_feed.py" >>"$LOG" 2>&1 || true

if [[ "${VOD_FEED_ALLOW_NOHUP:-0}" != "1" ]]; then
  echo "REFUSED nohup continuous_worker — set VOD_FEED_ALLOW_NOHUP=1 only for legacy Shorts labs" >&2
  exit 2
fi
nohup python3 "$BIN/mlbb_continuous_worker.py" >>"$LOG" 2>&1 &
echo "worker pid=$!"

# Nudge one feed cycle if queue has items.
sleep 8
python3 "$BIN/mlbb_job_watchdog.py" --nudge 2>/dev/null || true

if ! pgrep -f telegram_upload_bot.py >/dev/null 2>&1; then
  if systemctl cat telegram-upload-bot.service >/dev/null 2>&1; then
    systemctl restart telegram-upload-bot.service || true
  elif [[ "${VOD_FEED_ALLOW_NOHUP:-0}" == "1" ]]; then
    nohup python3 "$BIN/telegram_upload_bot.py" >>/root/data/mlbb/telegram_upload_bot.log 2>&1 &
  else
    echo "REFUSED nohup telegram — enable telegram-upload-bot.service" >&2
  fi
fi

echo "=== processes ==="
pgrep -af 'mlbb_continuous_worker|mlbb_calibration_feed|telegram_upload_bot' || true
echo "=== tail log ==="
tail -15 "$LOG" 2>/dev/null || true
