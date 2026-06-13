#!/usr/bin/env bash
# Run ON THE VPS: deploy bot + 5-game overnight pipeline (single source of truth).
set -Eeuo pipefail

REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
DEST=/usr/local/bin

cd "$REPO"
git fetch origin cursor/mlbb-video-pipeline-e712 2>/dev/null || git fetch origin
git checkout cursor/mlbb-video-pipeline-e712 2>/dev/null || git pull origin main || true
git pull --ff-only 2>/dev/null || true

apt-get update -qq
apt-get install -y -qq tesseract-ocr curl ffmpeg >/dev/null
python3 -m pip install -q --break-system-packages pytesseract opencv-python-headless numpy PyYAML 2>/dev/null || true

mkdir -p /root/data/mlbb/watermark_examples /root/data/mlbb/overnight_msk /root/data/mlbb/publish

install_scripts() {
  local f
  for f in \
    telegram_upload_bot.py \
    image_watermark_remove.py \
    instagram_digest_gallery_dl.py \
    youtube_download.py \
    youtube_health_check.py \
    youtube_game_prefs.py \
    nightly_youtube_montage.py \
    overnight_youtube_batch.py \
    overnight_msk.sh \
    overnight_catchup.sh \
    stop_competing_workers.sh \
    disable_legacy_publish_crons.sh \
    install_overnight_msk_cron.sh \
    montage_env.py \
    video_frame_io.py \
    gameplay_gate.py \
    smart_video_editor.py \
    mlbb_shorts_montage.py \
    mlbb_shorts_pipeline.py \
    hourly_new_sources_montage.py \
    install_mlbb_shorts_cron.sh \
    source_freshness.py \
    strict_segment_gate.py \
    visual_action_check.py \
    publish_ready_montage.py \
    daily_morning_plan.py \
    daily_evening_report.py \
    daily_ops_cron.sh \
    install_daily_ops_cron.sh \
    overnight_watchdog.sh \
    pipeline_watchdog.sh \
    run_job_until_ok.sh \
    pipeline_retry.py \
    action_showcase_2x5.py \
    action_showcase_queue.sh \
    genshin_boss_rebuild.py \
    morning_pubg_standoff_catchup.py \
    install_pipeline_watchdog_cron.sh; do
    if [[ -f "$REPO/scripts/$f" ]]; then
      install -m 755 "$REPO/scripts/$f" "$DEST/$f"
    fi
  done
  if [[ -f "$REPO/scripts/instagram_digest_run.sh" ]]; then
    install -m 755 "$REPO/scripts/instagram_digest_run.sh" "$DEST/instagram_digest_run.sh"
  fi
}

install_scripts

if ! command -v yt-dlp >/dev/null 2>&1; then
  echo "WARN: yt-dlp not found — pip install -U yt-dlp"
else
  python3 "$DEST/youtube_health_check.py" || echo "WARN: youtube_health_check failed"
fi

# Cron: 18:00 MSK batch; disable legacy auto-publish
if [[ -x "$DEST/install_overnight_msk_cron.sh" ]]; then
  bash "$DEST/install_overnight_msk_cron.sh"
fi
if [[ -x "$DEST/disable_legacy_publish_crons.sh" ]]; then
  bash "$DEST/disable_legacy_publish_crons.sh"
fi
if [[ -x "$DEST/install_daily_ops_cron.sh" ]]; then
  bash "$DEST/install_daily_ops_cron.sh"
fi
if [[ -x "$DEST/install_mlbb_shorts_cron.sh" ]]; then
  bash "$DEST/install_mlbb_shorts_cron.sh"
fi
if [[ -x "$DEST/install_pipeline_watchdog_cron.sh" ]]; then
  bash "$DEST/install_pipeline_watchdog_cron.sh"
fi

# Bot restart only when overnight batch is idle (do not disrupt active montage)
if pgrep -f 'overnight_youtube_batch.py|smart_video_editor.py' >/dev/null 2>&1; then
  echo "SKIP bot restart: overnight montage in progress"
else
  pkill -f telegram_upload_bot.py 2>/dev/null || true
  sleep 2
  pgrep -f telegram_upload_bot.py && pkill -9 -f telegram_upload_bot.py 2>/dev/null || true
  sleep 1
  if systemctl list-units --type=service 2>/dev/null | grep -q telegram-upload-bot; then
    systemctl restart telegram-upload-bot
    echo "restarted telegram-upload-bot"
  else
    nohup python3 "$DEST/telegram_upload_bot.py" >>/root/telegram_upload_bot.log 2>&1 &
    echo "started bot via nohup"
  fi
fi

grep -m1 BOT_VERSION "$DEST/telegram_upload_bot.py" 2>/dev/null || true
pgrep -af 'telegram_upload_bot|overnight_youtube_batch|smart_video_editor' || true
echo "OK deploy: 5-game overnight pipeline + telegram bot"
