#!/usr/bin/env bash
# Restore VOD bot from vod_pipeline_backup.sh archive.
#
#   bash scripts/vod_pipeline_restore.sh /root/backups/vod_pipeline/vod_pipeline_lite_*.tar.gz
#
# Does NOT delete inbox VODs or exemplars missing from the archive.
set -Eeuo pipefail

ARCHIVE="${1:-}"
REPO="${REPO:-/root/content_bot_ml}"

if [[ -z "$ARCHIVE" || ! -f "$ARCHIVE" ]]; then
  echo "Usage: $0 /root/backups/vod_pipeline/vod_pipeline_lite_YYYYMMDDTHHMMSSZ.tar.gz" >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

tar -xzf "$ARCHIVE" -C "$TMP"
WORK="$(find "$TMP" -maxdepth 1 -type d -name '_work_*' | head -1)"
[[ -n "$WORK" ]] || { echo "invalid archive layout"; exit 1; }

echo "===== vod_pipeline_restore $(date -Is) ====="
cat "$WORK/MANIFEST.txt" 2>/dev/null || true
echo ""

restore_file() {
  local src="$1" dst="$2"
  if [[ -e "$src" ]]; then
    mkdir -p "$(dirname "$dst")"
    cp -a "$src" "$dst"
    echo "  -> $dst"
  fi
}

if [[ -f "$WORK/video_bot.env" ]]; then
  cp -a "$WORK/video_bot.env" /root/.video_bot.env
  chmod 600 /root/.video_bot.env
  echo "restored /root/.video_bot.env"
fi

for f in "$WORK"/state/mlbb_*; do
  [[ -f "$f" ]] || continue
  base="$(basename "$f")"
  restore_file "$f" "/root/data/mlbb/${base#mlbb_}"
done

for game in mlbb pubg standoff genshin wot; do
  for f in "$WORK"/state/${game}_*; do
    [[ -f "$f" ]] || continue
    base="$(basename "$f")"
    restore_file "$f" "/root/data/$game/${base#${game}_}"
  done
done

restore_file "$WORK/repo_data/pubg_owner_labels.json" "$REPO/data/pubg_owner_labels.json"
restore_file "$WORK/repo_data/mobile_legends_owner_labels.json" "$REPO/data/mobile_legends_owner_labels.json"
restore_file "$WORK/repo_data/calibration_labels.json" "$REPO/data/mlbb/calibration_labels.json"
restore_file "$WORK/repo_data/highlight_classifier.joblib" "$REPO/data/mlbb/highlight_classifier.joblib"
restore_file "$WORK/repo_data/highlight_classifier_mobile_legends.joblib" "$REPO/data/mlbb/highlight_classifier_mobile_legends.joblib"

if [[ -d "$WORK/repo_data/highlight_exemplars_pubg" ]]; then
  restore_file "$WORK/repo_data/highlight_exemplars_pubg" "$REPO/data/highlight_exemplars/pubg"
fi

if [[ -d "$WORK/cache/panns_audio_cache" ]]; then
  restore_file "$WORK/cache/panns_audio_cache" /root/data/panns_audio_cache
fi
if [[ -d "$WORK/cache/vod_peak_feature_cache" ]]; then
  restore_file "$WORK/cache/vod_peak_feature_cache" /root/data/vod_peak_feature_cache
fi

BUNDLE="$WORK/repo/pipeline.bundle"
if [[ -f "$BUNDLE" ]]; then
  echo ""
  echo "Code bundle present. To checkout exact code:"
  echo "  cd $REPO && git fetch . $BUNDLE && git checkout \$(git bundle list-heads $BUNDLE | awk '{print \$1}')"
  echo "Or clone fresh:"
  echo "  git clone $BUNDLE /root/content_bot_ml_restored"
fi

echo ""
echo "Restore done. Restart feed + telegram:"
echo "  pkill -f shooter_vod_segment_feed; pkill -f telegram_upload_bot"
echo "  bash $REPO/scripts/run_telegram_bot.sh &"
echo "  # supervisor restarts feed on next cron tick, or:"
echo "  bash /usr/local/bin/mlbb_vod_segment_feed.sh"
