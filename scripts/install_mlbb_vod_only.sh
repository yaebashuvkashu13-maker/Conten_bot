#!/usr/bin/env bash
# DEPRECATED: do not install the legacy VOD-only stack.
# Single supported production path is deploy_unified_production.sh
# (systemd-owned feed, no nohup supervisors, no legacy watchdog crons).
#
# Usage (compat):
#   bash scripts/install_mlbb_vod_only.sh           → deploy_unified_production.sh
#   bash scripts/install_mlbb_vod_only.sh --env-only → refuse (use deploy)
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY="$ROOT/scripts/deploy_unified_production.sh"

if [[ "${1:-}" == "--env-only" ]]; then
  echo "REFUSED: install_mlbb_vod_only.sh --env-only is deprecated." >&2
  echo "Use: CONTENT_BOT_REPO=$ROOT bash $DEPLOY" >&2
  echo "(deploy pins safe env + installs the systemd unit)" >&2
  exit 2
fi

if [[ ! -x "$DEPLOY" && ! -f "$DEPLOY" ]]; then
  echo "FATAL: missing $DEPLOY" >&2
  exit 2
fi

echo "DEPRECATED install_mlbb_vod_only.sh → delegating to deploy_unified_production.sh"
export CONTENT_BOT_REPO="${CONTENT_BOT_REPO:-${REPO:-$ROOT}}"
export UNIFIED_BRANCH="${UNIFIED_BRANCH:-${VOD_DEPLOY_BRANCH:-cursor/vod-unified-production-a016}}"
exec bash "$DEPLOY" "$@"
