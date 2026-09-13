# RESTORE — поднять Content Bot через ~6 месяцев (новый сервер)

Русско-дружелюбный чеклист. Secrets **не** в git — только имена ключей.

## 0) Что скачать с GitHub

1. Клонировать: `git clone https://github.com/yaebashuvkashu13-maker/Conten_bot.git && cd Conten_bot`
2. Ветка: `main` (архив `archive/2026-09-13/`). Прод-код: `scripts/` + `archive/2026-09-13/code_snapshot/`.
3. Git tip сервера на паузе: **`8d8e7808f6b43ea1d64dee4380c6a5cd9188409e`**.

## 1) Машина

- Ubuntu 22.04+, Python 3.12, ffmpeg, yt-dlp, deno, диск **≥100GB** свободно.
- Установить deps проекта (opencv, joblib, sklearn, telegram, PANNs/torch по необходимости).

## 2) Секреты — `/root/.video_bot.env`

Скопировать `archive/2026-09-13/ENV.example` → `/root/.video_bot.env`, заполнить `<REDACTED>`:
- Токен бота **programofloyalbot**, `TG_CHAT_ID` (Anton)
- `CONTENT_BOT_REPO`, PUBG-only флаги (`EU_PUBG_ONLY` и `PUBG_*`)
- См. `ENV.example` + `ENV_KEYS_PUBG.txt`

**Никогда не коммитить** заполненный `.video_bot.env`.

## 3) Learning data

```bash
sudo mkdir -p /root/data/pubg /root/data/mlbb /root/data/vod_quality_ledger /root/data/vod_adaptive_thresholds
cp -a archive/2026-09-13/data/pubg/* /root/data/pubg/
cp -a archive/2026-09-13/data/mlbb/* /root/data/mlbb/
cp -a archive/2026-09-13/data/vod_quality_ledger/* /root/data/vod_quality_ledger/
cp -a archive/2026-09-13/data/vod_adaptive_thresholds/* /root/data/vod_adaptive_thresholds/
# большие файлы:
gzip -dc archive/2026-09-13/learning_compressed/pubg_moment_ranker.joblib.gz > /root/data/pubg/pubg_moment_ranker.joblib
gzip -dc archive/2026-09-13/learning_compressed/pubg_vod_segment_labels.json.gz > /root/data/pubg/vod_segment_labels.json
gzip -dc archive/2026-09-13/learning_compressed/pubg_clip_ledger.jsonl.gz > /root/data/vod_quality_ledger/pubg_clip_ledger.jsonl
# или: bash archive/2026-09-13/tarball_parts/REASSEMBLE.sh && tar -xzf content_bot_learning_20260913.tar.gz
```

Проверка: `pubg_owner_labels.json`, `pubg_moment_ranker.joblib`, `pubg_sent_ranges.json` с `"ready": true`.

## 4) Код и /usr/local/bin

```bash
sudo cp -a scripts/*.py scripts/*.sh /usr/local/bin/ 2>/dev/null || true
export PYTHONPATH=/root/content_bot_ml/scripts:/usr/local/bin
```

## 5) systemd

```bash
sudo cp archive/2026-09-13/systemd/*.service archive/2026-09-13/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now content-bot-vod-feed.service telegram-upload-bot.service
sudo systemctl enable --now content-bot-vod-hang.timer content-bot-vod-cleanup.timer content-bot-pubg-ranker.timer
```

## 6) Smoke test

1. `systemctl status content-bot-vod-feed telegram-upload-bot`
2. Telegram `/recover`
3. `python3 /usr/local/bin/pubg_moment_ranker.py --help`

## 7) Вкус владельца

Короткие перестрелки 5–12с, хвост после килла длиннее, без лута/AFK — см. `OWNER_PREFERENCES.md`.
