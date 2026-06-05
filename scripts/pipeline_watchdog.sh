#!/usr/bin/env bash
# Restart showcase / genshin rebuild if they died before delivering results.
set -Eeuo pipefail

LOG=/root/data/mlbb/pipeline_watchdog.log
exec >>"$LOG" 2>&1

if pgrep -f 'smart_video_editor.py' >/dev/null; then
  exit 0
fi

restart_if_needed() {
  local name="$1"
  local state_file="$2"
  local total_jobs="$3"
  local script="$4"
  local job_log="$5"

  if [[ ! -f "$state_file" ]]; then
    return 0
  fi
  if pgrep -f "$script" >/dev/null; then
    return 0
  fi

  local need
  need=$(python3 - <<PY 2>/dev/null || echo 0
import json
from pathlib import Path
p = Path("${state_file}")
try:
    d = json.loads(p.read_text())
except Exception:
    print(0)
    raise SystemExit
if d.get("completed"):
    print(0)
    raise SystemExit
jobs = d.get("jobs") or {}
ok = sum(1 for j in jobs.values() if j.get("status") == "ok")
total = int(d.get("total_jobs") or ${total_jobs})
print(1 if ok < total else 0)
PY
)

  if [[ "$need" != "1" ]]; then
    return 0
  fi

  echo "[$(date -Is)] watchdog: restart ${name}"
  /usr/local/bin/stop_competing_workers.sh 2>/dev/null || true
  nohup /usr/local/bin/run_job_until_ok.sh "$job_log" python3 "/usr/local/bin/${script}" --resume \
    >>"$job_log" 2>&1 &
}

restart_if_needed \
  "action_showcase_2x5" \
  "/root/data/mlbb/action_showcase_2x5_state.json" \
  10 \
  "action_showcase_2x5.py" \
  "/root/data/mlbb/action_showcase_2x5.log"

restart_if_needed \
  "genshin_boss_rebuild" \
  "/root/data/mlbb/genshin_boss_rebuild_state.json" \
  1 \
  "genshin_boss_rebuild.py" \
  "/root/data/mlbb/genshin_boss_rebuild.log"
