#!/usr/bin/env bash
set -Eeuo pipefail
REPO="${REPO:-/root/content_bot_ml}"
BRANCH="${BRANCH:-cursor/mlbb-video-pipeline-e712}"

cd "$REPO"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

pip3 install -q torch panns-inference open-clip-torch Pillow 2>/dev/null || true

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
