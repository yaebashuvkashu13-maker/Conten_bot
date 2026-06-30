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
  *)
    echo "usage: $0 {health|reset|audit} [args...]" >&2
    exit 1
    ;;
esac
