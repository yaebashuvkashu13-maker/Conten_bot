#!/usr/bin/env bash
# Export MLBB VOD state from an existing VPS for EU migration.
# Run on OLD server as root. Videos in inbox are NOT included (re-download on EU).
#
#   bash scripts/export_vod_state_bundle.sh
#   scp /root/mlbb_vod_state_bundle_*.tar.gz root@NEW_EU_IP:/root/
#   scp /root/.video_bot.env root@NEW_EU_IP:/root/.video_bot.env
set -Eeuo pipefail

REPO="${REPO:-/root/content_bot_ml}"
OUT_DIR="${OUT_DIR:-/root/mlbb_vod_state_export}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE="/root/mlbb_vod_state_bundle_${STAMP}.tar.gz"

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"/{data/mlbb,repo_data}

copy_if() {
  local src="$1" dst="$2"
  if [[ -e "$src" ]]; then
    mkdir -p "$(dirname "$dst")"
    cp -a "$src" "$dst"
    echo "  + $src"
  else
    echo "  - skip (missing): $src"
  fi
}

echo "===== export_vod_state_bundle $(date -Is) ====="
echo "repo=$REPO"

# Runtime state (small JSON — worth migrating)
copy_if /root/data/mlbb/vod_segment_state.json "$OUT_DIR/data/mlbb/vod_segment_state.json"
copy_if /root/data/mlbb/vod_segment_index.json "$OUT_DIR/data/mlbb/vod_segment_index.json"
copy_if /root/data/mlbb/vod_segment_labels.json "$OUT_DIR/data/mlbb/vod_segment_labels.json"
copy_if /root/data/mlbb/calibration_labels.json "$OUT_DIR/data/mlbb/calibration_labels.json"
copy_if /root/data/mlbb/adaptive_gate_state.json "$OUT_DIR/data/mlbb/adaptive_gate_state.json"

# Learning / scoring (optional but saves re-tuning time)
copy_if "$REPO/data/mobile_legends_owner_labels.json" "$OUT_DIR/repo_data/mobile_legends_owner_labels.json"
copy_if "$REPO/data/mlbb/calibration_labels.json" "$OUT_DIR/repo_data/calibration_labels_repo_copy.json"
copy_if "$REPO/data/mlbb/highlight_classifier.joblib" "$OUT_DIR/repo_data/highlight_classifier.joblib"
copy_if "$REPO/data/mlbb/highlight_classifier_mobile_legends.joblib" "$OUT_DIR/repo_data/highlight_classifier_mobile_legends.joblib"

# Exemplars (CLIP owner scoring — can be large; use INCLUDE_EXEMPLARS=0 to skip)
INCLUDE_EXEMPLARS="${INCLUDE_EXEMPLARS:-1}"
if [[ "$INCLUDE_EXEMPLARS" == "1" ]]; then
  copy_if "$REPO/data/highlight_exemplars/mobile_legends" "$OUT_DIR/repo_data/highlight_exemplars_mobile_legends"
else
  echo "  - skip exemplars (INCLUDE_EXEMPLARS=0)"
fi

{
  echo "exported_at=$(date -Is)"
  echo "hostname=$(hostname)"
  echo "git_rev=$(cd "$REPO" && git rev-parse HEAD 2>/dev/null || echo unknown)"
  echo "git_branch=$(cd "$REPO" && git branch --show-current 2>/dev/null || echo unknown)"
  echo ""
  echo "NOT included (copy separately):"
  echo "  /root/.video_bot.env  (secrets — scp by hand, chmod 600)"
  echo "  /root/data/mlbb/youtube_nightly/inbox/*.mp4  (re-download on EU)"
  echo ""
  echo "Restore on EU:"
  echo "  bash scripts/import_vod_state_bundle.sh /root/mlbb_vod_state_bundle_*.tar.gz"
} >"$OUT_DIR/MANIFEST.txt"

tar -C "$(dirname "$OUT_DIR")" -czf "$ARCHIVE" "$(basename "$OUT_DIR")"
rm -rf "$OUT_DIR"

SIZE="$(du -h "$ARCHIVE" | cut -f1)"
echo ""
echo "Bundle: $ARCHIVE ($SIZE)"
echo "Next:"
echo "  scp $ARCHIVE root@NEW_EU_IP:/root/"
echo "  scp /root/.video_bot.env root@NEW_EU_IP:/root/.video_bot.env"
