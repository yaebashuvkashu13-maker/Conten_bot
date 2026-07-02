#!/usr/bin/env bash
# Install on VPS cron (MSK): morning 09:00, evening 21:00
# 0 9 * * * /usr/local/bin/daily_ops_cron.sh morning
# 0 21 * * * /usr/local/bin/daily_ops_cron.sh evening
set -euo pipefail
MODE="${1:-morning}"
if [[ -f /root/.video_bot.env ]]; then set -a; source /root/.video_bot.env; set +a; fi
case "$MODE" in
  morning) python3 /usr/local/bin/daily_morning_plan.py ;;
  evening) python3 /usr/local/bin/daily_evening_report.py ;;
  *) echo "usage: $0 morning|evening"; exit 1 ;;
esac
