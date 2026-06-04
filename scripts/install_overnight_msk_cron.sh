#!/usr/bin/env bash
# Midnight MSK = 21:00 UTC. Disable legacy auto-send crons.
set -Eeuo pipefail

REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
DEST=/usr/local/bin
MARK="# overnight-msk-5games"
CRON_LINE="0 21 * * * root /usr/local/bin/overnight_msk.sh >>/root/data/mlbb/overnight_msk/cron.log 2>&1 $MARK"

install -m 755 "$REPO/scripts/overnight_msk.sh" "$DEST/overnight_msk.sh"
install -m 755 "$REPO/scripts/overnight_youtube_batch.py" "$DEST/overnight_youtube_batch.py"
mkdir -p "$REPO/config"
# Config lives in repo (git pull); no separate install path needed.

mkdir -p /root/data/mlbb/overnight_msk

# Install primary cron
if [[ -f /etc/cron.d/mlbb_video ]]; then
  grep -v 'overnight-msk-5games' /etc/cron.d/mlbb_video > /tmp/mlbb_video.cron || true
  grep -v 'overnight_msk' /tmp/mlbb_video.cron > /tmp/mlbb_video.cron2 || true
  cat /tmp/mlbb_video.cron2 > /etc/cron.d/mlbb_video
  echo "$CRON_LINE" >> /etc/cron.d/mlbb_video
else
  echo "$CRON_LINE" > /etc/cron.d/overnight_msk
  chmod 644 /etc/cron.d/overnight_msk
fi

# Disable legacy notification / duplicate YouTube pipelines
bash "$REPO/scripts/disable_legacy_publish_crons.sh"

echo "OK overnight MSK cron: 21:00 UTC = 00:00 Moscow"
grep overnight /etc/cron.d/* 2>/dev/null || true
