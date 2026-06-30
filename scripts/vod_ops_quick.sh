#!/bin/bash
# Quick VOD pipeline ops — health, reset exhausted, audit sample.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/scripts"
PY="${PY:-python3}"

case "${1:-health}" in
  health)
    "$PY" vod_pipeline_health.py "${@:2}"
    ;;
  reset)
    "$PY" reset_vod_inbox_exhausted.py "${@:2}"
    ;;
  audit)
    "$PY" audit_vod_inbox.py --limit 4 "${@:2}"
    ;;
  deploy)
    VPS_BRANCH="${VPS_BRANCH:-cursor/vod-pipeline-base-6cbd}" bash "$ROOT/scripts/vps_apply_vod_only.sh"
    ;;
  requeue)
    "$PY" requeue_inbox_vods.py "${@:2}"
    ;;
  rev)
    grep -m1 VOD_PIPELINE_REV "$ROOT/scripts/vod_game_registry.py" || true
    ;;
  *)
    echo "usage: $0 {health|reset|audit|deploy|rev} [args...]" >&2
    exit 1
    ;;
esac
