#!/usr/bin/env bash
# Deploy PUBG brawl-direct montage pipeline and restart batch on VPS.
set -Eeuo pipefail

REPO="${REPO:-/root/content_bot_ml}"
BRANCH="${BRANCH:-cursor/mlbb-video-pipeline-e712}"

cd "$REPO"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

install -m 755 \
  scripts/pubg_brawl_direct.py \
  scripts/pubg_tiktok_batch_10.py \
  scripts/pubg_owner_calibration.py \
  scripts/gameplay_gate.py \
  /usr/local/bin/

pkill -f pubg_tiktok_batch_10.py 2>/dev/null || true
pkill -f smart_video_editor.py 2>/dev/null || true
sleep 2
rm -f /var/lock/smart_video_editor.lock

nohup /usr/local/bin/run_job_until_ok.sh \
  /root/data/mlbb/pubg_tiktok_batch_10.log \
  python3 /usr/local/bin/pubg_tiktok_batch_10.py --reset --resume \
  >>/root/data/mlbb/pubg_tiktok_batch_10.log 2>&1 &

echo "pubg brawl batch restarted — tail -f /root/data/mlbb/pubg_tiktok_batch_10.log"
