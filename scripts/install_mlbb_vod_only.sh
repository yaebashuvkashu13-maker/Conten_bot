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
  /root/data/mlbb/youtube_nightly/inbox

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
  MLBB_SHORTS_ONLY=0 MLBB_VOD_DISABLED=0 MLBB_VOD_ONLY=1 MLBB_LEARNING_FIRST=0 MLBB_SEND_ENABLED=1 \
  MLBB_MAX_DAILY_SENDS=200 MLBB_CALIBRATION_FEED_ENABLED=0 MLBB_ONE_HEAVY_JOB=1 \
  MLBB_CALIBRATION_LENIENT=0 MLBB_CALIBRATION_FAST_INGEST=0 MLBB_SILVER_BOOTSTRAP=0 \
  MLBB_FEED_RESCORE=0 MLBB_OWNER_EMERGENCY=0 MLBB_OWNER_REQUIRE_SCORE=0 \
  MLBB_INGEST_ALWAYS=0 MLBB_AUTO_TRAIN=0 MLBB_FEED_RE_GATE=0 \
  MLBB_USE_CLASSIFIER=1 MLBB_HERO_SHORTS_MONTAGE=0 MLBB_MONTAGE_COOLDOWN_SEC=86400 \
  MLBB_NUDGE_LOAD_MAX=0 MLBB_FEED_STALE_SEC=7200 MLBB_WORKER_STALE_SEC=7200 \
  MLBB_VOD_BATCH_MAX=0 MLBB_VOD_SEND_ONE=1 \
  MLBB_VOD_INTERVAL_GAP_SEC=3 MLBB_VOD_PIPELINE_MAX_VODS=0 \
  MLBB_VOD_PIPELINE_MAX_MIN=0 MLBB_VOD_IDLE_SEC=8 \
  MLBB_VOD_SCAN_COOLDOWN_SEC=1800 VOD_GAP_BLOCK_COOLDOWN_SEC=900 \
  MLBB_VOD_PROBE_LIMIT=24 MLBB_VOD_SEGMENT_SEC=15 HIGHLIGHT_WINDOW_SEC=15 \
  MLBB_VOD_MIN_SEC=180 MLBB_VOD_MAX_SEC=1200 MLBB_VOD_TARGET_DUR_SEC=780 \
  MLBB_VOD_SKIP_LONG_SEC=1200 MLBB_VOD_MIN_PEAK_SEC=300 \
  MLBB_VOD_SKIP_REVALIDATE=1 \
  MLBB_VOD_SEASON=41 MLBB_VOD_YOUTUBE_DURATION_FILTER=1 \
  MLBB_VOD_SEARCH_FRESH=1 MLBB_VOD_MAX_AGE_DAYS=35 MLBB_VOD_SEARCH_SUPPLEMENT=1 \
  MLBB_VOD_SEARCH_LIMIT=20 MLBB_VOD_SEARCH_BATCH=3 MLBB_VOD_SEARCH_DELAY=6 MLBB_VOD_DOWNLOAD_DELAY=10 \
  MLBB_VOD_BG_WAIT_SEC=180 MLBB_VOD_AUTO_DOWNLOAD=1 MLBB_VOD_FULL_SCAN=1 \
  MLBB_VOD_BOOTSTRAP=0 MLBB_VOD_CHUNK_RENDER=1 MLBB_VOD_SIMPLE_AUDIO=1 \
  MLBB_VOD_GOP_SEC=1 MLBB_VOD_LEAD_SEC=12 MLBB_VOD_VARIABLE_LENGTH=1 \
  MLBB_VOD_ENCODE_CRF=23 MLBB_VOD_ENCODE_AUDIO_K=128 MLBB_VOD_ENCODE_PRESET=fast \
  MLBB_VOD_NO_CROP=1 MLBB_VOD_LANDSCAPE=1 MLBB_VOD_OUT_WIDTH=1280 MLBB_VOD_OUT_HEIGHT=720 \
  MLBB_VOD_OWNER_EXEMPLARS=1 MLBB_VOD_SYNC_OWNER=1 MLBB_VOD_MIN_CLIP_SCORE=0.08 \
  MLBB_FIGHT_MIN_SEC=8 MLBB_FIGHT_MAX_SEC=28 MLBB_FIGHT_HARD_MAX_SEC=32 \
  MLBB_FIGHT_TRIM_LONG=1 MLBB_FIGHT_SUSTAIN_QUIET_BINS=3 \
  MLBB_VOD_KILL_BANNER=1 MLBB_KILL_BANNER_REQUIRED=1 MLBB_KILL_BANNER_MIN_TIER=double \
  MLBB_KILL_BANNER_COLOR_ONLY=0 MLBB_BANNER_COLOR_AS_STREAK=0 MLBB_KILL_BANNER_SCAN_BEFORE=24 MLBB_KILL_BANNER_SCAN_AFTER=12 \
  MLBB_VOD_BANNER_PREFILTER=0 MLBB_VOD_BANNER_PREFILTER_PEAKS=8 MLBB_VOD_BANNER_DISCOVER=1 \
  MLBB_VOD_BANNER_PRESEND=0 MLBB_VOD_BANNER_SKIP_ON_MISS=0 MLBB_VOD_MOTION_ANCHOR_OK=0 \
  MLBB_KILL_BANNER_DISCOVER_STEP=5 MLBB_KILL_BANNER_DISCOVER_MAX_PROBES=12 MLBB_KILL_BANNER_DISCOVER_MAX_SEC=90 \
  MLBB_BANNER_FAST_DISCOVER=1 MLBB_BANNER_FAST_SHIP=1 MLBB_BANNER_FAST_MAX_PROBES=24 MLBB_BANNER_FAST_MAX_SEC=180 \
  MLBB_BANNER_FAST_STEP_SEC=28 MLBB_BANNER_FAST_SHIP_ON_FIRST=0 \
  MLBB_BANNER_FAST_SHIP_MIN_VOD_SEC=480 MLBB_BANNER_FAST_SHIP_MIN_PEAK_SEC=240 \
  MLBB_BANNER_FAST_SHIP_NO_FALLBACK=1 MLBB_VOD_QUALITY_MIN_SEC=480 MLBB_VOD_DISCOVER_MIN_SEC=480 \
  MLBB_VOD_QUALITY_FIRST=1 MLBB_KILL_BANNER_MIN_PRE_SEC=5 MLBB_KILL_BANNER_POST_SEC=3 MLBB_KILL_BANNER_FIGHT_PROBE_LAG=8 \
  MLBB_PRESEND_MIN_BANNER_LEAD=5 MLBB_KILL_BANNER_LEAD_SEC=5 \
  MLBB_VOD_ZERO_STREAK_SOFTEN=1 MLBB_VOD_ADAPTIVE_NOTIFY=0 MLBB_VOD_EXHAUST_NOTIFY=0 \
  SHOOTER_VOD_ZERO_STREAK_SOFTEN=2 SHOOTER_VOD_ADAPTIVE_NOTIFY=0 SHOOTER_VOD_EXHAUST_NOTIFY=0 \
  SHOOTER_VOD_DOWNLOAD_NOTIFY=0 SHOOTER_VOD_DELETE_EXHAUSTED=0 \
  SHOOTER_VOD_SEGMENT_GAP_SEC=45 PUBG_METRO_GATE=0 \
  SHOOTER_VOD_MONTAGE=1 SHOOTER_VOD_MONTAGE_ONLY=1 \
  SHOOTER_VOD_MONTAGE_MIN_CLIPS=3 SHOOTER_VOD_MONTAGE_MAX_CLIPS=3 \
  SHOOTER_VOD_OWNER_ANCHOR_MONTAGE=1 SHOOTER_VOD_OWNER_ANCHOR_SOFT_ALLOW=1 \
  PUBG_VOD_MONTAGE=1 STANDOFF_VOD_MONTAGE=1 WOT_VOD_MONTAGE=1 \
  PUBG_VOD_MONTAGE_ONLY=1 STANDOFF_VOD_MONTAGE_ONLY=1 WOT_VOD_MONTAGE_ONLY=1 \
  SHOOTER_VOD_FAST_PROBE=1 SHOOTER_VOD_PREFER_RUSSIAN=1 SHOOTER_VOD_SKIP_INTELLICLIP=1 \
  SHOOTER_VOD_MAX_PANN_PROBE=24 SHOOTER_VOD_SEED_FROM_FAST_PROBE=1 \
  MLBB_VOD_FAST_PROBE=1 GENSHIN_VOD_FAST_PROBE=1 WOT_VOD_FAST_PROBE=1 \
  MLBB_MOMENT_ANCHOR=banner MLBB_BANNER_ENRICH_ONLY=0 MLBB_BANNER_VISUAL_OK=1 \
  MLBB_BANNER_REF_MATCH=1 MLBB_BANNER_OWNER_REFS=1 \
  MLBB_KILL_BANNER_COLOR_ONLY=0 MLBB_BANNER_COLOR_AS_STREAK=0 \
  MLBB_FAST_PROBE_MODE=banner HIGHLIGHT_MLBB_SKIP_INTRO_SEC=120 \
  MLBB_COMBAT_GATE=1 MLBB_COMBAT_PROBE_MIN=0.40 MLBB_COMBAT_GATE_MIN=0.42 \
  MLBB_VOD_BANNER_DISCOVER=1 MLBB_VOD_BANNER_PREFILTER=0 \
  VOD_ANALYSIS_USE_PROXY=1 VOD_POOL_TTL_SEC=21600 \
  MLBB_TEAMFIGHT_MIN_SCORE=0.45 GENSHIN_BOSS_BAR_MIN_RATIO=0.7 \
  DAILY_GAME_CYCLE_ENABLED=1 \
  DAILY_GAME_STALL_ZERO_RUNS=4 DAILY_GAME_STALL_MAX_SEC=1800 DAILY_CYCLE_RUN_TIMEOUT_SEC=600 \
  DAILY_CYCLE_STALLED_IDLE_SEC=180 DAILY_CYCLE_HOURLY_SLA=1 DAILY_CYCLE_SLA_SEC=3600 \
  SHOOTER_VOD_SKIP_DISCOVERY_WHEN_INBOX_DEAD=1 SHOOTER_VOD_FAST_MONTAGE=1 \
  SHOOTER_VOD_MONTAGE_SHIP_PARTIAL=1 SHOOTER_VOD_MONTAGE_EARLY_SHIP=1 SHOOTER_VOD_MONTAGE_SOFT_MIN_CLIPS=1 \
  SHOOTER_VOD_DENSE_PROBE_STEP_SEC=50 SHOOTER_VOD_DENSE_PROBE_MAX=16 \
  SHOOTER_VOD_DENSE_GUN_MIN=0.045 SHOOTER_VOD_FAST_SKIP_INTRO=60 \
  SHOOTER_VOD_MAX_SEC=3600 SHOOTER_VOD_MIN_SEC=600 SHOOTER_VOD_MONTAGE_MIN_VOD_SEC=600 \
  SHOOTER_VOD_MAX_VODS_PER_RUN=3 HIGHLIGHT_ALLOW_NO_CLIP=1 HIGHLIGHT_CLIP_DISABLED=1 \
  SHOOTER_VOD_MONTAGE_CLIP_RANK=1 SHOOTER_VOD_MONTAGE_CLIP_MAX=6 \
  SHOOTER_VOD_MONTAGE_CLIP_TIMEOUT_SEC=60 SHOOTER_VOD_MONTAGE_CLIP_MIN=0.08 \
  HIGHLIGHT_SCORE_BUDGET_SEC=120 \
  SHOOTER_VOD_MONTAGE_PART_SEC=14 SHOOTER_VOD_MONTAGE_GATE_CORE_SEC=10 \
  SHOOTER_VOD_MONTAGE_CORE_PAD_SEC=2   SHOOTER_VOD_RECYCLE_MAX_PER_VOD=2 SHOOTER_VOD_RECYCLE_COOLDOWN_SEC=900 \
  SHOOTER_VOD_DISCOVERY_PAUSE_SEC=900 SHOOTER_VOD_ALLOW_HIGHLIGHT_FALLBACK=0 \
  VOD_ZERO_SEND_COOLDOWN_SEC=600 MLBB_VOD_DISCOVER_MIN_SEC=480 \
  MLBB_VOD_REOPEN_SOFT_EXHAUSTED=1 \
  PUBG_PANNS_TRUST_QUALITY_FLOOR=0.40 PUBG_PANNS_LOOT_OVERRIDE_MIN=0.40 \
  SHOOTER_AUTHOR_KILL_GATE=1 SHOOTER_REJECT_AUTHOR_DEATH=1 SHOOTER_REQUIRE_AUTHOR_KILL=1 \
  DAILY_GAME_MLBB_QUOTA=5 DAILY_GAME_PUBG_QUOTA=3 DAILY_GAME_STANDOFF_QUOTA=3 \
  DAILY_GAME_GENSHIN_QUOTA=5 DAILY_GAME_WOT_QUOTA=3 \
  MLBB_VOD_MAX_PER_VOD=5 MLBB_VOD_SOFT_MAX_PEAK_TRIES=12 MLBB_VOD_STRICT_PEAK_TRIES=4 \
  MLBB_VOD_SEGMENT_GAP_SEC=120 MLBB_VOD_LENIENT_UNIFORM=1 MLBB_VOD_TAIL_MIN_HUD_RATE=0.40 \
  MLBB_FORCE_RERENDER=1 MLBB_PRESEND_FREEZE_MIN_DUR=1.2 MLBB_PRESEND_FREEZE_MAX_START=3.0 \
  MLBB_PRESEND_MIN_MOTION=0.012 MLBB_PRESEND_MIN_MINIMAP_DELTA=0.010 \
  HIGHLIGHT_USE_OWNER_ANCHORS=0 HIGHLIGHT_CLIP_DISABLED=1 HIGHLIGHT_COLD_START=1 \
  HIGHLIGHT_MAX_PANN_PROBE=5 HIGHLIGHT_MAX_STAGE1=16 HIGHLIGHT_HEATMAP=0 \
  HIGHLIGHT_PARALLEL_WORKERS=2 SMART_FFMPEG_THREADS=2 \
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

