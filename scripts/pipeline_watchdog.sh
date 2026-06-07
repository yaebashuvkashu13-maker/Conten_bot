#!/usr/bin/env bash
# Restart pubg_mlbb strict queue only.
set -Eeuo pipefail

LOG=/root/data/mlbb/pipeline_watchdog.log
PAUSE=/root/data/mlbb/PAUSED_PIPELINES
exec >>"$LOG" 2>&1

if [[ -f "$PAUSE" ]] && grep -q 'pubg_mlbb_pipeline.py' "$PAUSE" 2>/dev/null; then
  echo "[$(date -Is)] watchdog: pubg_mlbb paused"
  exit 0
fi

if pgrep -f 'smart_video_editor.py' >/dev/null; then
  exit 0
fi

restart_if_needed() {
  local name="$1"
  local state_file="$2"
  local total_jobs="$3"
  local script="$4"
  local job_log="$5"
  local extra_args="${6:---resume}"

  if [[ -f "$PAUSE" ]] && grep -q "$script" "$PAUSE" 2>/dev/null; then
    return 0
  fi
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
  nohup /usr/local/bin/run_job_until_ok.sh "$job_log" python3 "/usr/local/bin/${script}" ${extra_args} \
    >>"$job_log" 2>&1 &
}

restart_if_needed \
  "pubg_mlbb_pipeline" \
  "/root/data/mlbb/pubg_mlbb_pipeline_state.json" \
  2 \
  "pubg_mlbb_pipeline.py" \
  "/root/data/mlbb/pubg_mlbb_pipeline.log" \
  "--resume"
