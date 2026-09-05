#!/usr/bin/env bash
# Restart shooter VOD feed workers when heartbeat files go stale.
set -euo pipefail
GUARD_DIR="${VOD_WORKER_GUARD_DIR:-/root/data/vod_worker_guard}"
MAX_AGE_SEC="${VOD_HEARTBEAT_MAX_AGE_SEC:-900}"
GAME="${1:-pubg}"
SERVICE="${2:-content-bot-${GAME}-vod-feed.service}"
HB="${GUARD_DIR}/${GAME}_feed.heartbeat"
mkdir -p "$GUARD_DIR"
now=$(date +%s)
if [[ ! -f "$HB" ]]; then
  echo "watchdog: missing heartbeat $HB — restart $SERVICE"
  systemctl restart "$SERVICE" || true
  exit 0
fi
ts=$(python3 - "$HB" <<'PY'
import json, sys, pathlib
p = pathlib.Path(sys.argv[1])
try:
    print(float(json.loads(p.read_text()).get("ts") or 0))
except Exception:
    print(p.stat().st_mtime)
PY
)
age=$(python3 -c "print(int($now - float('$ts')))")
if (( age > MAX_AGE_SEC )); then
  echo "watchdog: stale heartbeat age=${age}s > ${MAX_AGE_SEC}s — restart $SERVICE"
  systemctl restart "$SERVICE" || true
else
  echo "watchdog: ok game=$GAME age=${age}s"
fi
