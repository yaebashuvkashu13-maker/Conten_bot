# MLBB Content Farm

24/7 pipeline: YouTube MLBB VODs → 7–22s kill-UI clips → Telegram 👍/👎 calibration.

## Architecture

```
mlbb_continuous_worker (24/7)
├── mlbb_youtube_shorts_ingest   → Shorts calibration queue
├── mlbb_vod_segment_feed        → VOD kill-first scan → render → presend → send
├── mlbb_calibration_feed        → Send Shorts batches to owner
└── mlbb_continuous_worker_watchdog.sh (cron */2) + mlbb_health_guard.py (auto-recovery)

## Steady mode (default)

Avoids burst-then-silence:

| Parameter | Meaning |
|-----------|---------|
| `MLBB_STEADY_MODE=1` | Fixed pacing, no tier-3 flood |
| `MLBB_STEADY_FEED_INTERVAL_SEC=720` | ~4 clips every 12 min when queue has buffer |
| `MLBB_STEADY_INGEST_COOLDOWN_SEC=300` | Background ingest every 5 min while pending < target |
| `MLBB_MAX_SILENCE_SEC=5400` | Auto-recovery if no delivery 90 min |
| `MLBB_RESEND_UNLABELED_HOURS=48` | No duplicate sends (same clip) |

Autonomic recovery (`mlbb_health_guard.py`): clears stale locks, disk index, starvation ingest, forced feed — **no manual revive**.

```bash
python3 /usr/local/bin/mlbb_health_guard.py --check
python3 /usr/local/bin/mlbb_health_guard.py --recover
```

Cron:

```cron
*/2 * * * * /usr/local/bin/mlbb_continuous_worker_watchdog.sh
```

telegram_upload_bot.py           → 👍/👎 callbacks (uses mlbb_telegram_handlers)
```

## Server paths

| Path | Purpose |
|------|---------|
| `/root/.video_bot.env` | Secrets (TG_BOT_TOKEN, TG_CHAT_ID) |
| `/root/data/mlbb/` | State, labels, indexes |
| `/root/datasets/mlbb/vod_segments/` | Rendered VOD clips |
| `/root/datasets/mlbb/youtube_shorts/` | Downloaded Shorts (2026+) |
| `/root/datasets/mlbb/training_archive/2026/shorts/` | Full Shorts on owner 👍 (reuse) |
| `/root/datasets/mlbb/training_archive/2026/vod_segments/` | VOD clips on owner 👍 |
| `/root/data/mlbb/training_archive_index.jsonl` | Archive index |
| `/root/data/mlbb/youtube_nightly/inbox/` | MLBB VOD inbox |

## Training archive (reuse liked clips)

Ingest downloads Shorts uploaded **from 2026-01-01** (`MLBB_SHORTS_MIN_UPLOAD_DATE`).

When you press 👍:
- **Shorts** → full mp4 copied to `/root/datasets/mlbb/training_archive/2026/shorts/yt_{id}.mp4`
- **VOD segment** → `/root/datasets/mlbb/training_archive/2026/vod_segments/seg_{id}.mp4`
- Index append: `/root/data/mlbb/training_archive_index.jsonl`

List archived Shorts:

```bash
ls /root/datasets/mlbb/training_archive/2026/shorts/
```

## Training loop (owner 👍/👎)

```
Owner feedback → mlbb_telegram_handlers
  → labels + exemplars + training_archive
  → schedule_mlbb_retrain() (debounced: every 10 labels or 6h)
  → mlbb_learn_apply.sh → mlbb_train_classifier.py
  → data/mlbb/highlight_classifier_mobile_legends.joblib
```

Manual retrain:

```bash
bash scripts/mlbb_learn_apply.sh
python3 scripts/mlbb_train_classifier.py
```

Hero reference pack (showcase gate):

```bash
python3 scripts/mlbb_hero_refs_download.py
# → /root/datasets/mlbb/hero_refs/{hero_id}/icon.png
```

Active learning: `MLBB_ACTIVE_LEARNING=1` sorts pending queue by model uncertainty (clips near score threshold first).

## Deploy

```bash
cd /root/content_bot_ml
git pull
bash deploy/mlbb_deploy.sh
```

Cron (watchdog every 2 min + auto-recovery):

```cron
*/2 * * * * /usr/local/bin/mlbb_continuous_worker_watchdog.sh
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
5. **Opening trim** — skips intro/lobby junk at t=0 before Telegram send
6. Kill UI detector + OCR keywords
2. Variable fight length 7–22s
3. Presend: freeze detect + motion + spawn check
4. Owner 👎 → zone blocked in `mobile_legends_owner_labels.json`

## Telegram callbacks

Button formats:
- Shorts: `mlbb_hq_shorts:{video_id}` (📥 Скачать оригинал → send HQ + mark good) / `mlbb_no:{video_id}` (👎)
- VOD: `mlbb_hq_vseg:{segment_id}` / `mlbb_vseg_no:{segment_id}`
- Legacy (old messages): `mlbb_yes:` / `mlbb_vseg_yes:` still parsed but new sends use download button only
- `MLBB_HQ_AUTO_ON_GOOD=0` — no automatic HQ send; only on button press

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
