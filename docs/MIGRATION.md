# MLBB Content Farm — Migration Checklist

## Ready in git (portable)

| Component | Path | Notes |
|-----------|------|-------|
| Worker + watchdog | `scripts/mlbb_continuous_worker*.py/sh` | 24/7 orchestrator |
| VOD pipeline | `scripts/mlbb_vod_segment_feed.py`, `mlbb_vod_segment_store.py` | Kill-first, presend gate |
| Shorts ingest + feed | `scripts/mlbb_youtube_shorts_ingest.py`, `mlbb_calibration_feed.py` | Calibration queue |
| Kill UI + fight bounds | `scripts/mlbb_kill_ui.py`, `mlbb_fight_segment.py` | Detection |
| Calibration store | `scripts/mlbb_calibration_store.py` | Labels, index |
| Telegram handlers | `scripts/mlbb_telegram_handlers.py`, `mlbb_telegram_send.py` | 👍/👎 callbacks |
| Daily report | `scripts/mlbb_daily_report.py` | Status |
| Deploy script | `deploy/mlbb_deploy.sh` | One-command install |
| Env template | `config/mlbb.env.example` | All MLBB_* vars |
| Tests | `tests/test_mlbb_*.py` | 11 smoke tests |
| Runbook | `docs/MLBB_FARM.md` | Ops guide |

## Must copy from old server (not in git)

| Item | Path on server | Size / note |
|------|----------------|-------------|
| Secrets | `/root/.video_bot.env` | TG_BOT_TOKEN, TG_CHAT_ID |
| MLBB data | `/root/data/mlbb/` | State, labels, indexes, blocked_vods |
| VOD inbox | `/root/data/mlbb/youtube_nightly/inbox/` | Downloaded VODs |
| Shorts pool | `/root/datasets/mlbb/youtube_shorts/` | ~167 Shorts mp4 |
| VOD segments | `/root/datasets/mlbb/vod_segments/` | Rendered clips + exemplars |
| Exemplars | `/root/content_bot_ml/data/highlight_exemplars/` | CLIP training clips |
| Owner labels | `mobile_legends_owner_labels.json` | Per-zone 👍/👎 |
| Telegram bot | `/usr/local/bin/telegram_upload_bot.py` | Full bot (~110KB) |
| ML deps | `/usr/local/bin/smart_video_editor.py`, `highlight_scorer.py`, `gameplay_gate.py`, `mlbb_learning_first.py`, `youtube_download.py`, `montage_env.py`, `preview_gate.py`, `strict_montage_direct.py`, `visual_action_check.py` | Server Python stack |
| System deps | ffmpeg, ffprobe, tesseract, curl, python3-opencv | apt packages |
| Cron | `*/5 * * * * mlbb_continuous_worker_watchdog.sh` | Worker restart |
| Running services | `telegram_upload_bot.py`, `mlbb_continuous_worker.py` | systemd or nohup |

## New server setup (order)

```bash
# 1. System packages
apt update && apt install -y ffmpeg tesseract-ocr curl python3-pip python3-venv git

# 2. Clone repo
git clone https://github.com/yaebashuvkashu13-maker/Conten_bot.git /root/content_bot_ml
cd /root/content_bot_ml && git checkout cursor/content-farm-fixes-1a63

# 3. Copy data from old server (rsync)
rsync -avz old:/root/.video_bot.env /root/
rsync -avz old:/root/data/mlbb/ /root/data/mlbb/
rsync -avz old:/root/datasets/mlbb/ /root/datasets/mlbb/
rsync -avz old:/root/content_bot_ml/data/ /root/content_bot_ml/data/

# 4. Copy server-only Python modules
rsync -avz old:/usr/local/bin/smart_video_editor.py old:/usr/local/bin/highlight_scorer.py \
  old:/usr/local/bin/gameplay_gate.py old:/usr/local/bin/mlbb_learning_first.py \
  old:/usr/local/bin/youtube_download.py old:/usr/local/bin/montage_env.py \
  old:/usr/local/bin/preview_gate.py old:/usr/local/bin/strict_montage_direct.py \
  old:/usr/local/bin/visual_action_check.py old:/usr/local/bin/telegram_upload_bot.py \
  /usr/local/bin/

# 5. Deploy MLBB scripts from repo
bash /root/content_bot_ml/deploy/mlbb_deploy.sh

# 6. Python deps
pip install numpy opencv-python-headless yt-dlp pandas scikit-learn pytesseract torch torchaudio  # as needed

# 7. Cron + start services
echo '*/5 * * * * /usr/local/bin/mlbb_continuous_worker_watchdog.sh' | crontab -
nohup python3 /usr/local/bin/mlbb_continuous_worker.py >> /root/data/mlbb/mlbb_continuous_worker.log 2>&1 &
nohup python3 /usr/local/bin/telegram_upload_bot.py >> /root/data/mlbb/logs/telegram_upload_bot.log 2>&1 &

# 8. Verify
python3 /usr/local/bin/mlbb_daily_report.py --telegram
PYTHONPATH=/usr/local/bin python3 -m pytest /root/content_bot_ml/tests/ -q
```

## Migration readiness score

| Area | Status |
|------|--------|
| Core pipeline in git | ✅ Ready |
| Deploy automation | ✅ `mlbb_deploy.sh` |
| Env documentation | ✅ `mlbb.env.example` |
| Tests | ✅ 11 tests |
| Data backup script | ⚠️ Manual rsync (see above) |
| Server-only deps list | ✅ Documented above |
| telegram_upload_bot in git | ❌ Still server-only — copy manually |
| ML/scoring stack in git | ❌ Copy from server or reinstall |
| systemd units | ❌ Uses nohup+cron today — optional improvement |

**Verdict:** Migration is **~80% ready**. Repo covers the MLBB farm logic; you must rsync `/root/data/mlbb/`, `/root/datasets/mlbb/`, `.video_bot.env`, and ~10 server Python modules. Plan 1–2 hours for rsync + smoke test on new VPS.

## Post-migration smoke test

1. `mlbb_daily_report.py --telegram` → message arrives
2. 6 Shorts arrive within 5 min
3. 1 VOD clip arrives within 15 min
4. 👍 button works on Shorts and VOD clip
5. Worker heartbeat updates every minute in `mlbb_continuous_state.json`
