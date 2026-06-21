#!/usr/bin/env bash
# MLBB VOD-only: stable ~20-min match cutting, no Shorts worker chaos.
# Run on VPS after git pull: bash scripts/install_mlbb_vod_only.sh
set -Eeuo pipefail

REPO="${REPO:-/root/content_bot_ml}"
ENV_FILE="${ENV_FILE:-/root/.video_bot.env}"
PAUSE=/root/data/mlbb/PAUSED_PIPELINES
MARK="# mlbb-vod-only-mode"
BIN=/usr/local/bin

mkdir -p /root/data/mlbb /root/datasets/mlbb/vod_segments /root/data/mlbb/logs \
  /root/data/mlbb/youtube_nightly/inbox

# Pause every other pipeline.
cat >"$PAUSE" <<'EOF'
pubg_mlbb_pipeline.py
overnight_youtube_batch.py
viral_reference_ingest.py
eval_owner_labels.py
score_owner_windows.py
investor_demo_batch.py
action_showcase_2x5.py
morning_pubg_standoff_catchup.py
pubg_gunfire_rebuild.py
pubg_tiktok_batch_10.py
genshin_boss_rebuild.py
mlbb_showcase_rebuild.py
standoff_exemplar_ingest.py
nightly_youtube_montage.py
youtube_triple_montage.py
smart_video_editor.py
mlbb_continuous_worker.py
mlbb_calibration_feed.py
mlbb_youtube_shorts_ingest.py
mlbb_hero_shorts_montage.py
EOF

for pat in mlbb_continuous_worker mlbb_calibration_feed mlbb_youtube_shorts_ingest \
  mlbb_hero_shorts_montage pubg_mlbb_pipeline overnight_youtube_batch overnight_catchup \
  overnight_msk overnight_watchdog viral_reference_ingest eval_owner_labels score_owner_windows \
  investor_demo_batch action_showcase_2x5 morning_pubg_standoff \
  pubg_gunfire_rebuild pubg_tiktok_batch genshin_boss_rebuild \
  mlbb_showcase_rebuild standoff overnight_orchestrator \
  pubg_brawl_direct pubg_stream_learn youtube_triple_montage \
  nightly_youtube_montage hourly_new_sources pipeline_watchdog smart_video_editor; do
  pkill -f "$pat" 2>/dev/null || true
done
pkill -f 'run_job_until_ok.sh /root/data/mlbb/pubg_mlbb' 2>/dev/null || true
pkill -f 'run_job_until_ok.sh /root/data/mlbb/investor_demo' 2>/dev/null || true
pkill -f 'run_job_until_ok.sh /root/data/mlbb/action_showcase' 2>/dev/null || true
sleep 2
pkill -9 -f 'mlbb_vod_segment_feed' 2>/dev/null || true
rm -f /tmp/mlbb_vod_oneoff.lock /tmp/mlbb_vod_segment_feed.lock 2>/dev/null || true
rm -f /var/lock/smart_video_editor.lock /var/lock/overnight_msk.lock 2>/dev/null || true

