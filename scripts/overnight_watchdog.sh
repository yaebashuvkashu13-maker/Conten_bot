#!/usr/bin/env bash
# Restart catch-up if batch died with incomplete games (daytime safety net).
set -Eeuo pipefail

STATE=/root/data/mlbb/overnight_msk/state.json
LOG=/root/data/mlbb/overnight_msk/watchdog.log
exec >>"$LOG" 2>&1

if [[ -f /root/.video_bot.env ]]; then set -a; source /root/.video_bot.env; set +a; fi
if [[ "${MLBB_ONLY_MODE:-0}" == "1" ]]; then
  echo "[$(date -Is)] SKIP overnight_watchdog: MLBB_ONLY_MODE=1"
  exit 0
fi

if pgrep -f 'overnight_youtube_batch.py' >/dev/null; then
  exit 0
fi
if pgrep -f 'smart_video_editor.py' >/dev/null; then
  exit 0
fi

need=0
if [[ -f "$STATE" ]]; then
  need=$(python3 - <<'PY' 2>/dev/null || echo 1
import json
from pathlib import Path
p = Path("/root/data/mlbb/overnight_msk/state.json")
d = json.loads(p.read_text())
gs = d.get("game_status") or {}
done = sum(1 for g in gs.values() if g.get("status") == "ok" and int(g.get("montages_ok") or 0) > 0)
print(0 if done >= 5 else 1)
PY
)
fi

if [[ "$need" != "1" ]]; then
  exit 0
fi

echo "[$(date -Is)] watchdog: incomplete batch, starting catch-up"
/usr/local/bin/stop_competing_workers.sh 2>/dev/null || true
nohup /usr/local/bin/overnight_catchup.sh >/dev/null 2>&1 &
