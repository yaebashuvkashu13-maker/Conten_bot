#!/usr/bin/env bash
# Deploy viral highlight engine stack to VPS /usr/local/bin
set -Eeuo pipefail
REPO="${REPO:-/root/content_bot_ml}"
BRANCH="${BRANCH:-cursor/mlbb-video-pipeline-e712}"

cd "$REPO"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

pip3 install --break-system-packages -q torch panns-inference open-clip-torch Pillow scikit-learn joblib pyyaml 2>/dev/null || true

rm -rf /root/.cache/pip 2>/dev/null || true
find /root/videos -name '*.mp4' -mtime +3 -size +50M -delete 2>/dev/null || true

mkdir -p /root/data/mlbb/analysis_cache /root/data/highlight_exemplars
export HF_HOME=/root/data/mlbb/hf_cache
export HIGHLIGHT_EXEMPLAR_ROOT=/root/content_bot_ml/data/highlight_exemplars
export CONTENT_BOT_REPO=/root/content_bot_ml
export PUBG_OWNER_LABELS_PATH=/root/content_bot_ml/data/pubg_owner_labels.json
export HIGHLIGHT_QUERY_CONFIG=/root/content_bot_ml/config/highlight_queries.yaml
export HIGHLIGHT_SCORER=1
export HIGHLIGHT_USE_OWNER_ANCHORS=0
export HIGHLIGHT_SOFT_ANCHOR=1
export OWNER_PREVIEW_REQUIRED=1
export HIGHLIGHT_CLIP_DISABLED=0
export INTELLICLIP_FUSION=0

cp -f "$REPO/config/highlight_queries.yaml" /root/content_bot_ml/config/highlight_queries.yaml
install -m 644 "$REPO/data/pubg_owner_labels.json" /root/data/mlbb/pubg_owner_labels.json 2>/dev/null || true

install -m 755 \
  scripts/youtube_heatmap_peaks.py \
  scripts/viral_scorer.py \
  scripts/intelliclip_scorer.py \
  scripts/highlight_scorer.py \
  scripts/highlight_train.py \
  scripts/eval_owner_labels.py \
  scripts/eval_highlight_model.py \
  scripts/score_owner_windows.py \
  scripts/mlbb_vod_segment_store.py \
  scripts/mlbb_learn_apply.sh \
  scripts/mlbb_baseline_report.py \
  scripts/mlbb_learning_first.py \
  scripts/mlbb_fight_segment.py \
  scripts/mlbb_vod_montage_feed.py \
  scripts/mlbb_daily_report.py \
  scripts/eval_learning_first_gate.py \
  scripts/viral_reference_ingest.py \
  scripts/viral_reference_refresh.sh \
  scripts/highlight_probe.py \
  scripts/highlight_bootstrap_exemplars.py \
  scripts/vps_disk_cleanup.sh \
  scripts/strict_montage_direct.py \
  scripts/segment_preview.py \
  scripts/pubg_mlbb_pipeline.py \
  scripts/visual_action_check.py \
  scripts/preview_gate.py \
  scripts/pubg_combat_gate.py \
  scripts/pause_legacy_pipelines.sh \
  /usr/local/bin/

if [[ -f /root/.video_bot.env ]]; then set -a; source /root/.video_bot.env; set +a; fi

if [[ "${MLBB_ONLY_MODE:-0}" == "1" ]]; then
  bash "$REPO/scripts/install_mlbb_only_mode.sh"
  echo "highlight engine deployed (MLBB-only mode — no multi-game pipelines)"
  exit 0
fi

bash /usr/local/bin/pause_legacy_pipelines.sh
bash /usr/local/bin/vps_disk_cleanup.sh 2>/dev/null || true

python3 /usr/local/bin/highlight_bootstrap_exemplars.py --game pubg --vod yt_n97cHIR9Qow.mp4 || true
for f in pubg_owner_labels.json mobile_legends_owner_labels.json genshin_owner_labels.json standoff_owner_labels.json wot_owner_labels.json; do
  install -m 644 "$REPO/data/$f" "/root/content_bot_ml/data/$f" 2>/dev/null || true
  install -m 644 "$REPO/data/$f" "/root/data/mlbb/$f" 2>/dev/null || true
done
python3 /usr/local/bin/highlight_train.py --profile all || true
python3 /usr/local/bin/eval_owner_labels.py --profile pubg --csv /root/data/mlbb/eval_owner_labels.csv || true

nohup /usr/local/bin/run_job_until_ok.sh \
  /root/data/mlbb/pubg_mlbb_pipeline.log \
  python3 /usr/local/bin/pubg_mlbb_pipeline.py --reset \
  >>/root/data/mlbb/pubg_mlbb_pipeline.log 2>&1 &

echo "highlight engine deployed — probe:"
echo "  HIGHLIGHT_USE_OWNER_ANCHORS=0 python3 /usr/local/bin/highlight_probe.py --profile pubg --vod yt_n97cHIR9Qow.mp4"
