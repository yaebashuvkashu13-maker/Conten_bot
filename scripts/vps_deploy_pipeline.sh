#!/usr/bin/env bash
# Deploy cursor/vod-unified-production-a016 to production VPS (sole supported path).
set -euo pipefail

BRANCH="${UNIFIED_BRANCH:-${VOD_DEPLOY_BRANCH:-cursor/vod-unified-production-a016}}"
REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
UNIT="${VOD_FEED_SYSTEMD_UNIT:-content-bot-vod-feed.service}"

echo "=== deploy branch $BRANCH (unified) ==="
cd "$REPO"
git fetch origin "$BRANCH"
git checkout -B "$BRANCH" "origin/$BRANCH"
git reset --hard "origin/$BRANCH"

export CONTENT_BOT_REPO="$REPO"
export UNIFIED_BRANCH="$BRANCH"
bash "$REPO/scripts/deploy_unified_production.sh"

python3 "$REPO/scripts/pubg_regression_benchmark.py" \
  --output "/root/data/pubg/benchmark_deploy_$(date +%Y%m%dT%H%M%S).json" \
  || echo "WARN: regression benchmark failed (missing VODs?)"

systemctl is-active "$UNIT" 2>/dev/null || true
journalctl -u "$UNIT" -n 20 --no-pager 2>/dev/null || true
df -h /root | tail -1
echo "=== deploy done ==="
