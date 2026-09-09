#!/usr/bin/env bash
# Import state bundle produced by export_vod_state_bundle.sh on the NEW EU VPS.
#
#   bash scripts/import_vod_state_bundle.sh /root/mlbb_vod_state_bundle_*.tar.gz
set -Eeuo pipefail

ARCHIVE="${1:-}"
REPO="${REPO:-/root/content_bot_ml}"

if [[ -z "$ARCHIVE" || ! -f "$ARCHIVE" ]]; then
  echo "Usage: $0 /root/mlbb_vod_state_bundle_YYYYMMDDTHHMMSSZ.tar.gz" >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

tar -xzf "$ARCHIVE" -C "$TMP"
BUNDLE="$(find "$TMP" -maxdepth 1 -type d -name 'mlbb_vod_state_export' | head -1)"
[[ -n "$BUNDLE" ]] || BUNDLE="$TMP"

mkdir -p /root/data/mlbb "$REPO/data/highlight_exemplars/mobile_legends" "$REPO/data/mlbb"

restore() {
  local src="$1" dst="$2"
  if [[ -e "$src" ]]; then
    mkdir -p "$(dirname "$dst")"
    cp -a "$src" "$dst"
    echo "  -> $dst"
  fi
}

echo "===== import_vod_state_bundle $(date -Is) ====="
cat "$BUNDLE/MANIFEST.txt" 2>/dev/null || true
echo ""

echo "Runtime state:"
restore "$BUNDLE/data/mlbb/vod_segment_state.json" /root/data/mlbb/vod_segment_state.json
restore "$BUNDLE/data/mlbb/vod_segment_index.json" /root/data/mlbb/vod_segment_index.json
restore "$BUNDLE/data/mlbb/vod_segment_labels.json" /root/data/mlbb/vod_segment_labels.json
restore "$BUNDLE/data/mlbb/calibration_labels.json" /root/data/mlbb/calibration_labels.json
restore "$BUNDLE/data/mlbb/adaptive_gate_state.json" /root/data/mlbb/adaptive_gate_state.json

echo "Repo data:"
restore "$BUNDLE/repo_data/mobile_legends_owner_labels.json" "$REPO/data/mobile_legends_owner_labels.json"
restore "$BUNDLE/repo_data/highlight_classifier.joblib" "$REPO/data/mlbb/highlight_classifier.joblib"
restore "$BUNDLE/repo_data/highlight_classifier_mobile_legends.joblib" "$REPO/data/mlbb/highlight_classifier_mobile_legends.joblib"
if [[ -d "$BUNDLE/repo_data/highlight_exemplars_mobile_legends" ]]; then
  restore "$BUNDLE/repo_data/highlight_exemplars_mobile_legends" "$REPO/data/highlight_exemplars/mobile_legends"
fi

echo ""
echo "Import done. Ensure /root/.video_bot.env exists, then:"
echo "  CONTENT_BOT_REPO=$REPO bash $REPO/scripts/deploy_unified_production.sh"
echo "  systemctl is-active content-bot-vod-feed.service"
