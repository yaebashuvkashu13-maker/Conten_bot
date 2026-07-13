#!/usr/bin/env bash
# MLBB VOD-only: stable ~20-min match cutting, one process, owner Shorts exemplars.
# Run on VPS after git pull: bash scripts/install_mlbb_vod_only.sh
set -Eeuo pipefail

REPO="${REPO:-/root/content_bot_ml}"
ENV_FILE="${ENV_FILE:-/root/.video_bot.env}"
PAUSE=/root/data/mlbb/PAUSED_PIPELINES
MARK="# mlbb-vod-only-mode"
BIN=/usr/local/bin

mkdir -p /root/data/mlbb /root/datasets/mlbb/vod_segments /root/data/mlbb/logs \
  /root/data/mlbb/youtube_nightly/inbox \
  /root/datasets/mlbb/banner_calibration \
  "$REPO/data/mlbb_kill_banners/wiki" \
  "$REPO/data/mlbb_kill_banners/vod_crops" \
  "$REPO/data/mlbb_kill_banners/owner_cal/positive" \
  "$REPO/data/mlbb_kill_banners/owner_cal/negative"

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
mlbb_vod_montage_feed.py
mlbb_vod_oneoff.py
EOF

kill_all_competing() {
  for pat in mlbb_continuous_worker mlbb_calibration_feed mlbb_youtube_shorts_ingest \
    mlbb_hero_shorts_montage mlbb_vod_montage_feed \
    pubg_mlbb_pipeline overnight_youtube_batch overnight_catchup \
    overnight_msk overnight_watchdog viral_reference_ingest eval_owner_labels score_owner_windows \
    investor_demo_batch action_showcase_2x5 morning_pubg_standoff \
    pubg_gunfire_rebuild pubg_tiktok_batch genshin_boss_rebuild \
    mlbb_showcase_rebuild standoff overnight_orchestrator \
    pubg_brawl_direct pubg_stream_learn youtube_triple_montage \
    nightly_youtube_montage hourly_new_sources pipeline_watchdog highlight_train; do
    pkill -f "$pat" 2>/dev/null || true
  done
  if [[ ! -f /root/data/mlbb/OWNER_BATCH_RUNNING ]]; then
    pkill -f 'mlbb_vod_oneoff' 2>/dev/null || true
  fi
  pkill -f 'run_job_until_ok.sh /root/data/mlbb/pubg_mlbb' 2>/dev/null || true
  pkill -f 'run_job_until_ok.sh /root/data/mlbb/investor_demo' 2>/dev/null || true
  pkill -f 'run_job_until_ok.sh /root/data/mlbb/action_showcase' 2>/dev/null || true
}

kill_all_competing
sleep 2
kill_all_competing
pkill -9 -f 'mlbb_continuous_worker' 2>/dev/null || true
pkill -9 -f 'mlbb_calibration_feed' 2>/dev/null || true
pkill -9 -f 'mlbb_youtube_shorts_ingest' 2>/dev/null || true
if [[ "${MLBB_VOD_INSTALL_RESTART_FEED:-0}" == "1" ]]; then
  pkill -9 -f 'mlbb_vod_segment_feed.py' 2>/dev/null || true
  sleep 1
  rm -f /tmp/mlbb_vod_oneoff.lock /tmp/mlbb_vod_segment_feed.lock 2>/dev/null || true
fi
rm -f /tmp/mlbb_calibration_feed.lock /tmp/mlbb_youtube_shorts_ingest.lock 2>/dev/null || true
rm -f /tmp/mlbb_continuous_worker.lock 2>/dev/null || true
rm -f /root/data/mlbb/calibration_feed_empty_notify.json 2>/dev/null || true
rm -f /var/lock/smart_video_editor.lock /var/lock/overnight_msk.lock 2>/dev/null || true

disable_shorts_wrapper() {
  local name="$1"
  cat >"$BIN/$name" <<'EOF'
#!/usr/bin/env bash
echo "DISABLED: MLBB VOD-only — Shorts calibration off ($(date -Is))" >&2
exit 0
EOF
  chmod 755 "$BIN/$name"
}