# Legacy keys DAILY_*_QUOTA=10 overwrote user DAILY_GAME_* settings — remove them.
for key in DAILY_MLBB_QUOTA DAILY_PUBG_QUOTA DAILY_STANDOFF_QUOTA DAILY_GENSHIN_QUOTA DAILY_WOT_QUOTA; do
  sed -i "/^${key}=/d" "$ENV_FILE" 2>/dev/null || true
done

# EU split-server: MLBB on server 1, PUBG/Standoff on server 2 — keep MLBB quota at 0.
if [[ -f /root/data/mlbb/EU_PUBG_ONLY ]]; then
  sed -i 's/^DAILY_GAME_MLBB_QUOTA=.*/DAILY_GAME_MLBB_QUOTA=0/' "$ENV_FILE"
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
  "$REPO/scripts/mlbb_banner_ref_match.py" \
  "$REPO/scripts/mlbb_banner_pov_match.py" \
  "$REPO/scripts/mlbb_combat_moment.py" \
  "$REPO/scripts/mlbb_fight_segment.py" \
  "$REPO/scripts/mlbb_vod_adaptive_gate.py" \
  "$REPO/scripts/audit_mlbb_vod_inbox.py" \
  "$REPO/scripts/mlbb_pipeline_mode.py" \
  "$REPO/scripts/mlbb_calibration_store.py" \
  "$REPO/scripts/mlbb_calibration_feed.py" \
  "$REPO/scripts/mlbb_youtube_shorts_ingest.py" \
  "$REPO/scripts/mlbb_continuous_worker.py" \
  "$REPO/scripts/mlbb_learning_first.py" \
  "$REPO/scripts/highlight_scorer.py" \
  "$REPO/scripts/gameplay_gate.py" \
  "$REPO/scripts/pubg_shooting_gate.py" \
  "$REPO/scripts/pubg_combat_gate.py" \
  "$REPO/scripts/shooter_author_kill_gate.py" \
  "$REPO/scripts/shooter_vod_segment_feed.py" \
  "$REPO/scripts/vod_peak_gap.py" \
  "$REPO/scripts/calibration_dislike_reasons.py" \
  "$REPO/scripts/vod_state_io.py" \
  "$REPO/scripts/vod_env.py" \
  "$REPO/scripts/telegram_access.py" \
  "$REPO/scripts/extended_vod_fast_scan.py" \
  "$REPO/scripts/vod_owner_learning.py" \
  "$REPO/scripts/strict_montage_direct.py" \
  "$REPO/scripts/mlbb_continuous_worker_watchdog.sh" \
  "$REPO/scripts/mlbb_job_watchdog.py" \
  "$REPO/scripts/mlbb_vod_health_watchdog.sh" \
  "$REPO/scripts/cycle_stall_watchdog.sh" \
  "$REPO/scripts/daily_cycle_runner.py" \
  "$REPO/scripts/daily_game_cycle.py" \
  "$REPO/scripts/mlbb_vod_only_verify.sh" \
  "$REPO/scripts/vps_apply_vod_only.sh" \
  "$REPO/scripts/mlbb_telegram_video.py" \
  "$REPO/scripts/mlbb_runtime_cleanup.py" \
  "$REPO/scripts/telegram_upload_bot.py" \
  "$REPO/scripts/social_publish.py" \
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
export HIGHLIGHT_USE_OWNER_ANCHORS=0
export VOD_CALIBRATION_SEND_AS_FILE=0
export MLBB_VOD_AUTO_HQ_ON_YES=0
export SHOOTER_VOD_AUTO_HQ_ON_YES=0
export HIGHLIGHT_CLIP_DISABLED=1
export HIGHLIGHT_ALLOW_NO_CLIP=1
export HIGHLIGHT_HEATMAP=0
export MLBB_VOD_SEND_ONE=1
export MLBB_VOD_BATCH_MAX=0
export MLBB_VOD_MAX_PER_VOD=0
export MLBB_VOD_INTERVAL_GAP_SEC=3
export MLBB_VOD_PIPELINE_MAX_VODS=0
export MLBB_VOD_PIPELINE_MAX_MIN=0
export MLBB_VOD_PROBE_LIMIT=24
export MLBB_VOD_SEGMENT_GAP_SEC=75
export MLBB_FIGHT_MAX_SEC=55
export MLBB_FIGHT_HARD_MAX_SEC=65
export MLBB_FIGHT_TRIM_LONG=0
export HIGHLIGHT_MAX_PANN_PROBE=12
export HIGHLIGHT_MAX_STAGE1=10
export HIGHLIGHT_PARALLEL_WORKERS=2
export SMART_FFMPEG_THREADS=2
export MLBB_VOD_MIN_PEAK_SEC=300
export MLBB_VOD_SKIP_REVALIDATE=1
# Fast PANNs discovery + CLIP final rank (budgeted) — speed AND quality
export DAILY_CYCLE_RUN_TIMEOUT_SEC=600
export SHOOTER_VOD_QUALITY_FIRST=1
export SHOOTER_VOD_MONTAGE_SOFT_GATE=0
export SHOOTER_VOD_FAST_MONTAGE=1
export SHOOTER_VOD_FAST_PROBE=1
export SHOOTER_VOD_DENSE_PROBE_STEP_SEC=50
export SHOOTER_VOD_DENSE_PROBE_MAX=16
export SHOOTER_VOD_DENSE_GUN_MIN=0.045
export SHOOTER_VOD_FAST_SKIP_INTRO=60
export SHOOTER_VOD_ALLOW_HIGHLIGHT_FALLBACK=0
export SHOOTER_VOD_MONTAGE_PART_SEC=14
export SHOOTER_VOD_MONTAGE_GATE_CORE_SEC=10
export SHOOTER_VOD_MONTAGE_SHIP_PARTIAL=1
export SHOOTER_VOD_MONTAGE_EARLY_SHIP=1
export SHOOTER_VOD_MONTAGE_SOFT_MIN_CLIPS=1
export SHOOTER_VOD_MONTAGE_CLIP_RANK=1
export SHOOTER_VOD_MONTAGE_CLIP_MAX=6
export SHOOTER_VOD_MONTAGE_CLIP_TIMEOUT_SEC=60
export SHOOTER_VOD_MONTAGE_CLIP_MIN=0.08
export HIGHLIGHT_SCORE_BUDGET_SEC=120
export SHOOTER_VOD_RECYCLE_MAX_PER_VOD=1
export SHOOTER_VOD_DISCOVERY_PAUSE_SEC=900
export SHOOTER_VOD_MAX_SEC=14400
export SHOOTER_VOD_MIN_SEC=180
export SHOOTER_VOD_MONTAGE_MIN_VOD_SEC=240
export PUBG_METRO_GATE=0
export SHOOTER_VOD_MAX_VODS_PER_RUN=3
export SHOOTER_VOD_SKIP_DISCOVERY_WHEN_INBOX_DEAD=1
export SHOOTER_VOD_SKIP_INTELLICLIP=1
export YOUTUBE_DOWNLOAD_TIMEOUT=600
export DAILY_GAME_STALL_ZERO_RUNS=4
export DAILY_GAME_STALL_MAX_SEC=1800
export STALL_PROC_MAX_SEC=780
export DAILY_CYCLE_HOURLY_SLA=1
export MLBB_VOD_IDLE_SEC=8
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export LOGO_FILE=/nonexistent/mlbb_calibration_no_logo.png
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
IDLE_SEC="${MLBB_VOD_IDLE_SEC:-8}"
while true; do
  python3 -u /usr/local/bin/daily_cycle_runner.py \
    >>/root/data/mlbb/mlbb_vod_segment_feed.log 2>&1 || true
  sleep "$IDLE_SEC"
