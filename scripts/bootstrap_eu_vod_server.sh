#!/usr/bin/env bash
# Fresh EU VPS bootstrap for MLBB VOD-only pipeline.
# Prereqs: Ubuntu/Debian, root, outbound YouTube + Telegram OK (not RU datacenter).
#
# One-time on NEW server:
#   apt-get update && apt-get install -y git
#   git clone https://github.com/yaebashuvkashu13-maker/Conten_bot.git /root/content_bot_ml
#   cd /root/content_bot_ml && git checkout cursor/mlbb-video-pipeline-e712
#   cp config/video_bot.env.example /root/.video_bot.env && nano /root/.video_bot.env
#   bash scripts/bootstrap_eu_vod_server.sh
#
# Optional — restore state from old server first:
#   bash scripts/import_vod_state_bundle.sh /root/mlbb_vod_state_bundle_*.tar.gz
#   bash scripts/bootstrap_eu_vod_server.sh
set -Eeuo pipefail

REPO="${REPO:-/root/content_bot_ml}"
BRANCH="${VPS_BRANCH:-cursor/mlbb-video-pipeline-e712}"
ENV_FILE="${ENV_FILE:-/root/.video_bot.env}"

echo "===== bootstrap_eu_vod_server $(date -Is) ====="

if [[ $EUID -ne 0 ]]; then
  echo "Run as root" >&2
  exit 1
fi

if [[ ! -d "$REPO/.git" ]]; then
  echo "Repo missing at $REPO — clone first:" >&2
  echo "  git clone https://github.com/yaebashuvkashu13-maker/Conten_bot.git $REPO" >&2
  exit 1
fi

cd "$REPO"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH" || true

echo "--- apt packages ---"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  ffmpeg tesseract-ocr curl ca-certificates git tmux \
  python3 python3-pip python3-venv \
  >/dev/null

if ! command -v yt-dlp >/dev/null 2>&1; then
  python3 -m pip install -q --break-system-packages -U yt-dlp 2>/dev/null \
    || pip3 install -q -U yt-dlp
fi

echo "--- python packages (VOD scorer) ---"
python3 -m pip install -q --break-system-packages \
  numpy opencv-python-headless pytesseract PyYAML joblib scikit-learn \
  2>/dev/null || pip3 install -q numpy opencv-python-headless pytesseract PyYAML joblib scikit-learn

# CLIP + PANNs are heavy; install if RAM allows (16GB+ recommended)
if [[ "${BOOTSTRAP_INSTALL_ML:-1}" == "1" ]]; then
  python3 -m pip install -q --break-system-packages \
    torch torchvision --index-url https://download.pytorch.org/whl/cpu \
    2>/dev/null || echo "WARN: torch install failed — scorer may be slower or skip CLIP"
  python3 -m pip install -q --break-system-packages \
    git+https://github.com/openai/CLIP.git panns-inference \
    2>/dev/null || echo "WARN: CLIP/panns install failed — run pip manually"
fi

mkdir -p /root/data/mlbb/youtube_nightly/inbox /root/data/mlbb/logs \
  /root/datasets/mlbb/vod_segments \
  "$REPO/data/highlight_exemplars/mobile_legends"/{good,bad}

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$REPO/config/video_bot.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo ""
  echo "Created $ENV_FILE — EDIT TG_BOT_TOKEN and TG_CHAT_ID, then re-run:"
  echo "  nano $ENV_FILE"
  echo "  bash $REPO/scripts/bootstrap_eu_vod_server.sh"
  exit 0
fi

if ! grep -qE '^TG_BOT_TOKEN=.+$' "$ENV_FILE" || ! grep -qE '^TG_CHAT_ID=.+$' "$ENV_FILE"; then
  echo "FAIL: set TG_BOT_TOKEN and TG_CHAT_ID in $ENV_FILE" >&2
  exit 1
fi

echo "--- CPU thread tuning (8 vCPU / 32 GiB EU tier) ---"
NPROC="$(nproc 2>/dev/null || echo 4)"
if [[ "$NPROC" -ge 8 ]]; then
  ML_THREADS=4
elif [[ "$NPROC" -ge 4 ]]; then
  ML_THREADS=2
else
  ML_THREADS=1
fi
for kv in OMP_NUM_THREADS="${ML_THREADS}" OPENBLAS_NUM_THREADS="${ML_THREADS}" MKL_NUM_THREADS="${ML_THREADS}"; do
  key="${kv%%=*}"
  val="${kv#*=}"
  if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
  else
    echo "${key}=${val}" >>"$ENV_FILE"
  fi
done
echo "nproc=${NPROC} → ML threads=${ML_THREADS}"

echo "--- install MLBB VOD-only ---"
export MLBB_VOD_INSTALL_RESTART_FEED=1
CONTENT_BOT_REPO="$REPO" UNIFIED_BRANCH="${UNIFIED_BRANCH:-cursor/vod-unified-production-a016}" bash "$REPO/scripts/deploy_unified_production.sh"

echo "--- smoke tests ---"
if command -v yt-dlp >/dev/null; then
  timeout 60 yt-dlp --flat-playlist --playlist-end 1 \
    "https://www.youtube.com/results?search_query=MLBB+mythic+ranked&sp=EgQIBBAB" \
    --print "%(id)s" >/dev/null && echo "YouTube search: OK" || echo "WARN: YouTube search failed"
fi

bash /usr/local/bin/mlbb_vod_only_verify.sh || {
  echo "VERIFY failed — check logs:"
  echo "  tail -50 /root/data/mlbb/mlbb_vod_segment_feed.log"
  exit 1
}

echo ""
echo "===== EU bootstrap OK $(date -Is) ====="
echo "Feed log: tail -f /root/data/mlbb/mlbb_vod_segment_feed.log"
echo "Supervisor: pgrep -af mlbb_vod_segment_feed"
echo "Disk: df -h /"