touch "$ENV_FILE"
for kv in   MLBB_ONLY_MODE=1 VK_MLBB_DISABLED=1 VK_MLBB_NOTIFY_EMPTY=0 \
  MLBB_SHORTS_ONLY=0 MLBB_VOD_DISABLED=0 MLBB_VOD_ONLY=1 MLBB_LEARNING_FIRST=1 MLBB_SEND_ENABLED=0 \
  MLBB_MAX_DAILY_SENDS=10 MLBB_CALIBRATION_FEED_ENABLED=0 MLBB_ONE_HEAVY_JOB=1 \
  MLBB_CALIBRATION_LENIENT=0 MLBB_CALIBRATION_FAST_INGEST=0 MLBB_SILVER_BOOTSTRAP=0 \
  MLBB_FEED_RESCORE=0 MLBB_OWNER_EMERGENCY=0 MLBB_OWNER_REQUIRE_SCORE=0 \
  MLBB_INGEST_ALWAYS=0 MLBB_AUTO_TRAIN=0 MLBB_LEARN_APPLY_TRAIN=1 MLBB_FEED_RE_GATE=0 \
  MLBB_USE_CLASSIFIER=0 MLBB_VOD_QUALITY_MODEL=1 MLBB_VOD_QUALITY_MODEL_REQUIRED=1 \
  MLBB_TRAIN_HEAVY_FEATURES=0 MLBB_HERO_SHORTS_MONTAGE=0 MLBB_MONTAGE_COOLDOWN_SEC=86400 \
  MLBB_NUDGE_LOAD_MAX=0 MLBB_FEED_STALE_SEC=7200 MLBB_WORKER_STALE_SEC=7200 \
  MLBB_VOD_BATCH_MAX=0 MLBB_VOD_SEND_ONE=1 MLBB_VOD_MAX_PER_VOD=0 \
  MLBB_VOD_INTERVAL_GAP_SEC=3 MLBB_VOD_PIPELINE_MAX_VODS=3 \
  MLBB_VOD_PIPELINE_MAX_MIN=45 MLBB_VOD_MAX_PROCESS_MIN=20 MLBB_VOD_IDLE_SEC=15 \
  MLBB_VOD_PROBE_LIMIT=48 MLBB_VOD_SEGMENT_SEC=15 HIGHLIGHT_WINDOW_SEC=15 \
  MLBB_VOD_MIN_SEC=180 MLBB_VOD_MAX_SEC=1200 MLBB_VOD_TARGET_DUR_SEC=780 \
  MLBB_VOD_SKIP_LONG_SEC=1200 MLBB_VOD_MIN_PEAK_SEC=300 \
  MLBB_VOD_SKIP_REVALIDATE=0 \
  MLBB_VOD_SEASON=41 MLBB_VOD_YOUTUBE_DURATION_FILTER=1 \
  MLBB_VOD_SEARCH_FRESH=1 MLBB_VOD_MAX_AGE_DAYS=35 MLBB_VOD_SEARCH_SUPPLEMENT=1 \
  MLBB_VOD_SEARCH_LIMIT=20 MLBB_VOD_SEARCH_BATCH=3 MLBB_VOD_SEARCH_DELAY=6 MLBB_VOD_DOWNLOAD_DELAY=10 \
  MLBB_VOD_BG_WAIT_SEC=180 MLBB_VOD_AUTO_DOWNLOAD=1 MLBB_VOD_FULL_SCAN=1 \
  MLBB_VOD_BOOTSTRAP=0 MLBB_VOD_CHUNK_RENDER=1 MLBB_VOD_SIMPLE_AUDIO=1 \
  MLBB_VOD_GOP_SEC=1 MLBB_VOD_LEAD_SEC=4 MLBB_SAVAGE_BANNER_LEAD_SEC=14 MLBB_MANIAC_BANNER_LEAD_SEC=10 MLBB_VOD_VARIABLE_LENGTH=1 \
  MLBB_VOD_ENCODE_CRF=23 MLBB_VOD_ENCODE_AUDIO_K=128 MLBB_VOD_ENCODE_PRESET=fast \
  MLBB_VOD_NO_CROP=1 MLBB_VOD_LANDSCAPE=1 MLBB_VOD_OUT_WIDTH=1280 MLBB_VOD_OUT_HEIGHT=720 \
  MLBB_VOD_OWNER_EXEMPLARS=1 MLBB_VOD_SYNC_OWNER=1 MLBB_VOD_MIN_CLIP_SCORE=0.06 \
  MLBB_FIGHT_MIN_SEC=8 MLBB_FIGHT_MAX_SEC=28 MLBB_FIGHT_HARD_MAX_SEC=32 MLBB_FIGHT_POST_SEC=4 \
  MLBB_FIGHT_TRIM_LONG=1 MLBB_FIGHT_SUSTAIN_QUIET_BINS=3 \
  MLBB_VOD_KILL_BANNER=1 MLBB_KILL_BANNER_REQUIRED=1 MLBB_KILL_BANNER_MIN_TIER=double \
  MLBB_KILL_BANNER_COLOR_ONLY=0 MLBB_KILL_BANNER_SCAN_BEFORE=20 MLBB_KILL_BANNER_SCAN_AFTER=10 \
  MLBB_VOD_BANNER_PREFILTER=1 MLBB_VOD_BANNER_PREFILTER_PEAKS=12 MLBB_VOD_BANNER_DISCOVER=1 \
  MLBB_VOD_BANNER_DISCOVER_FULL=1 MLBB_VOD_BANNER_TIMESTEP_SCAN=1 \
  MLBB_VOD_BANNER_PRESEND=1 MLBB_VOD_BANNER_SKIP_ON_MISS=0 MLBB_VOD_MOTION_ANCHOR_OK=0 \
  MLBB_BANNER_POV_MATCH=1 MLBB_BANNER_POV_RESCAN=0 MLBB_BANNER_POV_MIN_SIM=0.30 MLBB_BANNER_POST_SEC=5 MLBB_BANNER_MAX_REL_POS=0.58 \
  MLBB_BANNER_PEAK_MAX_DIST_SEC=25 MLBB_VOD_PRESEND_SKIP_VISUAL_ON_BANNER=1 \
  MLBB_VOD_PRESEND_FAST_BANNER=1 \
  MLBB_CLIP_COMBAT_SAMPLES=10   MLBB_CLIP_MIN_ACTIVE_RATIO=0.40 MLBB_CLIP_MIN_HUD_RATE=0.42 \
  MLBB_CLIP_WINDOW_MIN_MOTION=0.018 MLBB_CLIP_WINDOW_MIN_MINIMAP=0.010 \
  MLBB_CLIP_MIN_TAIL_MOTION=0.016 MLBB_CLIP_MIN_TAIL_MINIMAP=0.007 \
  MLBB_VOD_QUALITY_MODE=1 MLBB_VOD_BAD_SHARE_TARGET=0.20 MLBB_VOD_DISABLE_SOFTEN=1 \
  MLBB_VOD_TAIL_EXCLUDE_SEC=75 MLBB_VOD_RANK_SCREEN_SEC=90 \
  MLBB_KILL_BANNER_DISCOVER_MAX_PROBES=64 MLBB_KILL_BANNER_DISCOVER_MAX_SEC=180 \
  MLBB_KILL_BANNER_SPARSE_MAX_SEC=180 MLBB_KILL_BANNER_DENSE_MAX_SEC=360 \
  MLBB_KILL_BANNER_TIMESTEP_SAMPLES=48 MLBB_KILL_BANNER_SPARSE_SAMPLES=48 MLBB_KILL_BANNER_SPARSE_STEP=3 \
  MLBB_KILL_BANNER_TITLE_MAX_SEC=240 MLBB_KILL_BANNER_TITLE_SAMPLES=64 MLBB_KILL_BANNER_TITLE_STEP=2 \
  MLBB_KILL_BANNER_DISCOVER_PEAK_HINTS=16 MLBB_KILL_BANNER_DISCOVER_PEAK_MAX_PROBES=8 MLBB_TEAMFIGHT_HUD_CAP=32 \
  MLBB_KILL_BANNER_DISCOVER_STEP=3 MLBB_VOD_BANNER_DENSE_SEC=0 MLBB_BANNER_DENSE_CHUNK_SEC=60 \
  MLBB_SAVAGE_DENSE_MAX_SPAN_SEC=360 MLBB_DENSE_STOP_ON_SAVAGE=1 \
  MLBB_SAVAGE_CLIP_MIN_SEC=8 MLBB_MANIAC_CLIP_MIN_SEC=12 \
  MLBB_VOD_HIGHLIGHT_SEND_ONE=0 MLBB_VOD_COLLECT_ONE=1 \
  MLBB_TITLE_SAVAGE_MIN_TIER=1 MLBB_BANNER_SAVAGE_TITLE_START_SEC=3 MLBB_VOD_SAVAGE_SHORT_OK=1 MLBB_VOD_SAVAGE_SHORT_MIN_SEC=60 \
  MLBB_KILL_BANNER_TAIL_PASS=0 MLBB_KILL_BANNER_COLOR_OCR_WINDOW_MIN=0.12 \
  MLBB_BANNER_REF_MATCH=1 MLBB_BANNER_REF_MIN_SIM=0.34 MLBB_BANNER_OWNER_EVIDENCE_MARGIN=0.10 \
  MLBB_BANNER_NEG_EXCLUDE_REASONS=wrong_hero \
  MLBB_FEEDBACK_GATE=1 MLBB_FEEDBACK_GATE_DISCOVERY=0 MLBB_VOD_ZERO_RECOVERY_SOFTEN=5 \
  MLBB_BANNER_ANCHOR_HOOK_MIN=0.04 MLBB_BANNER_MIN_HOOK=0.05 \
  MLBB_BANNER_HIGH_TIER_HOOK_MULT=0.80 MLBB_BANNER_TRIPLE_HOOK_MULT=0.90 \
  MLBB_VOD_STRICT_PEAK_TRIES=16 \
  MLBB_VOD_FAST_PROBE=1 MLBB_VOD_SEED_FROM_FAST_PROBE=1 \
  MLBB_VOD_ZERO_STREAK_SOFTEN=4 MLBB_VOD_ADAPTIVE_NOTIFY=1 MLBB_VOD_EXHAUST_NOTIFY=0 \
  MLBB_VOD_PRESEND_REJECT_NOTIFY=0 \
  MLBB_VOD_STREAK_CIRCUIT_MAX=12 MLBB_VOD_ZERO_YIELD_MAX=3 MLBB_VOD_CIRCUIT_SILENCE_SEC=7200 \
  SHOOTER_VOD_DISABLE_SOFTEN=1 EXTENDED_VOD_DISABLE_SOFTEN=1 \
  SHOOTER_VOD_ZERO_STREAK_SOFTEN=2 SHOOTER_VOD_ADAPTIVE_NOTIFY=1 SHOOTER_VOD_EXHAUST_NOTIFY=1 \
  SHOOTER_VOD_SEGMENT_GAP_SEC=45 PUBG_METRO_GATE=1 \
  SHOOTER_VOD_FAST_PROBE=1 SHOOTER_VOD_PREFER_RUSSIAN=1 SHOOTER_VOD_SKIP_INTELLICLIP=1 \
  SHOOTER_VOD_MAX_PANN_PROBE=24 SHOOTER_VOD_SEED_FROM_FAST_PROBE=1 \
  GENSHIN_VOD_FAST_PROBE=1 WOT_VOD_FAST_PROBE=1 \
  VOD_ANALYSIS_USE_PROXY=1 VOD_POOL_TTL_SEC=21600 \
  MLBB_TEAMFIGHT_MIN_SCORE=0.45 GENSHIN_BOSS_BAR_MIN_RATIO=0.7 \
  DAILY_GAME_CYCLE_ENABLED=1 DAILY_MLBB_QUOTA=10 DAILY_PUBG_QUOTA=10 DAILY_STANDOFF_QUOTA=10 \
  MLBB_VOD_MAX_PER_VOD=5 MLBB_VOD_SOFT_MAX_PEAK_TRIES=12 \
  MLBB_VOD_SEGMENT_GAP_SEC=120 MLBB_VOD_LENIENT_UNIFORM=0 MLBB_VOD_TAIL_MIN_HUD_RATE=0.55 SMART_UNIFORM_MIN_HUD_RATE=0.70 \
  MLBB_FORCE_RERENDER=0 MLBB_PRESEND_FREEZE_MIN_DUR=1.2 MLBB_PRESEND_FREEZE_MAX_START=3.0 \
  MLBB_PRESEND_MIN_MOTION=0.012 MLBB_PRESEND_MIN_MINIMAP_DELTA=0.010 \
  HIGHLIGHT_USE_OWNER_ANCHORS=1 HIGHLIGHT_CLIP_DISABLED=0 HIGHLIGHT_COLD_START=1 \
  MLBB_TRAIN_MAX_EXEMPLARS=500 MLBB_TRAIN_MIN_PRECISION=0.85 \
  MLBB_TRAIN_MIN_RECALL=0.70 MLBB_TRAIN_MAX_BAD_FALSE_PASS=0.10 \
  VOD_CALIBRATION_SEND_AS_FILE=0 \
  HIGHLIGHT_MAX_PANN_PROBE=12 HIGHLIGHT_MAX_STAGE1=48 HIGHLIGHT_HEATMAP=0 \
  HIGHLIGHT_EXEMPLAR_MAX=4 HIGHLIGHT_EXEMPLAR_FRAME_FRACTIONS=0.5 HIGHLIGHT_CLIP_BATCH_SIZE=8 \
  HIGHLIGHT_PARALLEL_WORKERS=3 SMART_FFMPEG_THREADS=4 \
  MLBB_VOD_HERO_GATE=1 MLBB_SOURCE_BLOCK_MIN_VODS=3 \
  VOD_HEARTBEAT_INTERVAL_SEC=30 VOD_HEARTBEAT_FRESH_SEC=600 \
  VOD_TRAIN_MIN_PRECISION=0.85 VOD_TRAIN_MIN_RECALL=0.70 VOD_TRAIN_MAX_BAD_FALSE_PASS=0.10 \
  VOD_PUBG_QUALITY_MODEL=1 VOD_STANDOFF_QUALITY_MODEL=1 \
  VOD_GENSHIN_QUALITY_MODEL=1 VOD_WOT_QUALITY_MODEL=1 \
  VISUAL_MLBB_MENU_OVERLAY_MAX=0.85 VISUAL_MLBB_MIN_FRAMES_PASS=2 \
  OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 \
  YTDLP_SLEEP_REQUESTS=2 YTDLP_SLEEP_INTERVAL=5 YTDLP_MAX_SLEEP_INTERVAL=15 \
  CONTENT_BOT_REPO=/root/content_bot_ml; do
  key="${kv%%=*}"
  val="${kv#*=}"
  if [[ -z "$key" || "$key" == *.sh ]]; then
    continue
  fi
  if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
  else
    echo "${key}=${val}" >>"$ENV_FILE"
  fi