done
EOF
chmod 755 "$WRAPPER_VOD"

# Force anti-hang / idle defaults (must override any leftover MLBB_VOD_IDLE_SEC=25).
for kv in \
  MLBB_VOD_IDLE_SEC=8 \
  SHOOTER_VOD_ALLOW_HIGHLIGHT_FALLBACK=0 \
  SHOOTER_VOD_DENSE_PROBE_STEP_SEC=50 \
  SHOOTER_VOD_DENSE_PROBE_MAX=16 \
  SHOOTER_VOD_DENSE_GUN_MIN=0.045 \
  SHOOTER_VOD_FAST_SKIP_INTRO=60 \
  SHOOTER_VOD_FAST_MONTAGE=1 \
  SHOOTER_VOD_MONTAGE_PART_SEC=14 \
  SHOOTER_VOD_MONTAGE_SHIP_PARTIAL=1 \
  SHOOTER_VOD_MONTAGE_EARLY_SHIP=1 \
  SHOOTER_VOD_MONTAGE_SOFT_MIN_CLIPS=1 \
  DAILY_CYCLE_RUN_TIMEOUT_SEC=600 \
  SHOOTER_VOD_MAX_SEC=14400 \
  PUBG_METRO_GATE=0 \
  SHOOTER_VOD_RECYCLE_MAX_PER_VOD=1 \
  SHOOTER_VOD_DISCOVERY_PAUSE_SEC=900 \
  PUBG_PANNS_TRUST_QUALITY_FLOOR=0.40 \
  SHOOTER_AUTHOR_KILL_GATE=1 \
  SHOOTER_REJECT_AUTHOR_DEATH=1 \
  SHOOTER_REQUIRE_AUTHOR_KILL=1 \
  HIGHLIGHT_CLIP_DISABLED=1 \
  SHOOTER_VOD_MONTAGE_CLIP_RANK=1 \
  SHOOTER_VOD_MONTAGE_CLIP_MAX=6 \
  SHOOTER_VOD_MONTAGE_CLIP_TIMEOUT_SEC=60 \
  SHOOTER_VOD_MONTAGE_CLIP_MIN=0.08 \
  HIGHLIGHT_SCORE_BUDGET_SEC=120 \
  VOD_CALIBRATION_SEND_AS_FILE=0 \
  MLBB_VOD_AUTO_HQ_ON_YES=0 \
  SHOOTER_VOD_AUTO_HQ_ON_YES=0; do
  key="${kv%%=*}"
  val="${kv#*=}"
  if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
  else
    echo "${key}=${val}" >>"$ENV_FILE"
  fi
done

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
echo "*/5 * * * * $BIN/cycle_stall_watchdog.sh >>/root/data/mlbb/cycle_stall_watchdog.log 2>&1 $MARK stall" >>"$TMP"
echo "*/15 * * * * $REPO/scripts/vps_apply_vod_only.sh >>/root/data/mlbb/vps_apply_vod.log 2>&1 $MARK auto-apply" >>"$TMP"
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

echo "===== MLBB VOD-only mode $(date -Is) ====="
echo "Supervisor: 1x mlbb_vod_segment_feed.sh loop | Shorts worker: OFF"
pgrep -af 'mlbb_vod_segment_feed|telegram_upload_bot' || echo "(starting…)"
tail -5 /root/data/mlbb/mlbb_vod_segment_feed.log 2>/dev/null || echo "(log empty yet)"
