#!/usr/bin/env bash
set -Eeuo pipefail
REPO="${REPO:-/root/content_bot_ml}"
BRANCH="${BRANCH:-cursor/mlbb-video-pipeline-e712}"

cd "$REPO"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

pip3 install --break-system-packages -q torch panns-inference open-clip-torch Pillow scikit-learn joblib 2>/dev/null || true

# Free disk for PANNs/CLIP weights
rm -rf /root/.cache/pip /root/.cache/huggingface/hub/models--timm--vit_base_patch32_clip_224.openai 2>/dev/null || true
find /root/videos -name '*.mp4' -mtime +3 -size +50M -delete 2>/dev/null || true

mkdir -p /root/data/mlbb /root/data/highlight_exemplars
export HF_HOME=/root/data/mlbb/hf_cache
export HIGHLIGHT_CLIP_PRETRAINED=laion2b_s34b_b79k
install -m 644 "$REPO/data/pubg_owner_labels.json" /root/data/mlbb/pubg_owner_labels.json 2>/dev/null || true
install -m 644 "$REPO/data/pubg_owner_labels.json" "$REPO/data/pubg_owner_labels.json" 2>/dev/null || true

install -m 755 \
  scripts/highlight_scorer.py \
  scripts/highlight_train.py \
  scripts/highlight_probe.py \
  scripts/highlight_bootstrap_exemplars.py \
  scripts/strict_montage_direct.py \
  scripts/segment_preview.py \
  scripts/pubg_mlbb_pipeline.py \
  scripts/pause_legacy_pipelines.sh \
  /usr/local/bin/

bash /usr/local/bin/pause_legacy_pipelines.sh

# Bootstrap exemplars from owner labels
python3 /usr/local/bin/highlight_bootstrap_exemplars.py --game pubg --vod yt_n97cHIR9Qow.mp4 || true
python3 /usr/local/bin/highlight_train.py --profile pubg --vod yt_n97cHIR9Qow.mp4 || true

export HIGHLIGHT_SCORER=1
nohup /usr/local/bin/run_job_until_ok.sh \
  /root/data/mlbb/pubg_mlbb_pipeline.log \
  python3 /usr/local/bin/pubg_mlbb_pipeline.py --reset \
  >>/root/data/mlbb/pubg_mlbb_pipeline.log 2>&1 &

echo "highlight scorer deployed — probe: python3 /usr/local/bin/highlight_probe.py --profile pubg --vod yt_n97cHIR9Qow.mp4"
