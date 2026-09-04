#!/usr/bin/env bash
# Deploy cursor/vod-pipeline-p0-p1-a016 to production VPS.
set -euo pipefail

BRANCH="${VOD_DEPLOY_BRANCH:-cursor/pubg-vod-quality-pipeline-a016}"
REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
SERVICE="${VOD_FEED_SERVICE:-content-bot-vod-feed}"
LOCK_FILE="${VOD_DEPLOY_LOCK_FILE:-/root/data/pubg/.deploy.lock}"

echo "=== deploy branch $BRANCH ==="
cd "$REPO"

# Immutable-ish deploy: lock for the duration, abort on dirty/diverged checkout.
mkdir -p "$(dirname "$LOCK_FILE")"
if [[ -f "$LOCK_FILE" ]]; then
  echo "FAIL: deploy lock present at $LOCK_FILE" >&2
  exit 1
fi
echo "pid=$$ branch=$BRANCH started=$(date -Is)" >"$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT
export VOD_DEPLOY_HOLDING_LOCK=1

# Move runtime labels out of git BEFORE dirty check.
bash "$REPO/scripts/migrate_runtime_labels.sh"

git fetch origin "$BRANCH"
# Prefer detached immutable commit when VOD_DEPLOY_COMMIT is set.
EXPECTED="${VOD_DEPLOY_COMMIT:-}"
if [[ -n "$EXPECTED" ]]; then
  git checkout --detach "$EXPECTED"
else
  git checkout "$BRANCH"
  git reset --hard "origin/$BRANCH"
  EXPECTED="$(git rev-parse HEAD)"
fi
export VOD_DEPLOY_COMMIT="$EXPECTED"
export VOD_DEPLOY_BRANCH="$BRANCH"

bash "$REPO/scripts/vps_deploy_check.sh"

bash "$REPO/scripts/install_mlbb_vod_only.sh"

python3 "$REPO/scripts/pubg_regression_benchmark.py" \
  --output /root/data/pubg/benchmark_deploy_$(date +%Y%m%dT%H%M%S).json \
  || echo "WARN: regression benchmark failed (missing VODs?)"

systemctl restart "$SERVICE" 2>/dev/null || systemctl restart shooter-vod-feed 2>/dev/null || true
sleep 3
systemctl is-active "$SERVICE" 2>/dev/null || systemctl is-active shooter-vod-feed 2>/dev/null || true
journalctl -u "$SERVICE" -n 20 --no-pager 2>/dev/null || journalctl -u shooter-vod-feed -n 20 --no-pager 2>/dev/null || true
df -h /root | tail -1
echo "=== deploy done commit=$EXPECTED ==="