touch "$ENV_FILE"
for kv in MLBB_ONLY_MODE=1 VK_MLBB_DISABLED=1 VK_MLBB_NOTIFY_EMPTY=0 \
  MLBB_VOD_DISABLED=0 MLBB_VOD_ONLY=1 MLBB_LEARNING_FIRST=0 MLBB_SEND_ENABLED=1 \
  MLBB_MAX_DAILY_SENDS=120 MLBB_CALIBRATION_FEED_ENABLED=0 MLBB_ONE_HEAVY_JOB=1 \
  MLBB_CALIBRATION_LENIENT=0 MLBB_CALIBRATION_FAST_INGEST=0 MLBB_SILVER_BOOTSTRAP=0 \
  MLBB_FEED_RESCORE=0 MLBB_OWNER_EMERGENCY=0 MLBB_OWNER_REQUIRE_SCORE=0 \
  MLBB_INGEST_ALWAYS=0 MLBB_AUTO_TRAIN=0 MLBB_FEED_RE_GATE=0 \
  MLBB_USE_CLASSIFIER=0 MLBB_HERO_SHORTS_MONTAGE=0 MLBB_MONTAGE_COOLDOWN_SEC=86400 \
  MLBB_NUDGE_LOAD_MAX=0 MLBB_FEED_STALE_SEC=7200 MLBB_WORKER_STALE_SEC=7200 \
  MLBB_VOD_BATCH_MAX=6 MLBB_VOD_PIPELINE_MAX_VODS=1 MLBB_VOD_PIPELINE_MAX_MIN=120 \
  MLBB_VOD_PROBE_LIMIT=20 MLBB_VOD_SEGMENT_SEC=15 HIGHLIGHT_WINDOW_SEC=15 \
  MLBB_VOD_MIN_SEC=900 MLBB_VOD_MAX_SEC=2700 MLBB_VOD_TARGET_DUR_SEC=1500 \
  MLBB_VOD_SKIP_LONG_SEC=2700 MLBB_VOD_MIN_PEAK_SEC=420 \
  MLBB_VOD_SEARCH_LIMIT=15 MLBB_VOD_SEARCH_DELAY=6 MLBB_VOD_DOWNLOAD_DELAY=10 \
  MLBB_VOD_BG_WAIT_SEC=180 MLBB_VOD_AUTO_DOWNLOAD=1 MLBB_VOD_FULL_SCAN=1 \
  MLBB_VOD_BOOTSTRAP=0 MLBB_VOD_CHUNK_RENDER=1 MLBB_VOD_SIMPLE_AUDIO=1 \
  MLBB_VOD_GOP_SEC=1 MLBB_VOD_LEAD_SEC=4 MLBB_VOD_VARIABLE_LENGTH=1 \
  MLBB_FORCE_RERENDER=1 MLBB_PRESEND_FREEZE_MIN_DUR=1.2 MLBB_PRESEND_FREEZE_MAX_START=3.0 \
  MLBB_PRESEND_MIN_MOTION=0.014 MLBB_PRESEND_MIN_MINIMAP_DELTA=0.010 \
  HIGHLIGHT_MAX_PANN_PROBE=16 HIGHLIGHT_MAX_STAGE1=24 HIGHLIGHT_HEATMAP=0 \
  YTDLP_SLEEP_REQUESTS=2 YTDLP_SLEEP_INTERVAL=5 YTDLP_MAX_SLEEP_INTERVAL=15 \
  CONTENT_BOT_REPO=/root/content_bot_ml; do
  key="${kv%%=*}"
  val="${kv#*=}"
  if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
  else
    echo "${key}=${val}" >>"$ENV_FILE"
  fi
done

# Search queries tuned for ~20 min ranked matches (comma-separated).
if ! grep -q '^MLBB_VOD_SEARCH_QUERIES=' "$ENV_FILE" 2>/dev/null; then
  echo 'MLBB_VOD_SEARCH_QUERIES=MLBB mythic ranked full match gameplay 20 minutes,Mobile Legends legend rank solo queue full match replay,MLBB ranked match gameplay no montage 15 minutes,Mobile Legends mythic ranked solo match full game' >>"$ENV_FILE"
fi

install -m 755 \
  "$REPO/scripts/mlbb_vod_segment_feed.py" \
  "$REPO/scripts/mlbb_vod_segment_store.py" \
  "$REPO/scripts/mlbb_fight_segment.py" \
  "$REPO/scripts/mlbb_learning_first.py" \
  "$REPO/scripts/mlbb_continuous_worker_watchdog.sh" \
  "$REPO/scripts/mlbb_job_watchdog.py" \
  "$REPO/scripts/mlbb_telegram_video.py" \
  "$REPO/scripts/mlbb_runtime_cleanup.py" \
  "$REPO/scripts/telegram_upload_bot.py" \
  "$REPO/scripts/youtube_mlbb_vod_prefs.py" \
  "$BIN/" 2>/dev/null || true

