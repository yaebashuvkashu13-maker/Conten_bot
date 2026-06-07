#!/usr/bin/env bash
# Pause investor_demo + action_showcase + legacy montage senders.
set -Eeuo pipefail

PAUSE=/root/data/mlbb/PAUSED_PIPELINES
mkdir -p /root/data/mlbb
cat >"$PAUSE" <<'EOF'
investor_demo_batch.py
action_showcase_2x5.py
morning_pubg_standoff_catchup.py
pubg_gunfire_rebuild.py
pubg_tiktok_batch_10.py
genshin_boss_rebuild.py
mlbb_showcase_rebuild.py
overnight_youtube_batch.py
EOF

for pat in investor_demo_batch action_showcase_2x5 morning_pubg_standoff pubg_gunfire_rebuild \
  pubg_tiktok_batch genshin_boss_rebuild mlbb_showcase_rebuild overnight_youtube_batch; do
  pkill -f "$pat" 2>/dev/null || true
done
pkill -f 'run_job_until_ok.sh /root/data/mlbb/investor_demo' 2>/dev/null || true
pkill -f 'run_job_until_ok.sh /root/data/mlbb/action_showcase' 2>/dev/null || true
rm -f /var/lock/smart_video_editor.lock

echo "OK paused legacy pipelines; active queue: pubg_mlbb_pipeline.py"
