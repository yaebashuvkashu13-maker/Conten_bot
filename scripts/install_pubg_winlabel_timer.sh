#!/usr/bin/env bash
# Install periodic 8s window labeling spam (does not touch VOD feed).
set -euo pipefail
REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
install -m 644 "$REPO/deploy/content-bot-pubg-winlabel.service" /etc/systemd/system/
install -m 644 "$REPO/deploy/content-bot-pubg-winlabel.timer" /etc/systemd/system/
# Ensure scripts are importable from PYTHONPATH used by the unit.
cp -f "$REPO/scripts/pubg_window_label_tg.py" /usr/local/bin/ 2>/dev/null || true
cp -f "$REPO/scripts/pubg_window_label_queue.py" /usr/local/bin/ 2>/dev/null || true
cp -f "$REPO/scripts/pubg_ranker_dataset.py" /usr/local/bin/ 2>/dev/null || true
cp -f "$REPO/scripts/pubg_label_reason.py" /usr/local/bin/ 2>/dev/null || true
systemctl daemon-reload
systemctl enable --now content-bot-pubg-winlabel.timer
systemctl restart content-bot-pubg-ranker.timer 2>/dev/null || true
systemctl list-timers 'content-bot-pubg-*' --all || true
echo "winlabel timer installed: ~4 windows every 20 minutes"
