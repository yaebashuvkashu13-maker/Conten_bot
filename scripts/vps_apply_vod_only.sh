#!/usr/bin/env bash
# Single VPS apply: git pull + VOD-only install + verify. Run after every code change.
set -Eeuo pipefail
REPO="${VPS_REPO_PATH:-/root/content_bot_ml}"
BRANCH="${VPS_BRANCH:-cursor/pubg-unlimited-ru-search-a016}"
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
if [[ -n "$REV_BEFORE" && "$REV_BEFORE" == "$REV_AFTER" ]]; then
  echo "no git change ($REV_AFTER) — skip install, keep VOD feed scanning"
  if [[ -f /root/data/mlbb/EU_PUBG_ONLY ]] || grep -q '^VOD_PUBG_ONLY=1' "${ENV_FILE:-/root/.video_bot.env}" 2>/dev/null; then
    echo "sync PUBG-only env keys"
    bash "$REPO/scripts/install_mlbb_vod_only.sh" --env-only 2>/dev/null || true
  fi
  bash /usr/local/bin/mlbb_vod_health_watchdog.sh || true
  if ! bash /usr/local/bin/mlbb_vod_only_verify.sh; then
    echo "APPLY FAILED verify (light)"
    exit 1
  fi
  echo "APPLY OK light $(date -Is)"
  exit 0
fi
export MLBB_VOD_INSTALL_RESTART_FEED=1
bash "$REPO/scripts/install_mlbb_vod_only.sh"
bash /usr/local/bin/mlbb_vod_health_watchdog.sh || true
if ! bash /usr/local/bin/mlbb_vod_only_verify.sh; then
  echo "APPLY FAILED verify"
  exit 1
fi
echo "APPLY OK $(date -Is)"
