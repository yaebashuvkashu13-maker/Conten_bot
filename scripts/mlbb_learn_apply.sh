#!/usr/bin/env bash
# After owner 👍/👎: sync labels, train, eval metrics. Sends stay ON (MLBB_SEND_ENABLED=1).
set -Eeuo pipefail
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

python3 - <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, "/usr/local/bin")
sys.path.insert(0, str(Path("/root/content_bot_ml") / "scripts"))

from highlight_scorer import clear_exemplar_cache
from mlbb_calibration_store import rebuild_index_from_disk, stats as cal_stats
from mlbb_vod_segment_store import backfill_owner_labels_from_vod_segments, stats as vseg_stats

synced = backfill_owner_labels_from_vod_segments()
n = rebuild_index_from_disk()
s = cal_stats()
vs = vseg_stats()
clear_exemplar_cache()
print(
    f"owner_backfill={synced} rebuild_index={n} "
    f"shorts_pending={s['pending']} vseg_pending={vs['pending']} "
    f"vseg_labels=👍{vs['good_labels']} 👎{vs['bad_labels']}"
)
PY

python3 /usr/local/bin/highlight_train.py --profile mobile_legends
python3 /usr/local/bin/eval_learning_first_gate.py || true

if [[ -x /usr/local/bin/mlbb_vod_segment_feed.sh ]]; then
  /usr/local/bin/mlbb_vod_segment_feed.sh
elif [[ -f "${CONTENT_BOT_REPO}/scripts/mlbb_vod_segment_feed.py" ]]; then
  flock -n /tmp/mlbb_vod_segment_feed.lock \
    python3 "${CONTENT_BOT_REPO}/scripts/mlbb_vod_segment_feed.py" \
    >>/root/data/mlbb/mlbb_vod_segment_feed.log 2>&1 || true
fi

/usr/local/bin/mlbb_calibration_feed.sh || true
echo "mlbb_learn_apply done $(date -Is)"
