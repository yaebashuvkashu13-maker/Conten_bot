#!/usr/bin/env bash
# Single VPS apply: git pull + VOD-only install + verify. Run after every code change.
set -Eeuo pipefail
REPO="${VPS_REPO_PATH:-/root/content_bot_ml}"
BRANCH="${VPS_BRANCH:-${VOD_DEPLOY_BRANCH:-cursor/vod-pipeline-p0-p1-a016}}"
ENV_FILE="${ENV_FILE:-/root/.video_bot.env}"
LOG=/root/data/mlbb/vps_apply_vod.log
mkdir -p /root/data/mlbb
exec >>"$LOG" 2>&1
echo "===== vps_apply_vod_only $(date -Is) ====="
cd "$REPO" || exit 1
REV_BEFORE="$(git rev-parse HEAD 2>/dev/null || echo "")"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"
REV_AFTER="$(git rev-parse HEAD 2>/dev/null || echo "")"

_sync_pubg_env() {
  if [[ -f /root/data/mlbb/EU_PUBG_ONLY ]] || grep -q '^VOD_PUBG_ONLY=1' "$ENV_FILE" 2>/dev/null; then
    echo "sync PUBG-only env keys"
    bash "$REPO/scripts/install_mlbb_vod_only.sh" --env-only || true
  fi
}

_verify_or_warn() {
  local label="$1"
  _sync_pubg_env
  if bash /usr/local/bin/mlbb_vod_only_verify.sh; then
    echo "APPLY OK ${label} $(date -Is) rev=${REV_AFTER}"
    return 0
  fi
  echo "VERIFY WARN ${label} — env synced, feed kept running (see verify output above)"
  bash /usr/local/bin/mlbb_vod_health_watchdog.sh || true
  return 0
}

if [[ -n "$REV_BEFORE" && "$REV_BEFORE" == "$REV_AFTER" ]]; then
  echo "no git change ($REV_AFTER) — skip full install"
  bash /usr/local/bin/mlbb_vod_health_watchdog.sh || true
  _verify_or_warn "light"
  exit 0
fi

export MLBB_VOD_INSTALL_RESTART_FEED=1
bash "$REPO/scripts/install_mlbb_vod_only.sh"
bash /usr/local/bin/mlbb_vod_health_watchdog.sh || true
_verify_or_warn "full"
exit 0
