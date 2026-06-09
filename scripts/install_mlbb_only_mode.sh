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
strict_montage_direct.py
nightly_youtube_montage.py
youtube_triple_montage.py
smart_video_editor.py
EOF

for pat in pubg_mlbb_pipeline overnight_youtube_batch overnight_catchup overnight_msk \
  viral_reference_ingest eval_owner_labels score_owner_windows \
  investor_demo_batch action_showcase_2x5 morning_pubg_standoff \
  pubg_gunfire_rebuild pubg_tiktok_batch genshin_boss_rebuild \
  mlbb_showcase_rebuild standoff overnight_orchestrator \
  pubg_brawl_direct pubg_stream_learn youtube_triple_montage \
  nightly_youtube_montage hourly_new_sources; do
  pkill -f "$pat" 2>/dev/null || true
done
pkill -f 'run_job_until_ok.sh /root/data/mlbb/pubg_mlbb' 2>/dev/null || true
pkill -f 'run_job_until_ok.sh /root/data/mlbb/investor_demo' 2>/dev/null || true
pkill -f 'run_job_until_ok.sh /root/data/mlbb/action_showcase' 2>/dev/null || true
rm -f /var/lock/smart_video_editor.lock /var/lock/overnight_msk.lock 2>/dev/null || true

touch "$ENV_FILE"
for kv in MLBB_ONLY_MODE=1 VK_MLBB_DISABLED=1 VK_MLBB_NOTIFY_EMPTY=0; do
  key="${kv%%=*}"
  val="${kv#*=}"
  if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
    sed -i "s/^${key}=.*/${key}=${val}/" "$ENV_FILE"
  else
    echo "${key}=${val}" >>"$ENV_FILE"
  fi
done

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
  "$REPO/scripts/daily_ops_cron.sh" \
  "$REPO/scripts/daily_morning_plan.py" \
  "$REPO/scripts/daily_evening_report.py" \
  "$BIN/" 2>/dev/null || true

bash "$REPO/scripts/disable_vk_mlbb_scheduler.sh" 2>/dev/null || true
bash "$REPO/scripts/install_mlbb_calibration_cron.sh"

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
echo "OK MLBB-only: Shorts ingest + calibration feed only"
