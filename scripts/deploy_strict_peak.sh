#!/usr/bin/env bash
# Deploy strict peak montage factory (5 games) on VPS.
set -Eeuo pipefail

REPO="${REPO:-/root/content_bot_ml}"
BRANCH="${BRANCH:-cursor/vod-unified-production-a016}"

cd "$REPO"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

install -m 755 \
  scripts/strict_segment_gate.py \
  scripts/strict_montage_direct.py \
  scripts/pubg_shooting_gate.py \
  scripts/pubg_brawl_direct.py \
  scripts/pubg_tiktok_batch_10.py \
  scripts/action_showcase_2x5.py \
  scripts/montage_env.py \
  scripts/gameplay_gate.py \
  scripts/smart_video_editor.py \
  /usr/local/bin/

pkill -f action_showcase_2x5.py 2>/dev/null || true
sleep 2

nohup /usr/local/bin/run_job_until_ok.sh \
  /root/data/mlbb/action_showcase_2x5.log \
  python3 /usr/local/bin/action_showcase_2x5.py --reset --resume \
  >>/root/data/mlbb/action_showcase_2x5.log 2>&1 &

echo "strict showcase restarted — tail -f /root/data/mlbb/action_showcase_2x5.log"