WRAPPER_VOD="$BIN/mlbb_vod_segment_feed.sh"
cat >"$WRAPPER_VOD" <<'EOF'
#!/usr/bin/env bash
# One VOD pass at a time — flock prevents duplicate feeds.
set -Eeuo pipefail
set -a
source /root/.video_bot.env
set +a
export CONTENT_BOT_REPO=/root/content_bot_ml
export PYTHONPATH="/usr/local/bin:${CONTENT_BOT_REPO}/scripts"
export HIGHLIGHT_HEATMAP=0
export MLBB_ONLY_MODE=1
export MLBB_VOD_ONLY=1
export MLBB_VOD_DISABLED=0
export MLBB_CALIBRATION_FEED_ENABLED=0
export MLBB_VOD_SHORT_MODE=1
export MLBB_VOD_MIN_SEC=900
export MLBB_VOD_MAX_SEC=2700
export MLBB_VOD_TARGET_DUR_SEC=1500
export MLBB_VOD_SKIP_LONG_SEC=2700
export MLBB_VOD_MIN_PEAK_SEC=420
export MLBB_VOD_SEARCH_LIMIT=15
export MLBB_VOD_PROBE_LIMIT=20
export MLBB_VOD_FULL_SCAN=1
export MLBB_VOD_BOOTSTRAP=0
export MLBB_VOD_SEGMENT_SEC=15
export HIGHLIGHT_WINDOW_SEC=15
export MLBB_VOD_SEGMENT_GAP_SEC=60
export MLBB_VOD_BATCH_MAX=6
export MLBB_VOD_PIPELINE_MAX_VODS=1
export MLBB_VOD_PIPELINE_MAX_MIN=120
export MLBB_VOD_SEARCH_DELAY=6
export MLBB_VOD_DOWNLOAD_DELAY=10
export MLBB_VOD_BG_WAIT_SEC=180
export HIGHLIGHT_MAX_PANN_PROBE=16
export HIGHLIGHT_MAX_STAGE1=24
export MLBB_SEEK_PREROLL=8
export MLBB_SEEK_PREROLL_60FPS=12
export MLBB_VOD_CHUNK_RENDER=1
export MLBB_VOD_SIMPLE_AUDIO=1
export MLBB_VOD_GOP_SEC=1
export MLBB_VOD_LEAD_SEC=4
export MLBB_VOD_VARIABLE_LENGTH=1
export MLBB_LEARNING_FIRST=0
export MLBB_SEND_ENABLED=1
export MLBB_FORCE_RERENDER=1
export LOGO_FILE=/nonexistent/mlbb_calibration_no_logo.png
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
exec flock -n /tmp/mlbb_vod_segment_feed.lock \
  python3 -u /usr/local/bin/mlbb_vod_segment_feed.py \
  >>/root/data/mlbb/mlbb_vod_segment_feed.log 2>&1
EOF
chmod 755 "$WRAPPER_VOD"

# Watchdog every 2 min — restarts VOD feed when idle, keeps Telegram bot alive.
TMP=$(mktemp)
crontab -l 2>/dev/null | grep -v "$MARK" \
  | grep -v 'mlbb-continuous-worker' \
  | grep -v 'mlbb_continuous_worker_watchdog' \
  | grep -v 'mlbb-calibration-cron' \
  | grep -v 'mlbb_calibration_feed.sh' \
  | grep -v 'mlbb_youtube_shorts_ingest.sh' \
  | grep -v 'mlbb-vod-segment-cron' \
  | grep -v 'mlbb_vod_segment_feed.sh' \
  >"$TMP" || true
echo "*/2 * * * * $BIN/mlbb_continuous_worker_watchdog.sh >>/root/data/mlbb/logs/mlbb_vod_watchdog.log 2>&1 $MARK watchdog" >>"$TMP"
crontab "$TMP"
rm -f "$TMP"

bash "$REPO/scripts/mlbb_deploy_sync.sh" 2>/dev/null || true
bash "$REPO/scripts/disable_vk_mlbb_scheduler.sh" 2>/dev/null || true

if systemctl is-active telegram-upload-bot >/dev/null 2>&1; then
  systemctl restart telegram-upload-bot
elif pgrep -f telegram_upload_bot.py >/dev/null 2>&1; then
  pkill -f telegram_upload_bot.py 2>/dev/null || true
  sleep 1
  nohup python3 "$BIN/telegram_upload_bot.py" >>/root/data/mlbb/telegram_upload_bot.log 2>&1 &
fi

# Start first VOD pass immediately (wrapper exits quietly if already running).
nohup "$WRAPPER_VOD" >>/root/data/mlbb/vod_only_bootstrap.log 2>&1 &

echo "===== MLBB VOD-only mode $(date -Is) ====="
echo "Shorts worker: OFF | VOD segment feed: ON (batch_max=6, 1 vod/pass)"
echo "Env:"
grep -E 'MLBB_VOD_|MLBB_CALIBRATION_FEED|MLBB_ONE_HEAVY|HIGHLIGHT_MAX' "$ENV_FILE" | head -20 || true
echo "Processes:"
pgrep -af 'mlbb_vod_segment_feed|telegram_upload_bot' || echo "(starting…)"
echo "Log tail:"
tail -3 /root/data/mlbb/mlbb_vod_segment_feed.log 2>/dev/null || echo "(empty yet)"
echo "OK: stable VOD cutting — one process, no Shorts parallel chaos"
