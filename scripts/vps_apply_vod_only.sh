#!/usr/bin/env bash
# Single VPS apply: git pull + VOD-only install + verify. Run after every code change.
# IMPORTANT: never kill a healthy feed on light (no-git-change) ticks.
set -Eeuo pipefail
REPO="${VPS_REPO_PATH:-/root/content_bot_ml}"
# Prefer hang-detector branch; fall back to legacy if unset elsewhere.
BRANCH="${VPS_BRANCH:-${VOD_DEPLOY_BRANCH:-cursor/vod-hang-autounload-a016}}"
ENV_FILE="${ENV_FILE:-/root/.video_bot.env}"
LOG=/root/data/mlbb/vps_apply_vod.log
mkdir -p /root/data/mlbb
exec >>"$LOG" 2>&1
echo "===== vps_apply_vod_only $(date -Is) branch=${BRANCH} ====="
cd "$REPO" || exit 1

# Persist deploy branch so future crons don't flip back to an old pipeline branch.
if [[ -f "$ENV_FILE" ]] && ! grep -q '^VOD_DEPLOY_BRANCH=' "$ENV_FILE" 2>/dev/null; then
  echo "VOD_DEPLOY_BRANCH=${BRANCH}" >>"$ENV_FILE"
elif [[ -f "$ENV_FILE" ]]; then
  sed -i "s|^VOD_DEPLOY_BRANCH=.*|VOD_DEPLOY_BRANCH=${BRANCH}|" "$ENV_FILE" || true
fi

REV_BEFORE="$(git rev-parse HEAD 2>/dev/null || echo "")"
git fetch origin "$BRANCH" || {
  echo "fetch failed for ${BRANCH} — keep running"
  exit 0
}
# Stash local runtime edits so pull can proceed (owner labels etc.).
git stash push -u -m "vps_apply auto-stash $(date -Is)" -- scripts data 2>/dev/null || true
git checkout "$BRANCH" || true
if ! git pull --ff-only origin "$BRANCH"; then
  echo "pull failed — keep running on $(git rev-parse --short HEAD 2>/dev/null)"
  exit 0
fi
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
  return 0
}

if [[ -n "$REV_BEFORE" && "$REV_BEFORE" == "$REV_AFTER" ]]; then
  echo "no git change ($REV_AFTER) — skip install, skip feed restart"
  # Light health only: hang detector tick (cooldown-safe). Do NOT full reinstall.
  bash /usr/local/bin/mlbb_vod_health_watchdog.sh || true
  exit 0
fi

# Code changed — install scripts but do NOT kill a live feed unless explicitly asked.
export MLBB_VOD_INSTALL_RESTART_FEED="${MLBB_VOD_INSTALL_RESTART_FEED:-0}"
bash "$REPO/scripts/install_mlbb_vod_only.sh"
# Ensure hang detector + health watchdog are on /usr/local/bin
cp -f "$REPO/scripts/vod_hang_detector.py" /usr/local/bin/ 2>/dev/null || true
cp -f "$REPO/scripts/mlbb_vod_health_watchdog.sh" /usr/local/bin/ 2>/dev/null || true
cp -f "$REPO/scripts/vod_feed_recover.py" /usr/local/bin/ 2>/dev/null || true
cp -f "$REPO/scripts/vod_force_send.py" /usr/local/bin/ 2>/dev/null || true
bash /usr/local/bin/mlbb_vod_health_watchdog.sh || true
_verify_or_warn "full"
# If feed service is down after install, start it — don't leave inactive.
if ! systemctl is-active content-bot-vod-feed.service >/dev/null 2>&1; then
  echo "feed inactive after apply — starting"
  systemctl start content-bot-vod-feed.service || true
fi
exit 0
