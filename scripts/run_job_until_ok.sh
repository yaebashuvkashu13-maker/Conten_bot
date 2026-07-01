#!/usr/bin/env bash
# Retry a command until success or max rounds. Usage:
#   run_job_until_ok.sh /path/to.log command arg1 arg2
set -Eeuo pipefail

LOG="${1:?log file required}"
shift

MAX_ROUNDS="${PIPELINE_RETRY_MAX_ROUNDS:-24}"
SLEEP_SEC="${PIPELINE_RETRY_SLEEP_SEC:-180}"

mkdir -p "$(dirname "$LOG")"

for round in $(seq 1 "$MAX_ROUNDS"); do
  echo "[$(date -Is)] round ${round}/${MAX_ROUNDS}: $*" >>"$LOG"
  if "$@"; then
    echo "[$(date -Is)] success on round ${round}" >>"$LOG"
    exit 0
  fi
  if [[ "$round" -lt "$MAX_ROUNDS" ]]; then
    echo "[$(date -Is)] failed round ${round}, sleep ${SLEEP_SEC}s" >>"$LOG"
    sleep "$SLEEP_SEC"
  fi
done

echo "[$(date -Is)] gave up after ${MAX_ROUNDS} rounds" >>"$LOG"
exit 1
