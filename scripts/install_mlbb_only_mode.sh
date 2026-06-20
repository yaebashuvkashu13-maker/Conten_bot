#!/usr/bin/env bash
# MLBB-only focus: stop all other games, keep Shorts calibration loop.
# Run on VPS after git pull: bash scripts/install_mlbb_only_mode.sh
set -Eeuo pipefail

REPO="${REPO:-/root/content_bot_ml}"
ENV_FILE="${ENV_FILE:-/root/.video_bot.env}"
PAUSE=/root/data/mlbb/PAUSED_PIPELINES
MARK="# mlbb-only-mode"
BIN=/usr/local/bin

mkdir -p /root/data/mlbb /root/datasets/mlbb/youtube_shorts

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
EOF

for pat in pubg_mlbb_pipeline overnight_youtube_batch overnight_catchup overnight_msk \
  overnight_watchdog viral_reference_ingest eval_owner_labels score_owner_windows \
  investor_demo_batch action_showcase_2x5 morning_pubg_standoff \
  pubg_gunfire_rebuild pubg_tiktok_batch genshin_boss_rebuild \
  mlbb_showcase_rebuild standoff overnight_orchestrator \
  pubg_brawl_direct pubg_stream_learn youtube_triple_montage \
  nightly_youtube_montage hourly_new_sources pipeline_watchdog; do
  pkill -f "$pat" 2>/dev/null || true
done
pkill -f 'run_job_until_ok.sh /root/data/mlbb/pubg_mlbb' 2>/dev/null || true
pkill -f 'run_job_until_ok.sh /root/data/mlbb/investor_demo' 2>/dev/null || true
pkill -f 'run_job_until_ok.sh /root/data/mlbb/action_showcase' 2>/dev/null || true
pkill -f 'mlbb_vod_segment_feed' 2>/dev/null || true
pkill -f 'mlbb_vod_oneoff' 2>/dev/null || true
pkill -f 'mlbb_vod_montage_feed' 2>/dev/null || true
rm -f /tmp/mlbb_vod_oneoff.lock /tmp/mlbb_vod_segment_feed.lock 2>/dev/null || true
rm -f /var/lock/smart_video_editor.lock /var/lock/overnight_msk.lock 2>/dev/null || true

touch "$ENV_FILE"
for kv in MLBB_ONLY_MODE=1 VK_MLBB_DISABLED=1 VK_MLBB_NOTIFY_EMPTY=0 \
  MLBB_VOD_DISABLED=1 MLBB_VOD_ONLY=0 MLBB_LEARNING_FIRST=0 MLBB_SEND_ENABLED=1 \
  MLBB_MAX_DAILY_SENDS=200 MLBB_CALIBRATION_BATCH=3 \
  MLBB_CALIBRATION_FEED_ENABLED=1 MLBB_ONE_HEAVY_JOB=0 \
  MLBB_TARGET_PENDING=30 MLBB_INGEST_COOLDOWN_SEC=120 MLBB_INGEST_IF_PENDING=30 MLBB_SHORTS_DAYS=365 \
  MLBB_SHORTS_MIN_YEAR=2025 MLBB_SHORTS_YEAR_ONLY=1 \
  MLBB_CALIBRATION_LENIENT=1 MLBB_CALIBRATION_FAST_INGEST=1 MLBB_SILVER_BOOTSTRAP=0 \
  MLBB_INGEST_MAX_DOWNLOADS=12 MLBB_INGEST_SKIP_IF_PENDING=30 \
  MLBB_REBUILD_INDEX_SEC=90 MLBB_PENDING_CACHE_SEC=20 MLBB_PENDING_SKIP_REPAIR=1 \
  MLBB_AUTO_TRAIN=1 MLBB_FEED_BACKFILL_LIMIT=0 MLBB_WORKER_BACKFILL_LIMIT=8 \
  MLBB_FEED_COOLDOWN_SEC=240 MLBB_FEED_COOLDOWN_PENDING_SEC=240 \
  YOUTUBE_SHORTS_FORMAT_HQ='bv*[height<=1080][height>=480]+ba/bv*[height<=1080]+ba/b[height<=1080]/best' \
  HIGHLIGHT_OWNER_BAD_PAD_SEC=90 VIRAL_MLBB_HOOK_MIN=0.06 VIRAL_MLBB_CLIP_HOOK_MIN=0.12 \
  VIRAL_SEGMENT_HOOK_MIN=0.06 VIRAL_MLBB_HOOK_FLOOR=0.06 VIRAL_MLBB_HOOK_CAP=0.12 \
  MLBB_USE_CLASSIFIER=1   MLBB_MONTAGE_COOLDOWN_SEC=14400 \
  MLBB_HERO_SHORTS_MONTAGE=0 MLBB_HERO_MONTAGE_MIN_PENDING=35 MLBB_HERO_MONTAGE_STALE_SEC=1200 \
  MLBB_JOB_MIN_GAP_SEC=45 MLBB_HERO_MONTAGE_DAILY_MAX=40 \
  MLBB_NUDGE_LOAD_MAX=0 MLBB_FEED_STALE_SEC=900 MLBB_CLAIM_STALE_SEC=300 \
  MLBB_WORKER_STALE_SEC=300 MLBB_FEED_STARVE_SEC=5400 \
  HIGHLIGHT_MAX_PANN_PROBE=18 HIGHLIGHT_MAX_STAGE1=32; do
  key="${kv%%=*}"
  val="${kv#*=}"
  if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
  else
    echo "${key}=${val}" >>"$ENV_FILE"
  fi
