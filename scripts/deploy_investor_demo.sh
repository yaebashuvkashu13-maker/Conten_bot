#!/usr/bin/env bash
# Investor demo: strict montages, multi-VOD, watchdog cron.
set -Eeuo pipefail

REPO="${REPO:-/root/content_bot_ml}"
BRANCH="${BRANCH:-cursor/mlbb-video-pipeline-e712}"

cd "$REPO"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

install -m 755 \
  scripts/strict_segment_gate.py \
  scripts/strict_montage_direct.py \
  scripts/investor_demo_batch.py \
  scripts/action_showcase_2x5.py \
  scripts/pubg_brawl_direct.py \
  scripts/pubg_shooting_gate.py \
  scripts/montage_env.py \
  scripts/gameplay_gate.py \
  scripts/smart_video_editor.py \
  scripts/overnight_youtube_batch.py \
  scripts/morning_pubg_standoff_catchup.py \
  scripts/pipeline_watchdog.sh \
  scripts/audit_strict_peak_cron.sh \
  /usr/local/bin/

bash "$REPO/scripts/install_pipeline_watchdog_cron.sh"

# Stop stale full-VOD PUBG scan (old logic)
pkill -f pubg_tiktok_batch_10.py 2>/dev/null || true
pkill -f action_showcase_2x5.py 2>/dev/null || true
rm -f /var/lock/smart_video_editor.lock
sleep 2

nohup /usr/local/bin/run_job_until_ok.sh \
  /root/data/mlbb/investor_demo_batch.log \
  python3 /usr/local/bin/investor_demo_batch.py --reset \
  >>/root/data/mlbb/investor_demo_batch.log 2>&1 &

echo "investor demo started — tail -f /root/data/mlbb/investor_demo_batch.log"
