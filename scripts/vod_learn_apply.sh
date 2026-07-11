#!/usr/bin/env bash
# Retrain CPU-cheap per-game VOD quality rankers after owner feedback.
set -Eeuo pipefail
GAME="${1:-all}"
LOCK="/tmp/vod_learn_apply_${GAME}.lock"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "skip vod_learn_apply game=$GAME: another instance"
  exit 0
fi

set -a
source /root/.video_bot.env
set +a
export CONTENT_BOT_REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
export PYTHONPATH="/usr/local/bin:${CONTENT_BOT_REPO}/scripts:${PYTHONPATH:-}"
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1

python3 /usr/local/bin/vod_quality_model.py --game "$GAME" --train --json \
  || python3 "${CONTENT_BOT_REPO}/scripts/vod_quality_model.py" --game "$GAME" --train --json \
  || echo "one or more quality candidates held by grouped gate"

echo "vod_learn_apply done game=$GAME $(date -Is)"