done
# Unquoted YOUTUBE_SHORTS_FORMAT breaks `source` (bash treats [height<=1080] as tests).
sed -i '/^YOUTUBE_SHORTS_FORMAT=/d' "$ENV_FILE" 2>/dev/null || true
sed -i '/^=1080/d' "$ENV_FILE" 2>/dev/null || true
sed -i '/^YOUTUBE_SHORTS_FORMAT_HQ=/d' "$ENV_FILE" 2>/dev/null || true
sed -i '/^MLBB_SHORTS_MIN_UPLOAD_DATE=/d' "$ENV_FILE" 2>/dev/null || true
if ! grep -q '^YOUTUBE_SHORTS_FORMAT_HQ=' "$ENV_FILE" 2>/dev/null; then
  echo "YOUTUBE_SHORTS_FORMAT_HQ='bv*[vcodec^=avc1][height<=1080][height>=720]+ba/bv*[height<=1080][height>=720]+ba/bv*[height<=1080]+ba/b[height<=1080]/best'" >>"$ENV_FILE"
fi

TMP=$(mktemp)
crontab -l 2>/dev/null >"$TMP" || true
grep -v 'pipeline-watchdog' "$TMP" \
  | grep -v 'pipeline_watchdog' \
  | grep -v 'overnight-msk' \
  | grep -v 'overnight-watchdog' \
  | grep -v 'overnight_msk' \
  | grep -v 'overnight_watchdog' \
  | grep -v 'viral_reference' \
  | grep -v 'daily-ops-morning' \
  | grep -v 'daily-ops-evening' \
  | grep -v 'mlbb-only-daily' \
  | grep -v 'mlbb-viral-weekly' \
  | grep -v 'vk-mlbb' \
  | grep -v 'vk_mlbb' \
  | grep -v 'youtube-nightly' \
  | grep -v 'investor' \
  | grep -v 'mlbb-hourly' \
  | grep -v 'tiktok_night' \
  | grep -v 'action_showcase' \
  >"${TMP}.new" || true
mv "${TMP}.new" "$TMP"

{
  cat "$TMP"
  echo "0 6 * * * $BIN/daily_ops_cron.sh morning >>/root/data/mlbb/daily_ops/cron.log 2>&1 $MARK mlbb-only-daily-morning"
  echo "0 18 * * * $BIN/daily_ops_cron.sh evening >>/root/data/mlbb/daily_ops/cron.log 2>&1 $MARK mlbb-only-daily-evening"
  echo "0 7 * * 1 python3 $BIN/mlbb_viral_analysis.py --telegram >>/root/data/mlbb/mlbb_viral_analysis.log 2>&1 $MARK mlbb-viral-weekly"
} | crontab -
rm -f "$TMP"

