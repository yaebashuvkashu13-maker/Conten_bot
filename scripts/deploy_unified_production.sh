#!/usr/bin/env bash
# Deploy the unified production branch onto the VPS hang-fix tree safely.
# Usage: CONTENT_BOT_REPO=/root/content_bot_ml ./scripts/deploy_unified_production.sh
set -euo pipefail
REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
BRANCH="${UNIFIED_BRANCH:-cursor/vod-unified-production-a016}"
cd "$REPO"
git fetch origin "$BRANCH"
# Stay on unified branch; never checkout slim feed branches.
git checkout -B "$BRANCH" "origin/$BRANCH"
# Mirror hot scripts into /usr/local/bin for PYTHONPATH consumers.
for f in \
  clip_hook_gate.py dislike_reason_gates.py vod_cheap_cascade.py telegram_delivery.py \
  vod_media_cache.py vod_clip_quality_ledger.py vod_weekly_quality_report.py \
  vod_inbox_recover.py vod_owner_feedback_bridge.py vod_send_drought_watch.py \
  game_adaptive_thresholds.py vod_hang_detector.py vod_force_send.py \
  smart_video_editor.py shooter_vod_segment_feed.py; do
  [[ -f "scripts/$f" ]] && cp -f "scripts/$f" "/usr/local/bin/$f"
done
bash scripts/install_vod_weekly_quality_report.sh || true
bash scripts/install_vod_send_drought_watch.sh || true
# Pin safe defaults in env
python3 - <<'PY'
from pathlib import Path
p = Path("/root/.video_bot.env")
wanted = {
    "VOD_FORCE_PRESEND_BYPASS": "0",
    "VOD_FORCE_SKIP_DISCOVERY": "0",
    "CLIP_HOOK_GATE": "1",
    "DISLIKE_REASON_GATES": "1",
    "VOD_CHEAP_CASCADE": "1",
    "VOD_AUDIO_PREFLIGHT": "1",
    "TELEGRAM_ENCODE": "1",
    "VOD_QUALITY_LEDGER": "1",
    "VOD_DROUGHT_AUTO_RECOVER": "1",
    "VOD_DROUGHT_HOURS": "2",
}
text = p.read_text() if p.exists() else ""
lines = text.splitlines(); keys=set(); out=[]
for line in lines:
    if not line or line.lstrip().startswith("#") or "=" not in line:
        out.append(line); continue
    k = line.split("=", 1)[0].strip()
    if k in wanted:
        out.append(f"{k}={wanted[k]}"); keys.add(k)
    else:
        out.append(line)
for k,v in wanted.items():
    if k not in keys:
        out.append(f"{k}={v}")
p.write_text("\n".join(out) + "\n")
print("env pinned", sorted(wanted))
PY
pkill -f 'shooter_vod_segment_feed.py' || true
sleep 8
pgrep -af 'shooter_vod_segment_feed.py' || echo "waiting for supervisor to relaunch feed"
echo "deployed $BRANCH @ $(git rev-parse --short HEAD)"
