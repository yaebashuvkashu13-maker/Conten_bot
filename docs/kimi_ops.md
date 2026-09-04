# Kimi ops assistant (Moonshot)

## Setup
1. Get API key at https://platform.moonshot.ai/ (or moonshot.cn).
2. Add to `/root/.video_bot.env`:
   ```
   MOONSHOT_API_KEY=sk-...
   KIMI_MODEL=moonshot-v1-auto
   MOONSHOT_API_BASE=https://api.moonshot.ai/v1
   ```
3. Restart: `systemctl restart telegram-upload-bot`
4. In Telegram (owner): `/kimi что не так с качеством?`

## What Kimi is for
- Ops Q&A, feedback triage, reject explanations
- NOT clip vision / gun / kill detection (that stays in the VOD pipeline)

## CLI
```
python3 scripts/kimi_ops_agent.py --check
python3 scripts/kimi_ops_agent.py --show-brief
python3 scripts/kimi_ops_agent.py "разбери последние дизлайки"
```