done

# EU split-server: MLBB on server 1, PUBG/Standoff on server 2 — keep MLBB quota at 0.
if [[ -f /root/data/mlbb/EU_PUBG_ONLY ]]; then
  sed -i 's/^DAILY_MLBB_QUOTA=.*/DAILY_MLBB_QUOTA=0/' "$ENV_FILE"
fi

VOD_SEARCH_CSV="$(python3 - <<PY
import sys
sys.path.insert(0, "$REPO/scripts")
from youtube_mlbb_vod_prefs import default_vod_search_queries_csv
print(default_vod_search_queries_csv())
PY
)"
sed -i '/^MLBB_VOD_SEARCH_QUERIES=/d' "$ENV_FILE"
printf 'MLBB_VOD_SEARCH_QUERIES="%s"\n' "$VOD_SEARCH_CSV" >>"$ENV_FILE"

install -m 755 \
  "$REPO/scripts/daily_game_cycle.py" \
  "$REPO/scripts/daily_cycle_runner.py" \
  "$REPO/scripts/daily_cycle_start_quota_now.py" \
  "$REPO/scripts/shooter_vod_segment_feed.py" \
  "$REPO/scripts/shooter_vod_segment_store.py" \
  "$REPO/scripts/shooter_vod_adaptive_gate.py" \
  "$REPO/scripts/shooter_vod_fast_scan.py" \
  "$REPO/scripts/mlbb_vod_fast_scan.py" \
  "$REPO/scripts/genshin_vod_fast_scan.py" \
  "$REPO/scripts/wot_vod_fast_scan.py" \
  "$REPO/scripts/vod_analysis_cache.py" \
  "$REPO/scripts/mlbb_teamfight_detector.py" \
  "$REPO/scripts/pubg_killfeed_ocr.py" \
  "$REPO/scripts/genshin_boss_segment.py" \
  "$REPO/scripts/wot_brawl_segment.py" \
  "$REPO/scripts/vod_scan_state.py" \
  "$REPO/scripts/pubg_metro_royale_gate.py" \
  "$REPO/scripts/youtube_shooter_vod_prefs.py" \
  "$REPO/scripts/mlbb_vod_segment_feed.py" \
  "$REPO/scripts/mlbb_vod_segment_store.py" \
  "$REPO/scripts/mlbb_vod_intervals.py" \
  "$REPO/scripts/mlbb_kill_banner.py" \
  "$REPO/scripts/mlbb_vod_title.py" \
  "$REPO/scripts/mlbb_hero_roles.py" \
  "$REPO/scripts/mlbb_source_yield.py" \
  "$REPO/scripts/mlbb_vod_dense_audit.py" \
  "$REPO/scripts/mlbb_vod_audit_send.py" \
  "$REPO/scripts/mlbb_vod_dense_hints.py" \
  "$REPO/scripts/mlbb_vod_title_rescan.py" \
  "$REPO/scripts/mlbb_banner_pov_match.py" \
  "$REPO/scripts/mlbb_banner_ref_ingest.py" \
  "$REPO/scripts/mlbb_banner_ref_match.py" \
  "$REPO/scripts/mlbb_banner_calibration_reasons.py" \
  "$REPO/scripts/mlbb_banner_calibration_store.py" \
  "$REPO/scripts/mlbb_banner_calibration_capture.py" \
  "$REPO/scripts/mlbb_banner_calibration_feed.py" \
  "$REPO/scripts/mlbb_banner_calibration_burst.py" \
  "$REPO/scripts/mlbb_banner_calibration_apply.py" \
  "$REPO/scripts/mlbb_banner_calibration_gate.py" \
  "$REPO/scripts/mlbb_banner_calibration_positive_feed.py" \
  "$REPO/scripts/mlbb_banner_calibration_positive_fast.py" \
  "$REPO/scripts/mlbb_banner_calibration_scan_send.py" \
  "$REPO/scripts/mlbb_banner_calibration_quick_send.py" \
  "$REPO/scripts/mlbb_banner_positive_scan.sh" \
  "$REPO/scripts/mlbb_feedback_pattern_miner.py" \
  "$REPO/scripts/mlbb_feedback_gate_tune.py" \
  "$REPO/scripts/mlbb_fight_segment.py" \
  "$REPO/scripts/mlbb_vod_adaptive_gate.py" \
  "$REPO/scripts/audit_mlbb_vod_inbox.py" \
  "$REPO/scripts/mlbb_pipeline_mode.py" \
  "$REPO/scripts/mlbb_calibration_store.py" \
  "$REPO/scripts/mlbb_calibration_feed.py" \
  "$REPO/scripts/mlbb_youtube_shorts_ingest.py" \
  "$REPO/scripts/mlbb_continuous_worker.py" \
  "$REPO/scripts/mlbb_learning_first.py" \
  "$REPO/scripts/mlbb_owner_learning.py" \
  "$REPO/scripts/mlbb_owner_feedback.py" \
  "$REPO/scripts/mlbb_model_training.py" \
  "$REPO/scripts/mlbb_vod_quality_model.py" \
  "$REPO/scripts/highlight_train.py" \
  "$REPO/scripts/mlbb_learn_apply.sh" \
  "$REPO/scripts/eval_learning_first_gate.py" \
  "$REPO/scripts/score_owner_windows.py" \
  "$REPO/scripts/highlight_scorer.py" \
  "$REPO/scripts/vod_state_io.py" \
  "$REPO/scripts/vod_pipeline_heartbeat.py" \
  "$REPO/scripts/vod_env.py" \
  "$REPO/scripts/telegram_access.py" \
  "$REPO/scripts/extended_vod_fast_scan.py" \
  "$REPO/scripts/vod_owner_learning.py" \
  "$REPO/scripts/vod_owner_feedback.py" \
  "$REPO/scripts/vod_quality_model.py" \
  "$REPO/scripts/vod_learn_apply.sh" \
  "$REPO/scripts/strict_montage_direct.py" \
  "$REPO/scripts/mlbb_continuous_worker_watchdog.sh" \
  "$REPO/scripts/mlbb_job_watchdog.py" \
  "$REPO/scripts/mlbb_vod_health_watchdog.sh" \
  "$REPO/scripts/mlbb_vod_only_verify.sh" \
  "$REPO/scripts/vps_apply_vod_only.sh" \
  "$REPO/scripts/mlbb_telegram_video.py" \
  "$REPO/scripts/mlbb_runtime_cleanup.py" \
  "$REPO/scripts/telegram_upload_bot.py" \
  "$REPO/scripts/youtube_mlbb_vod_prefs.py" \
  "$REPO/scripts/nightly_youtube_montage.py" \
  "$REPO/scripts/youtube_download.py" \
  "$REPO/scripts/youtube_game_prefs.py" \
  "$BIN/" 2>/dev/null || true

