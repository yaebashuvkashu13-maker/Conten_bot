# n8n workflows for Conten_bot

Import any JSON file: n8n → **Workflows** → **⋮** → **Import from File**.

## Before import

1. Project on server: `/workspace/Conten_bot` (adjust `PROJECT_DIR` in Execute Command nodes if different).
2. Set environment variables for n8n (docker-compose or systemd):

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=1006141589
PROJECT_DIR=/workspace/Conten_bot
PROXY_URL=socks5://user:pass@host:port
```

3. On server once:

```bash
cd /workspace/Conten_bot
git pull
python3 -m pip install -e .
cp config.instagram-mlbb.yaml config.yaml
# edit config.yaml: cookies, dry_run: false
```

4. n8n must allow **Execute Command** node (self-hosted default).

## Workflows

| File | What it does |
|------|----------------|
| `01_daily_instagram_telegram.json` | Every day 10:00 — new Instagram posts → Telegram |
| `02_montage_hero_telegram.json` | Webhook `POST /webhook/montage` body `{"hero":"gusion"}` — build montage + send |
| `03_manual_server_check.json` | Manual — count mp4, list reports |

## Webhook montage example

```bash
curl -X POST https://YOUR-N8N/webhook/montage \
  -H "Content-Type: application/json" \
  -d '{"hero":"gusion"}'
```

Heroes: `gusion`, `lancelot`, `chou`, `fanny`, `hayabusa`.
