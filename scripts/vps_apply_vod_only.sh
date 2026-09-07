#!/usr/bin/env bash
# VPS apply: pull unified branch + deploy_unified_production.sh only.
# Never reinstalls legacy nohup supervisors or watchdog crons.
set -Eeuo pipefail
REPO="${CONTENT_BOT_REPO:-${VPS_REPO_PATH:-/root/content_bot_ml}}"
BRANCH="${UNIFIED_BRANCH:-${VPS_BRANCH:-${VOD_DEPLOY_BRANCH:-cursor/vod-unified-production-a016}}}"
ENV_FILE="${VOD_BOT_ENV_FILE:-${ENV_FILE:-/root/.video_bot.env}}"
LOG="${VPS_APPLY_LOG:-/root/data/mlbb/vps_apply_vod.log}"
mkdir -p "$(dirname "$LOG")" /root/data/mlbb
exec >>"$LOG" 2>&1
echo "===== vps_apply_vod_only $(date -Is) branch=${BRANCH} (unified-only) ====="
cd "$REPO" || exit 1

# Persist deploy branch so crons cannot flip back to a hostile tip.
if [[ -f "$ENV_FILE" ]]; then
  if grep -q '^VOD_DEPLOY_BRANCH=' "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^VOD_DEPLOY_BRANCH=.*|VOD_DEPLOY_BRANCH=${BRANCH}|" "$ENV_FILE" || true
  else
    echo "VOD_DEPLOY_BRANCH=${BRANCH}" >>"$ENV_FILE"
  fi
  if grep -q '^UNIFIED_BRANCH=' "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^UNIFIED_BRANCH=.*|UNIFIED_BRANCH=${BRANCH}|" "$ENV_FILE" || true
  else
    echo "UNIFIED_BRANCH=${BRANCH}" >>"$ENV_FILE"
  fi
fi

REV_BEFORE="$(git rev-parse HEAD 2>/dev/null || echo "")"
git fetch origin "$BRANCH" || {
  echo "fetch failed for ${BRANCH} — keep running"
  exit 0
}
git stash push -u -m "vps_apply auto-stash $(date -Is)" -- scripts data 2>/dev/null || true
git checkout -B "$BRANCH" "origin/$BRANCH" || git checkout "$BRANCH" || true
if ! git pull --ff-only origin "$BRANCH"; then
  echo "pull failed — keep running on $(git rev-parse --short HEAD 2>/dev/null)"
  exit 0
fi
REV_AFTER="$(git rev-parse HEAD 2>/dev/null || echo "")"

if [[ -n "$REV_BEFORE" && "$REV_BEFORE" == "$REV_AFTER" ]]; then
  echo "no git change ($REV_AFTER) — skip redeploy; health check only"
  python3 /usr/local/bin/vod_feed_owner_health.py --game pubg || true
  exit 0
fi

export CONTENT_BOT_REPO="$REPO"
export UNIFIED_BRANCH="$BRANCH"
export VOD_BOT_ENV_FILE="$ENV_FILE"
bash "$REPO/scripts/deploy_unified_production.sh"
echo "APPLY OK $(date -Is) rev=${REV_AFTER}"
exit 0
