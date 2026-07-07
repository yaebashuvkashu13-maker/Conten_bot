#!/usr/bin/env bash
# After owner 👍/👎: sync labels, train, rescore queue, optional feed. Single-instance (flock).
set -Eeuo pipefail
LOCK=/tmp/mlbb_learn_apply.lock
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "skip learn_apply another instance running"
  exit 0
fi

set -a
source /root/.video_bot.env
set +a
export CONTENT_BOT_REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
export MLBB_DATA_ROOT="${MLBB_DATA_ROOT:-/root/data/mlbb}"
export PYTHONPATH="/usr/local/bin:${CONTENT_BOT_REPO}/scripts:${PYTHONPATH:-}"
export HIGHLIGHT_HEATMAP=0
export HIGHLIGHT_USE_OWNER_ANCHORS=0
export HIGHLIGHT_OWNER_BAD_PAD_SEC="${HIGHLIGHT_OWNER_BAD_PAD_SEC:-90}"
export HIGHLIGHT_OWNER_GOOD_PAD_SEC="${HIGHLIGHT_OWNER_GOOD_PAD_SEC:-45}"
export MLBB_LEARNING_FIRST="${MLBB_LEARNING_FIRST:-0}"
export MLBB_SEND_ENABLED="${MLBB_SEND_ENABLED:-1}"
export MLBB_USE_CLASSIFIER=1
export HIGHLIGHT_EXEMPLAR_ROOT="${HIGHLIGHT_EXEMPLAR_ROOT:-/root/content_bot_ml/data/highlight_exemplars}"

python3 - <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, "/usr/local/bin")
sys.path.insert(0, str(Path("/root/content_bot_ml") / "scripts"))

from mlbb_calibration_store import (
    backfill_ever_delivered,
    purge_non_mlbb_candidates,
    rebuild_index_from_disk,
    stats as cal_stats,
    sync_owner_learning,
)
from mlbb_owner_learning import backfill_shorts_to_owner_labels
from mlbb_vod_segment_store import backfill_owner_labels_from_vod_segments, stats as vseg_stats

synced_vseg = backfill_owner_labels_from_vod_segments()
synced_shorts = backfill_shorts_to_owner_labels()
synced_delivered = backfill_ever_delivered()
n = rebuild_index_from_disk()
purged = purge_non_mlbb_candidates()
s = cal_stats()
vs = vseg_stats()
print(
    f"owner_backfill vseg={synced_vseg} shorts={synced_shorts} delivered={synced_delivered} "
    f"rebuild_index={n} purged={purged} shorts_pending={s['pending']} owner_rank={s.get('owner_rank')} "
    f"vseg_pending={vs['pending']}"
)
PY

python3 /usr/local/bin/highlight_train.py --profile mobile_legends 2>/dev/null \
  || python3 "${CONTENT_BOT_REPO}/scripts/highlight_train.py" --profile mobile_legends

python3 - <<'PY'
import sys
sys.path.insert(0, "/usr/local/bin")
sys.path.insert(0, "/root/content_bot_ml/scripts")
from mlbb_calibration_store import stats, sync_owner_learning

print("owner_sync", sync_owner_learning())
print("stats", stats())
PY

python3 /usr/local/bin/eval_learning_first_gate.py 2>/dev/null || true

python3 "${CONTENT_BOT_REPO}/scripts/mlbb_feedback_pattern_miner.py" --write --print 2>/dev/null || true
python3 - <<'PY'
import sys
sys.path.insert(0, "/root/content_bot_ml/scripts")
from mlbb_feedback_gate_tune import apply_feedback_gates, clear_patterns_cache
clear_patterns_cache()
print("feedback_gate", apply_feedback_gates(force=True))
PY

if [[ "${MLBB_LEARN_APPLY_FEED:-0}" == "1" ]]; then
  set -a
  # shellcheck disable=SC1091
  source /root/.video_bot.env 2>/dev/null || true
  set +a
  if [[ "${MLBB_VOD_ONLY:-0}" == "1" || "${MLBB_CALIBRATION_FEED_ENABLED:-1}" == "0" ]]; then
    echo "skip calibration_feed: VOD-only"
  else
    /usr/local/bin/mlbb_calibration_feed.sh || true
  fi
fi
echo "mlbb_learn_apply done $(date -Is)"
