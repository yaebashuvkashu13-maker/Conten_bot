# Ночной отчёт: разметка игр, конкуренты, стабильность, roadmap

> Авто-исследование + аудит пайплайна MLBB Shorts farm (2026-06-15).

---

## 1. Как размечают игровое видео (MLBB / MOBA / шooters)

### Карта сигналов (что у нас уже есть vs что добавить)

| Зона экрана | Что означает | Текущий бот | Лучшие практики |
|-------------|--------------|-------------|-----------------|
| **Миникарта** (низ-слева MLBB) | Позиции, teamfight, objective | `minimap_delta`, presence rate | HSV-трекинг иконок ([mlbb_analyze_move](https://github.com/shonagase/mlbb_analyze_move)), YOLO на миникарте ([DeepestLeague](https://github.com/bsowlx/DeepestLeague)) |
| **Джойстик / скиллы** (низ-справа) | Активность игрока, не зритель | `skill_delta` как proxy | Отдельный ROI + temporal variance (`mlbb_hud_signals.py`) |
| **Top HUD / счёт** | KDA, таймер, фаза матча | `top_hud` stddev | OCR KDA + детект «Replay» / speed controls |
| **Kill banner / feed** | Savage, Maniac, wipe | `mlbb_kill_ui.py` (цвет + OCR) | YOLO epic-moment UI ([mlbb-ai-clipper](https://huggingface.co/frendyrachman/mlbb-ai-clipper)) |
| **Центр экрана** | Бой, cinematic, skin review | motion + center_text | CLIP exemplars + reject hero showcase |
| **Аудио** | Выстрелы, крики, музыка | PANNs gunfire (PUBG), music bed reject | Мультимодаль: audio spike + visual confirm |

### Replay vs live — не один индикатор

**Replay** в MLBB — это не видеофайл, а прогон inputs движком ([источник](https://mlbbhub.com/news/mlbb-how-to-watch-replay-guide-2026)). На YouTube часто:

- screen-record реплея без «живого» джойстика;
- много action в центре, но frozen skill bar;
- в title бывает `replay`.

**Live / phone capture:**

- высокая variance в зоне джойстика;
- миникарта + skills двигаются синхронно с боем.

**Вывод:** джойстик ≠ единственный признак. Комбинируем:

```
combat_intensity = center_motion + minimap + joystick + top_hud
replay_likelihood = title + (high center, low joystick)
live_match_likelihood = joystick + minimap + motion − replay_penalty
```

Реализовано в `scripts/mlbb_hud_signals.py`, метаданные пишутся при ingest, soft-boost в очереди калибровки.

**Политика:** по умолчанию replay **не режем** (`MLBB_REJECT_REPLAY=0`). Для обучения montage cuts live ranked предпочтительнее — soft rank boost.

### «Смотреть на общий счёт» (high rank)

На mythic/ranked:

- меньше «нубских» ошибок в кадре;
- больше structured teamfights;
- kill feed / scoreboard меняются предсказуемо.

**План:**

1. OCR top HUD (K/D или MVP overlay) — фаза 2;
2. фильтр title/search: `mythic`, `ranked`, `savage` (уже частично);
3. после 100+ 👍/👎 — LR/CLIP ranker на ваших labels, не generic PUBG LR.

---

## 2. Кто делает то же самое?

### MLBB

| Проект | Подход | Чем полезен нам |
|--------|--------|-----------------|
| [Eklipse.gg](https://eklipse.gg) | AI stream → highlights, MLBB support | Продуктовый эталон UX; event detection + auto vertical |
| [mlbb-ai-clipper (HF)](https://huggingface.co/frendyrachman/mlbb-ai-clipper) | YOLOv8m на epic UI | **Готовая модель** для Savage/Maniac banner — можно подключить как tier-2 gate |
| [mlbb_analyze_move](https://github.com/shonagase/mlbb_analyze_move) | OpenCV HSV minimap | Трекинг позиций, zone heatmap |
| In-game Highlights Recording | Встроенная запись лучших моментов | Источник «ground truth» клипов от Moonton |

**Open-source бота «как у нас» (Shorts farm + Telegram 👍/👎) — не найден.** Ближе всего: Eklipse/Short.ai (SaaS), mlbb-ai-clipper (только детект UI).

### PUBG

| Проект | Подход |
|--------|--------|
| [Short.ai PUBG](https://www.short.ai/ai-clip-maker/pubg-clips) | 1v4, snipe, chicken dinner + virality score |
| [Clypse PUBG](https://clypse.ai/tools/pubg-clipper) | VOD → vertical + captions |
| [NiceShot_AI](https://github.com/karimm-ai/NiceShot_AI) | YOLO per-game + OCR events |
| [Roboflow peace-game / big-pubgs](https://universe.roboflow.com/pubg-mobile/peace-game) | Datasets person+gun (не HUD, но CV base) |
| IEEE GUI shooter dataset | Minimap, ammo, progress bar classes |

**Наш PUBG:** scoring в `gameplay_gate.py` + `highlight_scorer.py`, но **нет** worker/ingest/Telegram farm. `pubg_combat_gate` — server-only.

---

## 3. Как TikTok / YouTube «понимают» игры

По публичным исследованиям ([Knight Institute](https://knightcolumbia.org/content/understanding-social-media-recommendation-algorithms), [arxiv 2503.20030](https://arxiv.org/pdf/2503.20030), [MLLM for video rec 2508.09789](https://arxiv.org/pdf/2508.09789)):

1. **Computer vision** — объекты, текст на экране, стиль монтажа;
2. **NLP** — title, hashtags, ASR аудио;
3. **Metadata** — game tag если автор указал;
4. **Embeddings** — user + video в общем пространстве (deep learning);
5. **Watch time / completion** — главный сигнал YouTube (не CTR).

**Что взять для бота:**

| Их сила | Наш аналог | Действие |
|---------|------------|----------|
| Multimodal embeddings | CLIP exemplars + PANNs | Расширять exemplar bank с 👍 |
| Semantic captions (MLLM) | title + OCR kill banner | Опционально: Qwen-VL caption для index |
| Game category | `profile=mobile_legends` | Жёсткий MLBB gate перед send |
| Engagement prediction | owner 👍/👎 | Active learning sort + retrain |
| Diversity | dedupe paths/ids | уже есть |

Мы **не конкурируем с их трафиком** — цель другая: **owner labels → montage model**, не FYP optimization.

---

## 4. Что сделано этой ночью (код)

| Изменение | Файл |
|-----------|------|
| HUD signals: minimap/joystick/replay/live | `scripts/mlbb_hud_signals.py` |
| HUD metadata при ingest + sort boost | `mlbb_youtube_shorts_ingest.py`, `mlbb_calibration_store.py` |
| PUBG UI refs downloader + layout JSON | `scripts/pubg_ui_refs_download.py` |
| Disk guard (88% warn / 95% critical) | `mlbb_health_guard.py` |
| Block Jess No Limit + skin review | `mlbb_channel_blocklist.py` (ранее) |
| Tests | `tests/test_mlbb_hud_signals.py`, `test_mlbb_channel_blocklist.py` |

### PUBG UI assets

Layout + crops: `PUBG_UI_REFS_ROOT` (default `/root/datasets/pubg/ui_refs/`):

```
layout.json   — ROI minimap, joystick, kill_feed, health, player_count
frames/       — reference screenshots
crops/        — нарезанные зоны HUD
index.json
```

Запуск: `python3 pubg_ui_refs_download.py` (на сервере после deploy).

---

## 5. Готовность к переезду

| Компонент | Статус |
|-----------|--------|
| MLBB worker + watchdog + health guard | ✅ в git |
| Steady mode + learning spam (~50/day) | ✅ env + код |
| Shorts ingest/feed/calibration | ✅ |
| VOD pipeline | ✅ в git, **выключен** на prod (`MLBB_VOD_DISABLED=1`) |
| Jess No Limit block | ✅ |
| `telegram_upload_bot.py` | ❌ server-only — копировать rsync |
| `highlight_scorer`, `gameplay_gate`, `smart_video_editor` | ⚠️ частично в git, deploy копирует с branch |
| PUBG farm | ❌ только scoring libs |
| systemd | ❌ nohup+cron (работает, но не ideal) |

**Вердict:** переезд **~85%**. Обязательно rsync: `/root/data/mlbb/`, `/root/datasets/mlbb/`, `.video_bot.env`, `telegram_upload_bot.py`, exemplars.

---

## 6. Готовность к ночной работе

| Риск | Митигация |
|------|-----------|
| Пустая очередь | health guard starvation ingest + disk index |
| Steady feed блок | `MLBB_STEADY_MIN_SEND_PENDING=1`, force after 900s silence |
| Stale locks | watchdog */2 + health guard kill |
| yt-dlp 403 | steady 2-query ingest, proxy выключен |
| VOD spam | `MLBB_VOD_DISABLED=1` |
| Skin review channels | blocklist + NEGATIVE_TITLE |
| Disk full | **новый** disk check в health guard |
| Telegram bot dead | watchdog restarts `telegram_upload_bot.py` |

**Ожидание на ночь:** 2 Shorts / ~28 min ≈ **40–50 clips** если ingest находит fresh Shorts. Если YouTube 403 — меньше, recovery подтянет.

---

## 7. Roadmap (приоритет)

### Сейчас (обучение распознаванию)

1. ✅ Spam unedited Shorts с 👍/👎
2. ✅ Active learning + HUD soft rank
3. ⏳ Добрать до 100+ yes / 100+ no labels
4. ⏳ Batch retrain classifier (`mlbb_train_classifier.py`)

### Следующий слой (точность)

1. Подключить [mlbb-ai-clipper YOLO](https://huggingface.co/frendyrachman/mlbb-ai-clipper) для epic UI
2. Minimap HSV tracker (как mlbb_analyze_move) для teamfight density
3. OCR KDA top bar для ranked filter
4. `MLBB_REJECT_REPLAY=1` опционально после A/B на labels

### Montage cuts (после стабильных Shorts 3+ дня)

1. Включить VOD kill-first (`MLBB_VOD_DISABLED=0`)
2. Fight bounds 7–22s (`mlbb_fight_segment.py`)
3. Opening trim `MLBB_SHORTS_TRIM_OPENING=1` для чище labels

### PUBG

1. ✅ UI refs layout downloaded
2. Commit `pubg_combat_gate` в repo
3. Mirror worker: `pubg_youtube_ingest` + calibration feed
4. Roboflow dataset fine-tune для kill feed

---

## 8. Идеи «выжать максимум»

- **YOLO epic UI** — один inference ≈ definitive Savage/Maniac
- **Scene library** — все 👍 в `scene_library_index.jsonl` для retrain
- **Hero refs** — уже 30 героев, блок skin showcase
- **Virality не цель** — для montage важнее *clean combat window*, не views
- **Ranked title bias** — search queries уже mythic/ranked heavy
- **MLLM captions** (offline) — semantic tags для search в archive
- **Cookies** `YOUTUBE_COOKIES_FILE` если 403 участится

---

## 9. Команды для утренней проверки

```bash
# Health
PYTHONPATH=/usr/local/bin python3 /usr/local/bin/mlbb_health_guard.py --check

# Stats
PYTHONPATH=/usr/local/bin python3 -c "from mlbb_calibration_store import stats; print(stats())"

# Logs
tail -50 /root/data/mlbb/logs/mlbb_continuous_watchdog.log
tail -30 /root/data/mlbb/mlbb_continuous_worker.log

# PUBG UI refs
ls -la /root/datasets/pubg/ui_refs/
```