install -m 755 \
  "$REPO/scripts/mlbb_viral_analysis.py" \
  "$REPO/scripts/mlbb_calibration_store.py" \
  "$REPO/scripts/mlbb_youtube_shorts_ingest.py" \
  "$REPO/scripts/mlbb_calibration_feed.py" \
  "$REPO/scripts/mlbb_calibration_weekly_report.py" \
  "$REPO/scripts/mlbb_vod_segment_store.py" \
  "$REPO/scripts/mlbb_vod_segment_feed.py" \
  "$REPO/scripts/youtube_mlbb_vod_prefs.py" \
  "$REPO/scripts/mlbb_fight_segment.py" \
  "$REPO/scripts/mlbb_learning_first.py" \
  "$REPO/scripts/mlbb_continuous_worker.py" \
  "$REPO/scripts/mlbb_continuous_worker_watchdog.sh" \
  "$REPO/scripts/mlbb_job_watchdog.py" \
  "$REPO/scripts/mlbb_daily_report.py" \
  "$REPO/scripts/mlbb_vod_montage_feed.py" \
  "$REPO/scripts/mlbb_viral_threshold_sync.py" \
  "$REPO/scripts/mlbb_hq_shorts_mission.py" \
  "$REPO/scripts/mlbb_hq_shorts_mission.sh" \
  "$REPO/scripts/mlbb_hero_shorts_montage.py" \
  "$REPO/scripts/mlbb_runtime_cleanup.py" \
  "$REPO/scripts/mlbb_telegram_video.py" \
  "$REPO/scripts/mlbb_mission_rollback.sh" \
  "$REPO/scripts/telegram_upload_bot.py" \
  "$REPO/scripts/overnight_youtube_batch.py" \
  "$REPO/scripts/overnight_catchup.sh" \
  "$REPO/scripts/overnight_msk.sh" \
  "$REPO/scripts/overnight_watchdog.sh" \
  "$REPO/scripts/pipeline_watchdog.sh" \
  "$REPO/scripts/pubg_mlbb_pipeline.py" \
  "$REPO/scripts/daily_ops_cron.sh" \
  "$REPO/scripts/daily_morning_plan.py" \
  "$REPO/scripts/daily_evening_report.py" \
  "$BIN/" 2>/dev/null || true

mkdir -p /root/datasets/mlbb/vod_segments

WRAPPER_VOD="$BIN/mlbb_vod_segment_feed.sh"
cat >"$WRAPPER_VOD" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
set -a
source /root/.video_bot.env
set +a
export CONTENT_BOT_REPO=/root/content_bot_ml
export HIGHLIGHT_HEATMAP=0
export MLBB_ONLY_MODE=1
export MLBB_VOD_SHORT_MODE=1
export MLBB_VOD_MIN_SEC=900
export MLBB_VOD_MAX_SEC=2700
export MLBB_VOD_TARGET_DUR_SEC=1500
export MLBB_VOD_SKIP_LONG_SEC=2700
export MLBB_VOD_MIN_PEAK_SEC=420
export MLBB_VOD_SEARCH_LIMIT=25
export MLBB_VOD_SEARCH_QUERIES="MLBB mythic ranked full match gameplay 20 minutes,Mobile Legends legend rank solo queue full match replay,MLBB ranked match gameplay no montage 15 minutes,Mobile Legends mythic ranked solo match full game"
export MLBB_VOD_PROBE_LIMIT=30
export MLBB_VOD_FULL_SCAN=1
export MLBB_VOD_BOOTSTRAP=0
export MLBB_VOD_SEGMENT_SEC=15
export HIGHLIGHT_WINDOW_SEC=15
export MLBB_VOD_SEGMENT_GAP_SEC=60
export MLBB_VOD_BATCH_MAX=30
export MLBB_SEEK_PREROLL=8
export MLBB_SEEK_PREROLL_60FPS=12
export MLBB_VOD_CHUNK_RENDER=1
export MLBB_VOD_SIMPLE_AUDIO=1
export MLBB_VOD_GOP_SEC=1
export MLBB_VOD_LEAD_SEC=4
export MLBB_VOD_VARIABLE_LENGTH=1
export MLBB_LEARNING_FIRST=0
export MLBB_SEND_ENABLED=1
export MLBB_PRESEND_FREEZE_MIN_DUR=1.2
export MLBB_PRESEND_FREEZE_MAX_START=3.0
export MLBB_PRESEND_MIN_MOTION=0.014
export MLBB_PRESEND_MIN_MINIMAP_DELTA=0.010
export MLBB_FORCE_RERENDER=1
export MLBB_VOD_PIPELINE_MAX_MIN=300
export MLBB_VOD_PIPELINE_MAX_VODS=8
export MLBB_VOD_SEARCH_DELAY=4
export MLBB_VOD_DOWNLOAD_DELAY=8
export YTDLP_SLEEP_REQUESTS=1.5
export YTDLP_SLEEP_INTERVAL=4
export YTDLP_MAX_SLEEP_INTERVAL=12
export LOGO_FILE=/nonexistent/mlbb_calibration_no_logo.png
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
flock -n /tmp/mlbb_vod_segment_feed.lock \
  python3 /usr/local/bin/mlbb_vod_segment_feed.py \
  >>/root/data/mlbb/mlbb_vod_segment_feed.log 2>&1
