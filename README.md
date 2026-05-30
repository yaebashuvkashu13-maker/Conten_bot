# Conten_bot

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
- `config.tiktok-mlbb.example.yaml` - proxy-safe TikTok dataset config for MLBB sources

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

For a larger MLBB dataset, copy the example config and keep proxy credentials in
an environment variable instead of committing them:

```bash
cp config.tiktok-mlbb.example.yaml config.tiktok-mlbb.yaml
export TIKTOK_PROXY_URL="http://user:pass@host:port"
python3 -m content_bot.tiktok_dataset --config config.tiktok-mlbb.yaml
```

Set `max_entries` per source so the total collected set reaches the target size
(for example 5 sources x 100 entries = 500 videos). The manifest files are stored
as JSONL next to the downloaded media, and repeated runs skip duplicate manifest
records by default.

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

### Build a gameplay montage

After collecting videos and running the gameplay filter, build a vertical
33-57 second montage from 3-4 gameplay-only scenes for one hero:

```bash
python3 -m content_bot.montage_builder \
  --hero gusion \
  --report-csv datasets/tiktok/reports/gameplay_filter_full.csv \
  --target-duration 45 \
  --scenes 4 \
  --output-dir datasets/outputs/montages
```

The builder skips known promo/official/event sources, keeps game audio, normalizes
volume, and uses video/audio crossfades so scenes do not cut off abruptly.

### Reduce music while keeping game sounds

For downloaded TikTok clips where music is mixed with gameplay sounds, use the
fast local audio cleaner. It cannot perfectly split sources, but it reduces common
music-bed ranges and preserves high-mid game SFX/transients:

```bash
python3 -m content_bot.audio_game_cleaner \
  --input datasets/outputs/montages/gusion_4scenes_45s.mp4 \
  --output datasets/outputs/montages/gusion_4scenes_45s_sfx.mp4 \
  --strength 0.85
```

There is also an optional heavier mode:

```bash
python3 -m pip install demucs
python3 -m content_bot.audio_game_cleaner \
  --method demucs \
  --input input.mp4 \
  --output output_sfx.mp4
```

Demucs is best-effort here: it separates music stems, not "game audio" directly,
so final quality still needs manual listening checks.

### Send Instagram posts/Reels to Telegram daily

The Instagram profile extractor in `yt-dlp` may fail, so this project also has a
cookie-based Instagram pipeline that uses Instagram's web API and a local state
file to avoid duplicate Telegram posts. It prioritizes photo posts and carousels,
then falls back to Reels/videos.

Required runtime secrets should be provided through environment variables, not
committed files:

```bash
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="1006141589"
# Optional: send the same posts to multiple chats.
export TELEGRAM_CHAT_IDS="1006141589,SECOND_CHAT_ID"
export INSTAGRAM_COOKIES_PATH="instagram_cookies.cookies"
export INSTAGRAM_PROXY_URL="socks5://user:pass@host:port"
```

Run once:

```bash
python3 -m content_bot.instagram_reels_pipeline \
  --config config.instagram-mlbb.yaml \
  --max-posts 3
```

Run daily at 18:00 Moscow time:

```bash
python3 -m content_bot.instagram_daily_scheduler \
  --time 18:00 \
  --timezone Europe/Moscow \
  --config config.instagram-mlbb.yaml \
  --max-posts 7
```

