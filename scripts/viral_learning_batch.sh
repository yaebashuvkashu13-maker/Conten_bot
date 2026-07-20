#!/usr/bin/env bash
# Viral learning: download Shorts → analyze → compare with current good → improve exemplars.
# Does NOT send Shorts to Telegram. Optional text report with --telegram.
set -Eeuo pipefail

REPO="${REPO:-/root/content_bot_ml}"
LOG="${LOG:-/root/data/mlbb/viral_learning_batch.log}"
LOCK="${LOCK:-/tmp/viral_learning_batch.lock}"
PER_GAME="${PER_GAME:-10}"
# TELEGRAM=1 → text improve report only (never videos)
TELEGRAM="${TELEGRAM:-0}"
APPLY_ENV="${APPLY_ENV:-0}"

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
export VIRAL_INGEST_SKIP_RULE_GATE="${VIRAL_INGEST_SKIP_RULE_GATE:-1}"

EXTRA=()
if [[ "$TELEGRAM" == "1" ]]; then
  EXTRA+=(--telegram)
fi
if [[ "$APPLY_ENV" == "1" ]]; then
  EXTRA+=(--apply-env)
fi

cd "$REPO"
{
  echo "===== $(date -Is) viral_learning_batch start per_game=$PER_GAME (improve, no video spam) ====="
  python3 "$REPO/scripts/viral_learning_batch.py" \
    --per-game "$PER_GAME" \
    --profile all \
    --train \
    "${EXTRA[@]}"
  echo "===== $(date -Is) viral_learning_batch done ====="
} >>"$LOG" 2>&1
