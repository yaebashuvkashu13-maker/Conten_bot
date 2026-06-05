# Conten_bot

> **Для AI-агентов и новых разработчиков:** вся архитектура, VPS, Telegram, Smart Edit, данные и типичные ошибки — в **[docs/AGENT_HANDBOOK.md](docs/AGENT_HANDBOOK.md)**.  
> Прочитайте его перед работой; после этого не нужно заново объяснять проект устно.

## Instagram -> Telegram skeleton

This repository now contains a minimal Python skeleton for pulling Instagram
posts via `yt-dlp` and publishing them into a Telegram channel.

### Files

- `pyproject.toml` - project metadata and dependencies
- `config.example.yaml` - example config with Telegram and Instagram sources
- `content_bot/instagram_ingest.py` - fetches Instagram posts/profile entries
- `content_bot/telegram_publisher.py` - publishes to Telegram Bot API
- `content_bot/state.py` - stores published post IDs to avoid duplicates
- `content_bot/main.py` - CLI entrypoint
- `content_bot/tiktok_dataset.py` - collects TikTok profile datasets via `yt-dlp`
- `content_bot/video_features.py` - extracts simple motion/visual features for ML
- `content_bot/hero_classifier.py` - trains a weak baseline classifier from extracted video features

### Quick start

1. Copy the example config:

   ```bash
   cp config.example.yaml config.yaml
   ```

2. Fill in:
   - Telegram bot token
   - Telegram channel ID / `@channel_name`
   - Instagram bloggers or post URLs
   - optional `instagram_cookies_path` if Instagram blocks public scraping
   - optional `proxy_url` if access should go through a proxy

3. Install dependencies:

   ```bash
   python3 -m pip install -e .
   ```

4. Run:

   ```bash
   python3 -m content_bot.main --config config.yaml
   ```

### State, idempotency, and safety

- `state_path` stores published Instagram `post_id` values — reposts are skipped.
- State is written **atomically** (temp file + rename). A `.bak` copy is kept.
- If JSON is corrupt, the bad file is renamed to `.corrupt.*` and `.bak` is restored when possible.
- If Telegram publish succeeds but state save fails, IDs go to `.recovery.jsonl` and are merged on the next run (no duplicate send).
- `config.yaml` and cookies are in `.gitignore` — **never commit bot tokens or session cookies**.
- `dry_run: true` skips Telegram API calls; Instagram is still fetched (use sparingly).

### Current limitations

- Telegram: thumbnail via `sendPhoto` or text link — full Instagram Reels/video files are not uploaded yet.
- Instagram carousels: first media item only.
- Post order: after `reverse()`, **oldest entries publish first** (newest last in queue).

### Notes

- The current implementation is intentionally simple and public-source oriented.
- Instagram profile extraction may require an authenticated cookies file depending
  on the source account and current anti-bot behavior.
- It is a foundation for later additions:
  - Russian rewriting / translation
  - media cleanup / redesign
  - stickers / templates
  - scheduling and moderation

## TikTok dataset foundation

Two helper scripts are included for ML/data work:

### Collect TikTok profile data

```bash
python3 -m content_bot.tiktok_dataset \
  --profile-url "https://www.tiktok.com/@mlbbttofficial" \
  --proxy-url "http://user:pass@host:port" \
  --max-entries 20 \
  --label "mlbb-official" \
  --download-media
```

### Extract simple ML features from videos

```bash
python3 -m content_bot.video_features \
  --input-dir datasets/tiktok \
  --output-csv datasets/features/mlbb.csv \
  --label hayabusa
```

This is the first layer for:
- hero classification
- skin classification
- ranking clips by engagement and content pattern
- scaling to PUBG / Genshin / WoT / Standoff 2

### Train weak hero classifier

```bash
python3 -m content_bot.hero_classifier train \
  --positive-dir /path/to/hayabusa_videos \
  --negative-dir /path/to/non_hayabusa_videos \
  --output-dir models/hayabusa_v1
```

### Score new videos with the trained model

```bash
python3 -m content_bot.hero_classifier score \
  --model-dir models/hayabusa_v1 \
  --input-dir /path/to/unlabeled_videos \
  --output-csv reports/hayabusa_scores.csv
```

