#!/usr/bin/env bash
# Audit VPS: no montage pipeline should send to Telegram without STRICT_PEAK_MONTAGE.
set -Eeuo pipefail

echo "=== crontab -l ==="
crontab -l 2>/dev/null || echo "(empty)"

echo ""
echo "=== /etc/cron.d ==="
grep -h . /etc/cron.d/* 2>/dev/null | grep -v '^#' | grep -v '^$' || true

echo ""
echo "=== running montage workers ==="
pgrep -af 'investor_demo|action_showcase|morning_pubg|pubg_gunfire|pubg_tiktok|genshin_boss|mlbb_showcase|overnight_youtube|smart_video_editor' || echo "(none)"

echo ""
echo "=== legacy workers without STRICT_PEAK (should be empty) ==="
for pid in $(pgrep -f 'smart_video_editor.py' 2>/dev/null || true); do
  if [[ -r "/proc/$pid/environ" ]]; then
    env_blob=$(tr '\0' '\n' <"/proc/$pid/environ")
    profile=$(echo "$env_blob" | grep -E '^(QUEUE_GAME_PROFILE|DEFAULT_GAME_PROFILE)=' | tail -1 || true)
    strict=$(echo "$env_blob" | grep '^STRICT_PEAK_MONTAGE=' || true)
    if [[ -n "$profile" ]] && [[ "$strict" != "STRICT_PEAK_MONTAGE=1" ]]; then
      echo "pid=$pid $profile strict=${strict:-unset}"
    fi
  fi
done

LEGACY_PATTERNS=(
  'morning_pubg_standoff_catchup.py'
  'pubg_gunfire_rebuild.py'
  'pubg_tiktok_batch'
  'genshin_boss_rebuild.py'
  'mlbb_showcase_rebuild.py'
)
echo ""
echo "=== legacy batch processes (kill if running) ==="
for pat in "${LEGACY_PATTERNS[@]}"; do
  pgrep -af "$pat" || true
done

echo ""
echo "=== investor_demo log tail (ALL_PASS) ==="
tail -30 /root/data/mlbb/investor_demo_batch.log 2>/dev/null | grep -E 'ALL_PASS|ACCEPTANCE|abort|blocked' || echo "(no matches)"
