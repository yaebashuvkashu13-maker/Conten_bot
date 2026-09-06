#!/usr/bin/env bash
# One-time: move mutable owner labels out of git checkout.
set -euo pipefail
REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
RUNTIME="${PUBG_OWNER_LABELS_PATH:-/root/data/pubg/pubg_owner_labels.json}"
SEED="$REPO/data/pubg_owner_labels.json"
mkdir -p "$(dirname "$RUNTIME")"
if [[ ! -f "$RUNTIME" ]]; then
  if [[ -f "$SEED" ]]; then
    cp "$SEED" "$RUNTIME"
    echo "seeded $RUNTIME from $SEED"
  else
    echo '{"videos":{}}' > "$RUNTIME"
    echo "created empty $RUNTIME"
  fi
fi
if git -C "$REPO" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git -C "$REPO" checkout -- data/pubg_owner_labels.json 2>/dev/null || true
fi
echo "runtime labels ready: $RUNTIME"
