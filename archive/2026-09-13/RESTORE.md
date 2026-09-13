# RESTORE — поднять Content Bot через ~6 месяцев (новый сервер)

Русско-дружелюбный чеклист. Secrets **не** в git — только имена ключей.

## 0) Что скачать с GitHub

1. Клонировать репозиторий:  
   `git clone https://github.com/yaebashuvkashu13-maker/Conten_bot.git && cd Conten_bot`
2. Ветка: `main` (архив `archive/2026-09-13/`).  
   Прод-код паузы также в `scripts/` (синхронизированные файлы) и `archive/2026-09-13/code_snapshot/`.
3. Git tip сервера на паузе: **`8d8e7808f6b43ea1d64dee4380c6a5cd9188409e`**.

## 1) Машина

- Ubuntu 22.04+ , Python 3.12, ffmpeg, yt-dlp, deno (для yt-dlp clients), достаточный диск (**≥100GB** свободно под VOD inbox).
- Установить deps из `requirements` / egg / docs проекта (opencv, torch/PANNs по необходимости, joblib, sklearn, python-telegram-bot и т.д.).

## 2) Секреты — создать `/root/.video_bot.env`

Скопировать шаблон: `archive/2026-09-13/ENV.example` → `/root/.video_bot.env`, заполнить `<REDACTED>`:

**Обязательные (типично):**
- `TG_BOT_TOKEN` / токен бота **programofloyalbot**
- `TG_CHAT_ID` (чат Anton)
- Любые `*_CHAT_ID`, YouTube cookies path если нужны
- `CONTENT_BOT_REPO=/root/content_bot_ml` (или путь клона)
- PUBG-only флаги: `EU_PUBG_ONLY=1` и связанные `PUBG_*` из ENV.example

Полный список имён ключей: `archive/2026-09-13/ENV.example` + `docs` pack `ENV_KEYS_PUBG.txt`.

**Никогда не коммитить** заполненный `.video_bot.env`.

## 3) Восстановить learning data

```bash
sudo mkdir -p /root/data/pubg /root/data/mlbb /root/data/vod_quality_ledger /root/data/vod_adaptive_thresholds
# вариант A — из распакованного дерева архива
cp -a archive/2026-09-13/data/pubg/* /root/data/pubg/
cp -a archive/2026-09-13/data/mlbb/* /root/data/mlbb/
cp -a archive/2026-09-13/data/vod_quality_ledger/* /root/data/vod_quality_ledger/
cp -a archive/2026-09-13/data/vod_adaptive_thresholds/* /root/data/vod_adaptive_thresholds/

# вариант B — большие файлы из gzip
gzip -dc archive/2026-09-13/learning_compressed/pubg_moment_ranker.joblib.gz > /root/data/pubg/pubg_moment_ranker.joblib
gzip -dc archive/2026-09-13/learning_compressed/pubg_vod_segment_labels.json.gz > /root/data/pubg/vod_segment_labels.json
gzip -dc archive/2026-09-13/learning_compressed/pubg_clip_ledger.jsonl.gz > /root/data/vod_quality_ledger/pubg_clip_ledger.jsonl

# вариант C — полный tar
tar -xzf archive/2026-09-13/content_bot_learning_20260913.tar.gz -C /tmp
# затем скопировать data/ как выше; проверить sha256 из TARBALL.sha256 / SHA256SUMS.txt
```

Проверка: существуют `pubg_owner_labels.json` и `pubg_moment_ranker.joblib`; `pubg_sent_ranges.json` имеет `"ready": true`.

## 4) Код и `/usr/local/bin`

```bash
git checkout main   # или нужный commit
sudo mkdir -p /usr/local/bin
sudo cp -a scripts/*.py scripts/*.sh /usr/local/bin/ 2>/dev/null || true
# сверить с archive/2026-09-13/code_snapshot/ при сомнениях
export PYTHONPATH=/root/content_bot_ml/scripts:/usr/local/bin
```

## 5) systemd

```bash
sudo cp archive/2026-09-13/systemd/*.service archive/2026-09-13/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now content-bot-vod-feed.service telegram-upload-bot.service
sudo systemctl enable --now content-bot-vod-hang.timer content-bot-vod-cleanup.timer
sudo systemctl enable --now content-bot-pubg-ranker.timer
# shorts autolearn — чинить отдельно (на паузе был failed)
```

## 6) Smoke test

1. `systemctl status content-bot-vod-feed telegram-upload-bot`
2. В Telegram: `/recover` или дождаться heartbeat в ledger
3. Прогнать ranker dry: `python3 /usr/local/bin/pubg_moment_ranker.py --help`
4. Не скачивать терабайты сразу — включить cleanup timer

## 7) Предпочтения владельца (не потерять)

- Короткие перестрелки 5–12с, **хвост после килла длиннее**, старт короткий
- Резать лут / AFK / бег без стрельбы
- Metro Royale PUBG Mobile
- См. `OWNER_PREFERENCES.md`

## 8) Если чего-то нет в git

- Сырые VOD mp4 **намеренно не** архивировались — качать заново через discovery
- Exemplar mp4 — список в `EXCLUDED_MP4.txt`; попросить владельца переслать эталоны в TG
