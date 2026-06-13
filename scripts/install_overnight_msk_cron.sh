#!/usr/bin/env bash
# 18:00 MSK = 15:00 UTC → montages in Telegram by 08:00 MSK (5 games × 1 cut).
set -Eeuo pipefail

REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
DEST=/usr/local/bin
MARK="# overnight-msk-5games"
CRON_LINE="0 15 * * * root /usr/local/bin/overnight_msk.sh >>/root/data/mlbb/overnight_msk/cron.log 2>&1 $MARK"
WATCHDOG_MARK="# overnight-watchdog"
WATCHDOG_LINE="*/20 * * * * root /usr/local/bin/overnight_watchdog.sh >>/root/data/mlbb/overnight_msk/watchdog.log 2>&1 $WATCHDOG_MARK"

install -m 755 "$REPO/scripts/overnight_msk.sh" "$DEST/overnight_msk.sh"
install -m 755 "$REPO/scripts/overnight_catchup.sh" "$DEST/overnight_catchup.sh"
install -m 755 "$REPO/scripts/overnight_youtube_batch.py" "$DEST/overnight_youtube_batch.py"
install -m 755 "$REPO/scripts/overnight_watchdog.sh" "$DEST/overnight_watchdog.sh" 2>/dev/null || true
install -m 755 "$REPO/scripts/nightly_youtube_montage.py" "$DEST/nightly_youtube_montage.py"
install -m 755 "$REPO/scripts/youtube_download.py" "$DEST/youtube_download.py"
install -m 755 "$REPO/scripts/youtube_game_prefs.py" "$DEST/youtube_game_prefs.py"
install -m 755 "$REPO/scripts/montage_env.py" "$DEST/montage_env.py"
install -m 755 "$REPO/scripts/smart_video_editor.py" "$DEST/smart_video_editor.py"
install -m 755 "$REPO/scripts/stop_competing_workers.sh" "$DEST/stop_competing_workers.sh"
mkdir -p "$REPO/config" /root/data/mlbb/overnight_msk

if [[ -f /etc/cron.d/mlbb_video ]]; then
  grep -v 'overnight-msk-5games' /etc/cron.d/mlbb_video | grep -v 'overnight-watchdog' > /tmp/mlbb_video.cron || true
  cat /tmp/mlbb_video.cron > /etc/cron.d/mlbb_video
  echo "$CRON_LINE" >> /etc/cron.d/mlbb_video
  echo "$WATCHDOG_LINE" >> /etc/cron.d/mlbb_video
else
  {
    echo "$CRON_LINE"
    echo "$WATCHDOG_LINE"
  } > /etc/cron.d/overnight_msk
  chmod 644 /etc/cron.d/overnight_msk
fi

bash "$REPO/scripts/disable_legacy_publish_crons.sh"

echo "OK overnight MSK cron: 15:00 UTC = 18:00 Moscow"
grep -E 'overnight|watchdog' /etc/cron.d/* 2>/dev/null || true
