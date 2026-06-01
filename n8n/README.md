# n8n workflows for Conten_bot (HTTP version)

Your n8n **does not support** `Execute Command` node → workflows use **HTTP Request** to a small API on the server.

## Step 1 — start API on server (once)

```bash
cd /workspace/Conten_bot
git pull
python3 -m pip install -e .

export PROJECT_DIR=/workspace/Conten_bot
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=1006141589

# run in background (or systemd — see below)
nohup python3 -m content_bot.n8n_server > /tmp/n8n_api.log 2>&1 &
curl http://127.0.0.1:8765/health
```

Expected: `{"ok": true, "service": "conten_bot_n8n_api"}`

### If n8n runs in Docker

Use host IP instead of 127.0.0.1, e.g. in n8n env:

```env
CONTEN_BOT_API_URL=http://172.17.0.1:8765
```

(or `host.docker.internal` on some setups)

## Step 2 — n8n variables

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=1006141589
CONTEN_BOT_API_URL=http://127.0.0.1:8765
```

## Step 3 — import workflows

Import **each JSON as a separate workflow** (3 files = 3 workflows, not one canvas).

1. `03_manual_server_check.json` — test first
2. `02_montage_hero_telegram.json`
3. `01_daily_instagram_telegram.json`

## API endpoints

| POST path | Action |
|-----------|--------|
| `/check` | Count mp4, list reports |
| `/instagram` | Run content_bot.main |
| `/montage` | Body `{"hero":"gusion"}` |

## systemd (optional, auto-start API)

```ini
[Unit]
Description=Conten_bot n8n API
After=network.target

[Service]
WorkingDirectory=/workspace/Conten_bot
Environment=PROJECT_DIR=/workspace/Conten_bot
EnvironmentFile=/etc/conten_bot.env
ExecStart=/usr/bin/python3 -m content_bot.n8n_server
Restart=always

[Install]
WantedBy=multi-user.target
```

Put tokens in `/etc/conten_bot.env` (chmod 600).
