#!/usr/bin/env bash
# Full highlight stack sync to VPS /usr/local/bin
set -Eeuo pipefail
REPO="${REPO:-/root/content_bot_ml}"
BRANCH="${BRANCH:-cursor/mlbb-video-pipeline-e712}"

cd "$REPO"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

echo "GIT=$(git log -1 --oneline)"

pip3 install --break-system-packages -q torch panns-inference open-clip-torch Pillow scikit-learn joblib 2>/dev/null || true

# Disk hygiene
rm -rf /root/.cache/pip 2>/dev/null || true
find /root/videos -name '*.mp4' -mtime +2 -size +80M -delete 2>/dev/null || true

export HF_HOME=/root/data/mlbb/hf_cache
export HIGHLIGHT_EXEMPLAR_ROOT=/root/content_bot_ml/data/highlight_exemplars
export CONTENT_BOT_REPO=/root/content_bot_ml
export PUBG_OWNER_LABELS_PATH=/root/content_bot_ml/data/pubg_owner_labels.json
export HIGHLIGHT_SCORER=1
export HIGHLIGHT_USE_OWNER_ANCHORS=0
export HIGHLIGHT_SOFT_ANCHOR=1
export HIGHLIGHT_QUERY_CONFIG=/root/content_bot_ml/config/highlight_queries.yaml
export OWNER_PREVIEW_REQUIRED=1

install -m 644 config/highlight_queries.yaml /root/content_bot_ml/config/highlight_queries.yaml

install -m 755 \
  scripts/youtube_heatmap_peaks.py \
  scripts/viral_scorer.py \
  scripts/intelliclip_scorer.py \
  scripts/highlight_scorer.py \
  scripts/visual_action_check.py \
  scripts/deploy_highlight_scorer.sh \
  scripts/vps_disk_cleanup.sh \
  scripts/highlight_train.py \
  scripts/eval_owner_labels.py \
  scripts/highlight_probe.py \
  scripts/highlight_bootstrap_exemplars.py \
  scripts/highlight_bootstrap_panns_peaks.py \
  scripts/strict_montage_direct.py \
  scripts/pubg_brawl_direct.py \
  scripts/segment_preview.py \
  scripts/pubg_mlbb_pipeline.py \
  scripts/smart_video_editor.py \
  scripts/pause_legacy_pipelines.sh \
  scripts/deploy_highlight_scorer.sh \
  /usr/local/bin/

# Stop stuck full-VOD scans
pkill -f 'pubg_mlbb_pipeline.py' 2>/dev/null || true
pkill -f 'run_job_until_ok.sh /root/data/mlbb/pubg_mlbb_pipeline' 2>/dev/null || true
bash /usr/local/bin/pause_legacy_pipelines.sh

python3 /usr/local/bin/highlight_bootstrap_exemplars.py --game pubg --vod yt_n97cHIR9Qow.mp4 || true
python3 /usr/local/bin/highlight_bootstrap_panns_peaks.py --game pubg --vod yt_FpMs48XOnq0.mp4 --min-panns 0.35 --top 10 || true
mv /usr/local/data/highlight_exemplars/pubg /root/content_bot_ml/data/highlight_exemplars/ 2>/dev/null || true

grep -c calibrated_pann_gun_min /usr/local/bin/highlight_scorer.py
python3 -c "import panns_inference; print('panns_ok')"

nohup /usr/local/bin/run_job_until_ok.sh \
  /root/data/mlbb/pubg_mlbb_pipeline.log \
  python3 /usr/local/bin/pubg_mlbb_pipeline.py --reset \
  >>/root/data/mlbb/pubg_mlbb_pipeline.log 2>&1 &

echo "SYNC_OK branch=$BRANCH"
