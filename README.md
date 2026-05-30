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
- `content_bot/bulk_collect.py` - bulk TikTok download + Instagram snapshot via proxy
- `content_bot/dataset_index.py` - indexes local `.mp4` files (your existing video base)
- `content_bot/batch_features.py` - extracts features for hundreds/thousands of videos
- `content_bot/proxy_config.py` - reads proxy from config or `PROXY_URL` env

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

### Rate limits (HTTP 429 / Telegram flood control)

When publishing many posts in a row, Telegram may return **429** (flood control).
The bot retries automatically using `retry_after` from the API response and sleeps
between publishes (`publish_delay_seconds` in config). Tune both values in
`config.yaml` if you still hit limits with large source lists.

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

## Priority 1: use proxy before it expires

TikTok and Instagram often block direct requests. While your proxy subscription is
still active, collect data locally so you can train models without paying again
until you really need fresh scraping.

```bash
cp config.collect.example.yaml config.collect.yaml
export PROXY_URL="http://user:pass@host:port"

# TikTok videos + manifests
python3 -m content_bot.bulk_collect --config config.collect.yaml --tiktok

# Instagram metadata snapshot (works with config.instagram-mlbb.yaml + cookies)
python3 -m content_bot.bulk_collect --config config.collect.yaml --instagram
```

`config.collect.yaml` is gitignored — keep proxy credentials only on your machine.

## Priority 2: index your existing ~2300 videos

Videos are not stored in git (`datasets/` is ignored). Point tools at your folder:

```bash
python3 -m content_bot.dataset_index --root /path/to/your/videos
python3 -m content_bot.batch_features --from-index datasets/index/videos.csv
```

For hero/skin training, organize copies under `datasets/labeled/` (see
`datasets/labeled/README.md`).

## Priority 3: hero + skin classification

Multiclass training from folder names (`hero` or `hero/skin`):

```bash
python3 -m content_bot.hero_classifier train-multiclass \
  --data-dir datasets/labeled \
  --output-dir models/mlbb_heroes_v1
```

Or train from precomputed features (faster iteration):

```bash
python3 -m content_bot.hero_classifier train-multiclass \
  --features-csv datasets/features/all.csv \
  --output-dir models/mlbb_heroes_v1
```

## Roadmap (your priorities)

| Phase | Goal | Status |
|-------|------|--------|
| 1 | Bulk download via proxy (TikTok + Instagram snapshot) | CLI ready |
| 2 | Index + features for large local video base | CLI ready |
| 3 | Hero/skin multiclass classifier | baseline ready |
| 4 | **Original videos** (not just cuts) — templates, VO, compositing | not started |
| 5 | Instagram → Telegram with good RU captions | skeleton only |
| 6 | Translate text inside images | future |

Phase 4 (original content) will need a separate video composition module on top of
classification — the current ML stack only understands and scores footage.

