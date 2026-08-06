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
