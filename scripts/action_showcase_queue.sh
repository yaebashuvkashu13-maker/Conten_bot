#!/usr/bin/env bash
# Start 2x5 showcase with auto-retry until all 10 montages are delivered.
set -Eeuo pipefail

LOG=/root/data/mlbb/action_showcase_2x5.log
STATE=/root/data/mlbb/action_showcase_2x5_state.json

mkdir -p /root/data/mlbb

if [[ "${1:-}" == "--reset" ]]; then
  rm -f "$STATE"
  shift
fi

if pgrep -f 'action_showcase_2x5.py' >/dev/null; then
  echo "[$(date -Is)] showcase already running" >>"$LOG"
  exit 0
fi

/usr/local/bin/stop_competing_workers.sh 2>/dev/null || true
nohup /usr/local/bin/run_job_until_ok.sh "$LOG" \
  python3 /usr/local/bin/action_showcase_2x5.py --resume \
  >>"$LOG" 2>&1 &

echo "[$(date -Is)] showcase queued (auto-retry enabled)" >>"$LOG"