WRAPPER_VOD="$BIN/mlbb_vod_segment_feed.sh"
cat >"$WRAPPER_VOD" <<'EOF'
#!/usr/bin/env bash
# One VOD feed at a time — continuous loop, flock prevents duplicates.
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
export MLBB_VOD_NO_CROP=1
export MLBB_VOD_LANDSCAPE=1
export MLBB_VOD_OUT_WIDTH=1280
export MLBB_VOD_OUT_HEIGHT=720
export MLBB_VOD_VARIABLE_LENGTH=1
export MLBB_VOD_OWNER_EXEMPLARS=1
export HIGHLIGHT_USE_OWNER_ANCHORS=1
export VOD_CALIBRATION_SEND_AS_FILE=0
export HIGHLIGHT_CLIP_DISABLED=0
export MLBB_VOD_SEND_ONE=1
export MLBB_VOD_BATCH_MAX=0
export MLBB_VOD_MAX_PER_VOD=0
export MLBB_VOD_INTERVAL_GAP_SEC=3
export MLBB_VOD_PIPELINE_MAX_VODS=3
export MLBB_VOD_PIPELINE_MAX_MIN=45
export MLBB_VOD_PROBE_LIMIT=48
export MLBB_VOD_SEGMENT_GAP_SEC=75
export MLBB_FIGHT_MAX_SEC=28
export MLBB_FIGHT_HARD_MAX_SEC=32
export MLBB_FIGHT_POST_SEC=4
export MLBB_FIGHT_TRIM_LONG=1
export MLBB_BANNER_POST_SEC=5
export MLBB_BANNER_NEG_REF_MATCH=1
export MLBB_BANNER_OWNER_GATE=1
export MLBB_BANNER_POS_REF_MATCH=1
export MLBB_BANNER_CALIB_TARGET=50
export MLBB_BANNER_CALIB_BATCH=3
export MLBB_BANNER_CALIB_VODS=2
export MLBB_BANNER_CALIB_MIN_TIER=1
export MLBB_VOD_BANNER_PRESEND=1
export MLBB_VOD_MOTION_ANCHOR_OK=0
export MLBB_VOD_FAST_PROBE=1
export HIGHLIGHT_MAX_PANN_PROBE=12
export HIGHLIGHT_MAX_STAGE1=48
export HIGHLIGHT_EXEMPLAR_MAX=4
export HIGHLIGHT_EXEMPLAR_FRAME_FRACTIONS=0.5
export HIGHLIGHT_CLIP_BATCH_SIZE=8
export HIGHLIGHT_PARALLEL_WORKERS=3
export SMART_FFMPEG_THREADS=4
export MLBB_VOD_MIN_PEAK_SEC=300
export MLBB_VOD_SKIP_REVALIDATE=0
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export LOGO_FILE=/nonexistent/mlbb_calibration_no_logo.png
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
IDLE_SEC="${MLBB_VOD_IDLE_SEC:-25}"
while true; do
  python3 -u /usr/local/bin/daily_cycle_runner.py \
    >>/root/data/mlbb/mlbb_vod_segment_feed.log 2>&1 || true
  sleep "$IDLE_SEC"
