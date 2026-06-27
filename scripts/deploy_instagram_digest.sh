#!/usr/bin/env bash
# Run ON VPS after git pull or scp.
set -Eeuo pipefail
REPO="${1:-/root/content_bot_ml}"
DEST=/usr/local/bin

install -m 755 "$REPO/scripts/build_instagram_config.py" "$DEST/build_instagram_config.py"
install -m 755 "$REPO/scripts/instagram_digest_run.sh" "$DEST/instagram_digest_run.sh"
install -m 755 "$REPO/scripts/instagram_background_worker.py" "$DEST/instagram_background_worker.py"

mkdir -p /root/data/mlbb /var/lock
touch /root/data/mlbb/instagram_digest.log

# 16:00 UTC = 19:00 MSK
CRON_LINE='0 16 * * * root /usr/local/bin/instagram_digest_run.sh # ig-digest-19msk'
if [[ -f /etc/cron.d/mlbb_video ]]; then
  if ! grep -q instagram_digest_run /etc/cron.d/mlbb_video; then
    echo "$CRON_LINE" >> /etc/cron.d/mlbb_video
  fi
else
  echo "$CRON_LINE" > /etc/cron.d/instagram_digest
  chmod 644 /etc/cron.d/instagram_digest
fi

echo "Deployed Instagram digest. Cron: 16:00 UTC = 19:00 MSK"
echo "Test: IG_DIGEST_DRY_RUN=1 /usr/local/bin/instagram_digest_run.sh"
echo "Live: /usr/local/bin/instagram_digest_run.sh"
