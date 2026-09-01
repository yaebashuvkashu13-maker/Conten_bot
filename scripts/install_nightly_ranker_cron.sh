#!/usr/bin/env bash
# Optional nightly ranker train+benchmark (promote only when PUBG_RANKER_AUTO_PROMOTE=1).
set -euo pipefail
REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
LOG="/root/data/pubg/nightly_ranker.log"
exec >>"$LOG" 2>&1
echo "=== nightly ranker $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
cd "$REPO"
git pull --ff-only origin "${VOD_DEPLOY_BRANCH:-cursor/pubg-unlimited-ru-search-a016}" 2>/dev/null || true
python3 "$REPO/scripts/pubg_nightly_ranker_deploy.py" --train --promote \
  --output /root/data/pubg/nightly_ranker_report.json