done
EOF
chmod 755 "$WRAPPER_VOD"

disable_shorts_wrapper mlbb_calibration_feed.sh
disable_shorts_wrapper mlbb_youtube_shorts_ingest.sh

for f in /etc/cron.d/mlbb_video /etc/cron.d/youtube_proactive; do
  [[ -f "$f" ]] || continue
  grep -v 'mlbb_calibration_feed' "$f" \
    | grep -v 'mlbb_youtube_shorts_ingest' \
    | grep -v 'mlbb_continuous_worker' \
    | grep -v 'continuous.worker' >"${f}.vod_only" || true
  if grep -qE '^[0-9*]' "${f}.vod_only" 2>/dev/null; then
    mv "${f}.vod_only" "$f"
  else
    rm -f "$f" "${f}.vod_only"
  fi
done

# Single supervisor process for the VOD loop (not cron-spawned duplicates).
if [[ "${MLBB_VOD_INSTALL_RESTART_FEED:-0}" == "1" ]]; then
  pkill -f 'mlbb_vod_segment_feed.sh' 2>/dev/null || true
  sleep 1
  nohup "$WRAPPER_VOD" >>/root/data/mlbb/vod_only_supervisor.log 2>&1 &
elif ! pgrep -f 'mlbb_vod_segment_feed.sh' >/dev/null 2>&1; then
  nohup "$WRAPPER_VOD" >>/root/data/mlbb/vod_only_supervisor.log 2>&1 &
