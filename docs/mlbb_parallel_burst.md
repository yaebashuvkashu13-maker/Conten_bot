# MLBB burst download (прокси на 1–2 дня)

## Задача

Скачать **4000–5000** роликов Mobile Legends за короткое окно оплаченного прокси. Источники **не только CSV**, а:

- таблицы `current_mlbb_ranked_videos.csv` и `gameplay_filter_latest.csv`;
- официальные и фанатские каналы TikTok;
- хештеги (`#mlbb`, `#hayabusa`, …);
- поиск yt-dlp (`tiktoksearch…: mobile legends gameplay`).

## Параллельно (одна команда)

```bash
bash /usr/local/bin/run_parallel_stack.sh
```

| Процесс | Что делает |
|---------|------------|
| `tiktok_mass_download.py` | 8 потоков yt-dlp, цель 5000 mp4 |
| `instagram_background_worker.py` | тик каждые 10 мин (подготовка дайджеста) |
| `audio_game_extract_worker.py` | wav из скачанных mp4 каждые ~90 с |
| `ad_screenshot_ingest.py` | индекс скринов рекламы |

## Деплой на VPS

```bash
cd /root/content_bot_ml && git pull
bash scripts/deploy_parallel_burst.sh /root/content_bot_ml/scripts
bash /usr/local/bin/run_parallel_stack.sh
```

## Проверка

```bash
find /root/datasets/tiktok/mlbb -name '*.mp4' | wc -l
tail -f /root/data/mlbb/mass_download.log
cat /root/data/mlbb/download_state.json | jq '.mass_last_stats,.mass_on_disk'
```

Геймплей остаётся в `mlbb/<channel>/`. Остальное — в `mlbb/non_gameplay/` (не удаляем, пригодится для обучения «что не слать»).

## Скрины рекламы из Telegram

1. В боте: **`/ad`** (или **`/реклама`**) — включить режим.
2. Пришлите **фото** скринов (можно пачкой).
3. **`/ad_done`** — выключить режим и проиндексировать.

Файлы: `/root/data/mlbb/ad_examples/`, индекс: `ad_examples_index.json`.

## Перевод текста на картинке на русский

| Вариант | Сложность | Когда |
|---------|-----------|--------|
| Русская подпись в Telegram (саммари поста) | низкая | **сейчас** — Instagram digest |
| Субтитры поверх видео (burn-in RU) | средняя | после стабильного ASR |
| Замена текста **на самой картинке** (OCR → inpaint → отрисовка) | высокая, желателен GPU | отдельный этап |

Полный «перевод на картинке» технически возможен (PaddleOCR / EasyOCR + LaMa inpaint + PIL/FreeType), но на CPU на тысячах кадров это медленно и дорого по VPS. Рациональный план: сначала русский текст **под** контентом в боте, затем пилот на 1–2 скринах рекламы, которых вы пришлёте.
