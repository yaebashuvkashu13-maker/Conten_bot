#!/usr/bin/env bash
# Create a restorable snapshot of the VOD bot (code + env + state).
#
#   bash scripts/vod_pipeline_backup.sh              # lite (~50–120 MB)
#   bash scripts/vod_pipeline_backup.sh learning     # + PUBG exemplars (~3 GB)
#   INCLUDE_INBOX=1 bash scripts/vod_pipeline_backup.sh full  # + inbox mp4 (10+ GB)
#
# Backups: /root/backups/vod_pipeline/
# Restore:  bash scripts/vod_pipeline_restore.sh /root/backups/vod_pipeline/vod_pipeline_lite_*.tar.gz
set -Eeuo pipefail

REPO="${REPO:-/root/content_bot_ml}"
BACKUP_ROOT="${BACKUP_ROOT:-/root/backups/vod_pipeline}"
TIER="${1:-lite}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
WORK="$BACKUP_ROOT/_work_${STAMP}"
ARCHIVE="$BACKUP_ROOT/vod_pipeline_${TIER}_${STAMP}.tar.gz"
TAG_NAME="${BACKUP_TAG:-backup/${STAMP}}"

copy_if() {
  local src="$1" dst="$2"
  if [[ -e "$src" ]]; then
    mkdir -p "$(dirname "$dst")"
    cp -a "$src" "$dst"
    echo "  + $src"
  else
    echo "  - skip: $src"
  fi
}

rm -rf "$WORK"
mkdir -p "$WORK"/{repo,state,data_root,repo_data,cache}

echo "===== vod_pipeline_backup tier=$TIER $(date -Is) ====="
cd "$REPO"

GIT_SHA="$(git rev-parse HEAD)"
GIT_BRANCH="$(git branch --show-current 2>/dev/null || echo detached)"
PIPELINE_REV="$(grep -E '^VOD_PIPELINE_REV' scripts/vod_game_registry.py 2>/dev/null | head -1 || echo unknown)"

echo "repo=$REPO branch=$GIT_BRANCH sha=$GIT_SHA"
echo "$PIPELINE_REV"

echo "git bundle…"
git bundle create "$WORK/repo/pipeline.bundle" HEAD

copy_if /root/.video_bot.env "$WORK/video_bot.env"

echo "runtime state (all games)…"
for game in mlbb pubg standoff genshin wot; do
  base="/root/data/$game"
  [[ -d "$base" ]] || continue
  for name in vod_segment_state.json vod_segment_index.json vod_segment_labels.json vod_segment_feed_sent.json; do
    copy_if "$base/$name" "$WORK/state/${game}_$name"
  done
done

for f in daily_game_cycle.json mlbb_vod_segment_feed_sent.json calibration_labels.json adaptive_gate_state.json pubg_owner_labels.json; do
  copy_if "/root/data/mlbb/$f" "$WORK/state/mlbb_$f"
done

echo "owner labels & small repo data…"
copy_if "$REPO/data/pubg_owner_labels.json" "$WORK/repo_data/pubg_owner_labels.json"
copy_if "$REPO/data/mobile_legends_owner_labels.json" "$WORK/repo_data/mobile_legends_owner_labels.json"
copy_if "$REPO/data/mlbb/calibration_labels.json" "$WORK/repo_data/calibration_labels.json"
copy_if "$REPO/data/mlbb/highlight_classifier.joblib" "$WORK/repo_data/highlight_classifier.joblib"
copy_if "$REPO/data/mlbb/highlight_classifier_mobile_legends.joblib" "$WORK/repo_data/highlight_classifier_mobile_legends.joblib"

echo "small caches…"
if [[ -d /root/data/panns_audio_cache ]]; then
  copy_if /root/data/panns_audio_cache "$WORK/cache/panns_audio_cache"
fi
if [[ -d /root/data/vod_peak_feature_cache ]]; then
  copy_if /root/data/vod_peak_feature_cache "$WORK/cache/vod_peak_feature_cache"
fi

if [[ "$TIER" == "learning" || "$TIER" == "full" ]]; then
  echo "PUBG exemplars (learning tier)…"
  copy_if "$REPO/data/highlight_exemplars/pubg" "$WORK/repo_data/highlight_exemplars_pubg"
fi

if [[ "$TIER" == "full" && "${INCLUDE_INBOX:-0}" == "1" ]]; then
  echo "inbox mp4 (INCLUDE_INBOX=1)…"
  for game in mlbb pubg standoff genshin wot; do
    inbox="/root/data/$game/youtube_nightly/inbox"
    if [[ -d "$inbox" ]]; then
      copy_if "$inbox" "$WORK/data_root/${game}_inbox"
    fi
  done
fi

{
  echo "backup_tier=$TIER"
  echo "created_at=$(date -Is)"
  echo "hostname=$(hostname)"
  echo "git_branch=$GIT_BRANCH"
  echo "git_sha=$GIT_SHA"
  echo "$PIPELINE_REV"
  echo "git_tag=$TAG_NAME"
  echo ""
  echo "Approx sizes:"
  echo "  lite     — code bundle + env + JSON state + small caches (~50–150 MB)"
  echo "  learning — lite + PUBG exemplars (~3 GB)"
  echo "  full     — learning; add INCLUDE_INBOX=1 for downloaded VOD mp4 (10+ GB)"
  echo ""
  echo "Restore:"
  echo "  bash $REPO/scripts/vod_pipeline_restore.sh $ARCHIVE"
} >"$WORK/MANIFEST.txt"

mkdir -p "$BACKUP_ROOT"
tar -C "$BACKUP_ROOT" -czf "$ARCHIVE" "$(basename "$WORK")"
rm -rf "$WORK"
case "$TIER" in
  lite) ln -sfn "$(basename "$ARCHIVE")" "$BACKUP_ROOT/latest_lite.tar.gz" ;;
  learning) ln -sfn "$(basename "$ARCHIVE")" "$BACKUP_ROOT/latest_learning.tar.gz" ;;
  full) ln -sfn "$(basename "$ARCHIVE")" "$BACKUP_ROOT/latest_full.tar.gz" ;;
esac

SIZE="$(du -h "$ARCHIVE" | cut -f1)"
BYTES="$(stat -c%s "$ARCHIVE" 2>/dev/null || stat -f%z "$ARCHIVE")"

if [[ "${CREATE_GIT_TAG:-1}" == "1" ]]; then
  if git tag -l "$TAG_NAME" | grep -q .; then
    echo "git tag exists: $TAG_NAME"
  else
    git tag -a "$TAG_NAME" -m "VOD pipeline backup $STAMP ($PIPELINE_REV)" HEAD
    echo "git tag created: $TAG_NAME"
    if git push origin "$TAG_NAME" 2>/dev/null; then
      echo "git tag pushed to origin"
    else
      echo "note: tag not pushed (push manually: git push origin $TAG_NAME)"
    fi
  fi
fi

echo ""
echo "Backup ready: $ARCHIVE"
echo "Size: $SIZE (${BYTES} bytes)"
echo "Git: $GIT_BRANCH @ $GIT_SHA"
echo "$PIPELINE_REV"
echo "Tag: $TAG_NAME"