else
  echo "VOD feed supervisor already running — left untouched"
fi

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
echo "*/3 * * * * $BIN/mlbb_continuous_worker_watchdog.sh >>/root/data/mlbb/logs/mlbb_vod_watchdog.log 2>&1 $MARK watchdog" >>"$TMP"
echo "*/5 * * * * $BIN/mlbb_vod_health_watchdog.sh >>/root/data/mlbb/logs/mlbb_vod_health.log 2>&1 $MARK health" >>"$TMP"
echo "25 3 * * * /usr/local/bin/mlbb_learn_apply.sh >>/root/data/mlbb/logs/mlbb_learn_apply.log 2>&1 $MARK daily-learn" >>"$TMP"
echo "40 3 * * * /usr/local/bin/vod_learn_apply.sh all >>/root/data/mlbb/logs/vod_learn_apply.log 2>&1 $MARK daily-all-game-learn" >>"$TMP"
install -m 755 "$REPO/scripts/mlbb_banner_positive_scan.sh" /usr/local/bin/mlbb_banner_positive_scan.sh 2>/dev/null || true
crontab "$TMP"
rm -f "$TMP"

bash "$REPO/scripts/mlbb_deploy_sync.sh" 2>/dev/null || true
bash "$REPO/scripts/disable_vk_mlbb_scheduler.sh" 2>/dev/null || true

