#!/usr/bin/env bash
# After owner 👍/👎: rebuild Shorts index, train MLBB classifier, push next calibration batch.
set -Eeuo pipefail
set -a
source /root/.video_bot.env
set +a
export CONTENT_BOT_REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
export MLBB_DATA_ROOT="${MLBB_DATA_ROOT:-/root/data/mlbb}"
export HIGHLIGHT_HEATMAP=0
export HIGHLIGHT_USE_OWNER_ANCHORS=0

python3 - <<'PY'
from mlbb_calibration_store import rebuild_index_from_disk, stats
n = rebuild_index_from_disk()
s = stats()
print(f"rebuild_index_from_disk={n} pending={s['pending']} good_ex={s['good_exemplars']} bad_ex={s['bad_exemplars']}")
PY

python3 /usr/local/bin/highlight_train.py --profile mobile_legends || true
/usr/local/bin/mlbb_calibration_feed.sh || true
echo "mlbb_learn_apply done $(date -Is)"
