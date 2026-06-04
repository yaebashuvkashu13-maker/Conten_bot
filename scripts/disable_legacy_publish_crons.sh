#!/usr/bin/env bash
# Turn off automated Telegram sends except overnight_msk batch.
set -Eeuo pipefail

MARKS=(
  'ig-digest-19msk'
  'yt-proactive-mlbb'
  'youtube-nightly-mlbb'
  'morning-publish'
  'mlbb-evening'
)

for f in /etc/cron.d/*; do
  [[ -f "$f" ]] || continue
  tmp=$(mktemp)
  skip=0
  while IFS= read -r line; do
    drop=0
    for m in "${MARKS[@]}"; do
      if [[ "$line" == *"$m"* ]]; then
        drop=1
        break
      fi
    done
    if [[ "$line" == *instagram_digest* ]] || [[ "$line" == *morning_publish* ]]; then
      drop=1
    fi
    if [[ $drop -eq 0 ]]; then
      echo "$line" >>"$tmp"
    else
      skip=1
    fi
  done <"$f"
  if [[ $skip -eq 1 ]]; then
    mv "$tmp" "$f"
    echo "pruned $f"
  else
    rm -f "$tmp"
  fi
done

TMP=$(mktemp)
if crontab -l 2>/dev/null | grep -v 'nightly_youtube.sh' \
  | grep -v 'morning_publish_reminder' \
  | grep -v 'hourly_new_sources' \
  | grep -v 'mlbb_evening' \
  | grep -v 'youtube_triple_montage' >"$TMP"; then
  crontab "$TMP"
  echo "pruned root crontab"
fi
rm -f "$TMP"

ENV=/root/.video_bot.env
touch "$ENV"
grep -q '^OVERNIGHT_EXCLUSIVE=' "$ENV" || echo 'OVERNIGHT_EXCLUSIVE=1' >>"$ENV"
grep -q '^IG_DIGEST_DRY_RUN=' "$ENV" || echo 'IG_DIGEST_DRY_RUN=1' >>"$ENV"
# Keep Instagram worker config-only; no evening digest spam
if grep -q '^IG_NOTIFY_EMPTY=' "$ENV"; then
  sed -i 's/^IG_NOTIFY_EMPTY=.*/IG_NOTIFY_EMPTY=0/' "$ENV"
else
  echo 'IG_NOTIFY_EMPTY=0' >>"$ENV"
fi

echo "Legacy auto-publish crons disabled. Active: overnight_msk (00:00 MSK)."
