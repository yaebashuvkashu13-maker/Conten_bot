#!/usr/bin/env bash
# Deploy cursor/vod-pipeline-p0-p1-a016 to production VPS.
set -euo pipefail

BRANCH="${VOD_DEPLOY_BRANCH:-cursor/vod-pipeline-p0-p1-a016}"
REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
SERVICE="${VOD_FEED_SERVICE:-content-bot-vod-feed}"

echo "=== deploy branch $BRANCH ==="
cd "$REPO"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git reset --hard "origin/$BRANCH"

bash "$REPO/scripts/migrate_runtime_labels.sh"
bash "$REPO/scripts/vps_deploy_check.sh" || {
  echo "WARN: deploy check failed — inspect dirty tree / labels"
}

bash "$REPO/scripts/install_mlbb_vod_only.sh"

python3 "$REPO/scripts/pubg_regression_benchmark.py" \
  --output /root/data/pubg/benchmark_deploy_$(date +%Y%m%dT%H%M%S).json \
  || echo "WARN: regression benchmark failed (missing VODs?)"

systemctl restart "$SERVICE" 2>/dev/null || systemctl restart shooter-vod-segment-feed 2>/dev/null || true
sleep 3
systemctl is-active "$SERVICE" 2>/dev/null || systemctl is-active shooter-vod-segment-feed 2>/dev/null || true
journalctl -u "$SERVICE" -n 20 --no-pager 2>/dev/null || journalctl -u shooter-vod-segment-feed -n 20 --no-pager 2>/dev/null || true
df -h /root | tail -1
echo "=== deploy done ==="
