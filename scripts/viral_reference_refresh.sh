#!/usr/bin/env bash
# Weekly silver viral reference refresh — does not block montage cron.
set -Eeuo pipefail

REPO="${REPO:-/root/content_bot_ml}"
LOG="${LOG:-/root/data/mlbb/viral_reference_refresh.log}"
LOCK="${LOCK:-/tmp/viral_reference_refresh.lock}"

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date -Is) skip: another refresh running" >>"$LOG"
  exit 0
fi

set -a
# shellcheck source=/dev/null
source /root/.video_bot.env 2>/dev/null || true
set +a

export CONTENT_BOT_REPO="$REPO"
export HIGHLIGHT_EXEMPLAR_ROOT="$REPO/data/highlight_exemplars"
export VIRAL_REFERENCE_ROOT="/root/datasets/viral_reference"
export HIGHLIGHT_CLIP_DISABLED=0
export HIGHLIGHT_USE_OWNER_ANCHORS=0

cd "$REPO"
{
  echo "===== $(date -Is) viral_reference_refresh start ====="
  python3 "$REPO/scripts/viral_learning_batch.py" --per-game 15 --profile all --train || \
  python3 "$REPO/scripts/viral_reference_ingest.py" --profile all --max-download 40
  python3 "$REPO/scripts/highlight_train.py" --profile pubg || true
  python3 "$REPO/scripts/highlight_train.py" --profile mobile_legends || true
  python3 "$REPO/scripts/eval_highlight_model.py" --profile all --report "$REPO/data/training/eval_latest.json" || true
  echo "===== $(date -Is) viral_reference_refresh done ====="
} >>"$LOG" 2>&1
