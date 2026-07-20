#!/usr/bin/env bash
# Viral learning batch: 10 high-view Shorts per game + feature analysis + report.
set -Eeuo pipefail

REPO="${REPO:-/root/content_bot_ml}"
LOG="${LOG:-/root/data/mlbb/viral_learning_batch.log}"
LOCK="${LOCK:-/tmp/viral_learning_batch.lock}"
PER_GAME="${PER_GAME:-10}"

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date -Is) skip: another viral_learning_batch running" >>"$LOG"
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
export VIRAL_INGEST_FAST="${VIRAL_INGEST_FAST:-1}"
export VIRAL_INGEST_MIN_GAMEPLAY="${VIRAL_INGEST_MIN_GAMEPLAY:-0.52}"

cd "$REPO"
{
  echo "===== $(date -Is) viral_learning_batch start per_game=$PER_GAME ====="
  python3 "$REPO/scripts/viral_learning_batch.py" \
    --per-game "$PER_GAME" \
    --profile all \
    --telegram
  echo "===== $(date -Is) viral_learning_batch done ====="
} >>"$LOG" 2>&1
