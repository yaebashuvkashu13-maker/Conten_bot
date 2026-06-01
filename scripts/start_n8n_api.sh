#!/bin/bash
# Paste entire file or run: bash /workspace/Conten_bot/scripts/start_n8n_api.sh
set -e
cd /workspace/Conten_bot 2>/dev/null || cd /workspace/conten_bot 2>/dev/null || cd /workspace/Conten_bot
git pull origin cursor/proxy-dataset-pipeline-6e59 2>/dev/null || git pull || true
python3 -m pip install -e . -q
# TELEGRAM_* must already be exported or in /etc/conten_bot.env
nohup python3 -m content_bot.n8n_server > /tmp/n8n_api.log 2>&1 &
sleep 2
curl -s http://127.0.0.1:8765/health
echo ""
echo "If ok:true — API running. Then run n8n workflow 03."
