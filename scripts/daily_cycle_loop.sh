#!/usr/bin/env bash
# Persistent daily game cycle — runs until stopped (no fixed 05:00 cutoff).
set -euo pipefail
set -a
# shellcheck disable=SC1091
source /root/.video_bot.env
set +a

export PYTHONPATH="${CONTENT_BOT_REPO:-/root/content_bot_ml}/scripts:${PYTHONPATH:-}"
export SHOOTER_VOD_QUALITY_FIRST="${SHOOTER_VOD_QUALITY_FIRST:-1}"
export MLBB_VOD_QUALITY_FIRST="${MLBB_VOD_QUALITY_FIRST:-1}"
export SHOOTER_VOD_MONTAGE_SOFT_GATE="${SHOOTER_VOD_MONTAGE_SOFT_GATE:-0}"
export MLBB_KILL_BANNER_MIN_PRE_SEC="${MLBB_KILL_BANNER_MIN_PRE_SEC:-12}"
export MLBB_KILL_BANNER_FIGHT_PROBE_LAG="${MLBB_KILL_BANNER_FIGHT_PROBE_LAG:-12}"
export MLBB_PRESEND_MIN_BANNER_LEAD="${MLBB_PRESEND_MIN_BANNER_LEAD:-12}"
export MLBB_VOD_LEAD_SEC="${MLBB_VOD_LEAD_SEC:-12}"
export MLBB_KILL_BANNER_LEAD_SEC="${MLBB_KILL_BANNER_LEAD_SEC:-12}"
export MLBB_BANNER_FAST_SHIP="${MLBB_BANNER_FAST_SHIP:-0}"
export DAILY_GAME_STALL_ZERO_RUNS="${DAILY_GAME_STALL_ZERO_RUNS:-4}"
export DAILY_GAME_STALL_MAX_SEC="${DAILY_GAME_STALL_MAX_SEC:-1200}"
export DAILY_CYCLE_RUN_TIMEOUT_SEC="${DAILY_CYCLE_RUN_TIMEOUT_SEC:-1800}"
export SHOOTER_VOD_SKIP_DISCOVERY_WHEN_INBOX_DEAD="${SHOOTER_VOD_SKIP_DISCOVERY_WHEN_INBOX_DEAD:-1}"

cd "${CONTENT_BOT_REPO:-/root/content_bot_ml}"
LOG="${DAILY_CYCLE_LOOP_LOG:-/root/data/mlbb/daily_cycle_loop.log}"
mkdir -p "$(dirname "$LOG")"

while true; do
  echo "$(date -u -Is) daily_cycle_runner start" >>"$LOG"
  timeout "${DAILY_CYCLE_RUN_TIMEOUT_SEC:-3600}" python3 -u scripts/daily_cycle_runner.py >>"$LOG" 2>&1 || true
  python3 - <<'PY' >>"$LOG" 2>&1 || true
import json
from pathlib import Path
p = Path("/root/data/mlbb/daily_game_cycle.json")
print(p.read_text() if p.exists() else "no cycle state")
PY
  sleep "${DAILY_CYCLE_LOOP_SLEEP_SEC:-20}"
done