EOF
chmod 755 "$WRAPPER_VOD"

MARK_VOD="# mlbb-vod-segment-cron"
TMP2=$(mktemp)
crontab -l 2>/dev/null | grep -v "$MARK_VOD" >"$TMP2" || true
crontab "$TMP2"
rm -f "$TMP2"

bash "$REPO/scripts/install_mlbb_continuous_worker.sh" 2>/dev/null || true

# Worker owns feed+ingest loop — do not install legacy calibration crons on top.
TMP_CAL=$(mktemp)
crontab -l 2>/dev/null | grep -v 'mlbb-calibration-cron' \
  | grep -v 'mlbb_calibration_feed.sh' \
  | grep -v 'mlbb_youtube_shorts_ingest.sh' >"$TMP_CAL" || true
crontab "$TMP_CAL"
rm -f "$TMP_CAL"

bash "$REPO/scripts/mlbb_deploy_sync.sh" 2>/dev/null || true

clean_system_cron() {
  local f
  for f in /etc/cron.d/mlbb_video /etc/cron.d/youtube_proactive /etc/cron.d/overnight_msk; do
    [[ -f "$f" ]] || continue
    grep -v 'pipeline-watchdog' "$f" \
      | grep -v 'pipeline_watchdog' \
      | grep -v 'overnight-msk' \
      | grep -v 'overnight-watchdog' \
      | grep -v 'overnight_msk' \
      | grep -v 'overnight_watchdog' \
      | grep -v 'youtube_triple_montage' \
      | grep -v 'yt-proactive' \
      >"${f}.mlbb_only" || true
    if grep -qE '^[0-9*]' "${f}.mlbb_only" 2>/dev/null; then
      mv "${f}.mlbb_only" "$f"
      chmod 644 "$f"
    else
      rm -f "$f" "${f}.mlbb_only"
      echo "removed $f (only multi-game jobs)"
    fi
  done
}
clean_system_cron

if systemctl is-active telegram-upload-bot >/dev/null 2>&1; then
  systemctl restart telegram-upload-bot
elif pgrep -f telegram_upload_bot.py >/dev/null 2>&1; then
  pkill -f telegram_upload_bot.py 2>/dev/null || true
  sleep 1
  nohup python3 "$BIN/telegram_upload_bot.py" >>/root/telegram_upload_bot.log 2>&1 &
fi

bash "$REPO/scripts/disable_vk_mlbb_scheduler.sh" 2>/dev/null || true

if [[ "${MLBB_CALIBRATION_FEED_ENABLED:-1}" != "1" ]]; then
  bash "$REPO/scripts/install_mlbb_calibration_cron.sh"
fi

if [[ -f /etc/cron.d/overnight_msk ]]; then
  rm -f /etc/cron.d/overnight_msk
  echo "removed /etc/cron.d/overnight_msk"
fi

echo "===== MLBB-only mode $(date -Is) ====="
echo "Paused pipelines in $PAUSE"
echo "Cron:"
crontab -l 2>/dev/null | grep -E 'mlbb|daily' || true
echo "Remaining multi-game procs:"
pgrep -af 'pubg_mlbb|overnight|viral_reference|eval_owner|score_owner|genshin|standoff|wot' || echo "(none)"
echo "OK MLBB-only: 24/7 continuous worker — fresh YouTube Shorts (~15/hour), VOD off"
echo "/etc/cron.d:"
ls -la /etc/cron.d/mlbb_video /etc/cron.d/youtube_proactive 2>/dev/null || echo "(multi-game crons removed)"
