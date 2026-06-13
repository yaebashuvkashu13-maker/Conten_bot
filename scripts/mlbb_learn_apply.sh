#!/usr/bin/env bash
# After owner 👍/👎: sync labels, train, eval gates. NO sendVideo in LEARNING_FIRST until gate pass.
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
export MLBB_LEARNING_FIRST="${MLBB_LEARNING_FIRST:-1}"
export MLBB_LEARN_MIN_PRECISION="${MLBB_LEARN_MIN_PRECISION:-0.40}"

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

python3 /usr/local/bin/eval_learning_first_gate.py
GATE_RC=$?
if [[ "$GATE_RC" -ne 0 ]]; then
  echo "LEARNING_FIRST gate FAIL — sendVideo blocked (metric=precision_7d)" >&2
fi

python3 - <<'PY'
import json
import sys
from pathlib import Path
from mlbb_learning_first import precision_7d, sends_allowed, transition_passed

print(f"transition_passed={transition_passed()} sends_allowed={sends_allowed()} precision_7d={precision_7d():.3f}")
if not sends_allowed():
    print("skip feeds: LEARNING_FIRST gate not passed", file=sys.stderr)
    sys.exit(0)
PY

if python3 - <<'PY'
from mlbb_learning_first import sends_allowed
import sys
sys.exit(0 if sends_allowed() else 1)
PY
then
  if [[ -x /usr/local/bin/mlbb_vod_segment_feed.sh ]]; then
    /usr/local/bin/mlbb_vod_segment_feed.sh
  elif [[ -f "${CONTENT_BOT_REPO}/scripts/mlbb_vod_segment_feed.py" ]]; then
    flock -n /tmp/mlbb_vod_segment_feed.lock \
      python3 "${CONTENT_BOT_REPO}/scripts/mlbb_vod_segment_feed.py" \
      >>/root/data/mlbb/mlbb_vod_segment_feed.log 2>&1 || true
  fi
  /usr/local/bin/mlbb_calibration_feed.sh || true
fi

echo "mlbb_learn_apply done $(date -Is)"
