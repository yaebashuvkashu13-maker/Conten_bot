#!/usr/bin/env bash
# Batch retrain: exemplar sync + highlight classifier from owner labels.
set -euo pipefail

REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
BIN="${MLBB_SCRIPTS_BIN:-/usr/local/bin}"
DATA="${MLBB_DATA_ROOT:-/root/data/mlbb}"
LOCK="${DATA}/mlbb_learn_apply.lock"
PY="${PYTHON:-python3}"
MAX_SEC="${MLBB_LEARN_APPLY_MAX_SEC:-600}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
train_py="${BIN}/mlbb_train_classifier.py"
learning_py="${BIN}/mlbb_learning_first.py"
[[ -f "$train_py" ]] || train_py="${script_dir}/mlbb_train_classifier.py"
[[ -f "$learning_py" ]] || learning_py="${script_dir}/mlbb_learning_first.py"

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "learn_apply: already running"
  exit 0
fi

echo "learn_apply: start $(date -Is)"
start_ts=$(date +%s)

mark_start() {
  "$PY" -c "
import importlib.util, sys
path = sys.argv[1]
spec = importlib.util.spec_from_file_location('mlf', path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.mark_retrain_started()
" "$learning_py" || true
}

mark_done() {
  local ok="$1"
  "$PY" -c "
import importlib.util, sys
path, ok = sys.argv[1], sys.argv[2] == '1'
spec = importlib.util.spec_from_file_location('mlf', path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.mark_retrain_finished(ok=ok)
" "$learning_py" "$ok" || true
}

mark_start
trap 'mark_done 0' EXIT

if [[ -f "${REPO}/.video_bot.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${REPO}/.video_bot.env" 2>/dev/null || true
  set +a
fi
if [[ -f /root/.video_bot.env ]]; then
  set -a
  # shellcheck disable=SC1091
  source /root/.video_bot.env
  set +a
fi

export CONTENT_BOT_REPO="$REPO"
export MLBB_DATA_ROOT="$DATA"

timeout_sec() {
  local limit="$1"
  shift
  timeout --preserve-status "$limit" "$@" || {
    code=$?
    if [[ "$code" -eq 124 ]]; then
      echo "learn_apply: timeout after ${limit}s"
      return 124
    fi
    return "$code"
  }
}

rc=0
if ! timeout_sec "$MAX_SEC" "$PY" "$train_py"; then
  rc=1
fi

elapsed=$(( $(date +%s) - start_ts ))
echo "learn_apply: done rc=$rc elapsed=${elapsed}s"
exit "$rc"