if systemctl is-active telegram-upload-bot >/dev/null 2>&1; then
  systemctl restart telegram-upload-bot
elif pgrep -f telegram_upload_bot.py >/dev/null 2>&1; then
  pkill -f telegram_upload_bot.py 2>/dev/null || true
  sleep 1
  nohup env PYTHONPATH="/usr/local/bin:${CONTENT_BOT_REPO:-/root/content_bot_ml}/scripts" \
    python3 "$BIN/telegram_upload_bot.py" >>/root/data/mlbb/telegram_upload_bot.log 2>&1 &
fi

sleep 3
bash "$BIN/mlbb_vod_only_verify.sh" || true

echo "===== MLBB banner reference bank $(date -Is) ====="
python3 "$REPO/scripts/mlbb_banner_ref_ingest.py" --all --vod-root /root/data/mlbb/youtube_nightly/inbox || true
python3 "$REPO/scripts/mlbb_feedback_pattern_miner.py" --write --print || true

echo "===== MLBB VOD-only mode $(date -Is) ====="
CURRENT_BRANCH="$(cd "$REPO" && git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
DEPLOY_BRANCH="${VPS_BRANCH:-${CURRENT_BRANCH:-cursor/mlbb-dense-savage-scan-6cbd}}"
if [[ -f "$ENV_FILE" ]]; then
  if grep -q '^VPS_BRANCH=' "$ENV_FILE"; then
    sed -i "s|^VPS_BRANCH=.*|VPS_BRANCH=$DEPLOY_BRANCH|" "$ENV_FILE"
  else
    echo "VPS_BRANCH=$DEPLOY_BRANCH" >>"$ENV_FILE"
  fi
fi
echo "Supervisor: 1x mlbb_vod_segment_feed.sh loop | Shorts worker: OFF"
pgrep -af 'mlbb_vod_segment_feed|telegram_upload_bot' || echo "(starting…)"
tail -5 /root/data/mlbb/mlbb_vod_segment_feed.log 2>/dev/null || echo "(log empty yet)"
