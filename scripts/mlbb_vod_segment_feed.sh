#!/usr/bin/env bash
# One VOD feed supervisor — systemd is the only owner.
# flock prevents a second copy; on contention try orphan cleanup then fail
# (Restart=on-failure backs off without burning StartLimit on exit 0 loops).
set -Eeuo pipefail
LOCK=/tmp/mlbb_vod_supervisor.lock
exec 9>"$LOCK"
acquire() { flock -n 9; }
if ! acquire; then
  echo "$(date -Is) lock busy — orphan cleanup" >&2
  while read -r pid; do
    [[ -z "$pid" || "$pid" == "$$" ]] && continue
    kill -TERM "$pid" 2>/dev/null || true
  done < <(pgrep -f 'mlbb_vod_segment_feed\.sh' || true)
  sleep 2
  if ! acquire; then
    echo "$(date -Is) FATAL: cannot acquire supervisor lock" >&2
    exit 1
  fi
fi
REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
export CONTENT_BOT_REPO="$REPO"
export PYTHONPATH="/usr/local/bin:${REPO}/scripts${PYTHONPATH:+:$PYTHONPATH}"
export VOD_PUBG_ONLY="${VOD_PUBG_ONLY:-1}"
export HIGHLIGHT_HEATMAP="${HIGHLIGHT_HEATMAP:-0}"
IDLE_SEC="${MLBB_VOD_IDLE_SEC:-25}"
LOG_DIR="${VOD_FEED_LOG_DIR:-/root/data/mlbb}"
mkdir -p "$LOG_DIR"
RUNNER="$REPO/scripts/daily_cycle_runner.py"
if [[ ! -f "$RUNNER" ]]; then
  RUNNER=/usr/local/bin/daily_cycle_runner.py
fi
while true; do
  python3 -u "$RUNNER" >>"$LOG_DIR/mlbb_vod_segment_feed.log" 2>&1 || true
  sleep "$IDLE_SEC"
done
