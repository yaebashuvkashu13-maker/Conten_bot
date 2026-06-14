# MLBB Content Farm

24/7 pipeline: YouTube MLBB VODs → 7–22s kill-UI clips → Telegram 👍/👎 calibration.

## Architecture

```
mlbb_continuous_worker (24/7)
├── mlbb_youtube_shorts_ingest   → Shorts calibration queue
├── mlbb_vod_segment_feed        → VOD kill-first scan → render → presend → send
├── mlbb_calibration_feed        → Send Shorts batches to owner
└── mlbb_continuous_worker_watchdog.sh (cron */5)

telegram_upload_bot.py           → 👍/👎 callbacks (uses mlbb_telegram_handlers)
```

## Server paths

| Path | Purpose |
|------|---------|
| `/root/.video_bot.env` | Secrets (TG_BOT_TOKEN, TG_CHAT_ID) |
| `/root/data/mlbb/` | State, labels, indexes |
| `/root/datasets/mlbb/vod_segments/` | Rendered VOD clips |
| `/root/datasets/mlbb/youtube_shorts/` | Downloaded Shorts |
| `/root/data/mlbb/youtube_nightly/inbox/` | MLBB VOD inbox |

## Deploy

```bash
cd /root/content_bot_ml
git pull
bash deploy/mlbb_deploy.sh
```

Cron (watchdog every 5 min):

```cron
*/5 * * * * /usr/local/bin/mlbb_continuous_worker_watchdog.sh
```

## Manual commands

```bash
# Force VOD batch (kill UI peaks)
MLBB_FORCE_BATCH_COUNT=8 python3 /usr/local/bin/mlbb_force_send_batch.py

# Daily status to Telegram
python3 /usr/local/bin/mlbb_daily_report.py --telegram

# Run tests
cd /root/content_bot_ml && python3 -m pytest tests/ -q
```

## Key env vars

See `config/mlbb.env.example`.

Critical:
- `MLBB_VOD_KILL_FIRST=1` — fast kill-UI scan (default)
- `MLBB_VOD_FULL_FRAME=1` — keep skill buttons visible
- `MLBB_FORCE_MAX_LIVE_VOD_SEC=2700` — block long 60fps live VODs
- `MLBB_SEND_ENABLED=1` — master send switch

## Quality gates

1. **MLBB identity gate** (always on) — minimap HUD + wrong-game block
2. **Activity gate** (always on) — static image + music slides
3. **Gameplay gate** (always on) — hero spawn preview, showcase, lobby/draft
4. **Verify gate** (always on) — MLBB scorer + HUD + exemplar match (blocks other MOBAs)
5. Kill UI detector + OCR keywords
2. Variable fight length 7–22s
3. Presend: freeze detect + motion + spawn check
4. Owner 👎 → zone blocked in `mobile_legends_owner_labels.json`

## Telegram callbacks

Button formats:
- Shorts: `mlbb_yes:{video_id}` / `mlbb_no:{video_id}`
- VOD: `mlbb_vseg_yes:{segment_id}` / `mlbb_vseg_no:{segment_id}`

Handler module: `scripts/mlbb_telegram_handlers.py`

Integrate in `telegram_upload_bot.py`:

```python
from mlbb_telegram_handlers import handle_callback_query
# in callback handler:
handle_callback_query(query)
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| No videos | Check `MLBB_SEND_ENABLED=1`, worker running, daily cap |
| Frozen clips | Long 60fps VOD — add to `blocked_vods.json` |
| `skip feed: another calibration_feed` | Stale lock — auto-cleared in new feed version |
| Multiple vod feeds | Singleton lock in `vod_segment_feed.lock` |
| Worker dead | Watchdog restarts + Telegram alert |
