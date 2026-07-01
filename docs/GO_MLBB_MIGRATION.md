# MLBB Shorts pipeline — план переноса на Go

Документ для Go-разработчика. Цель: стабильный 24/7 пайплайн калибровки Shorts, **без потери Python ML**.

## Почему не полный rewrite

| Слой | Строк (порядок) | Go? |
|------|-----------------|-----|
| Оркестрация (worker, store, feed, bot HTTP) | ~8–12k | **Да** |
| CV/ML (OpenCV HUD, CLIP, sklearn train, PANNs) | ~15k+ | **Нет — Python service** |
| Shell/cron/deploy | ~80 скриптов | Заменить на systemd + один бинарник |

Полный перенос всего репозитория (~36k строк Python) = высокий риск и потеря 500+ exemplars/обучения.
Реалистичный путь: **Go orchestrator + Python scorer** (subprocess или HTTP).

## Что переносить в Go (фаза 1 — MLBB Shorts)

### Пакеты

```
cmd/mlbb-worker/          # один долгоживущий процесс вместо mlbb_continuous_worker.py
internal/store/           # calibration_labels, index, feed_sent, ever_delivered (JSON + flock)
internal/queue/           # pending, claim/release, owner_score sort
internal/ingest/          # YouTube search pool, yt-dlp subprocess, correspondence gate
internal/feed/            # batch pick, Telegram sendVideo
internal/correspondence/  # title ↔ MLBB query (порт mlbb_correspondence.go)
internal/config/          # .video_bot.env
```

### Источники для порта (Python → Go)

| Python | Go ответственность |
|--------|-------------------|
| `mlbb_calibration_store.py` | store + queue |
| `mlbb_continuous_worker.py` | worker loop, cooldowns |
| `mlbb_calibration_feed.py` | feed |
| `mlbb_correspondence.py` | correspondence (чистая логика) |
| `mlbb_youtube_shorts_ingest.py` | ingest orchestration (yt-dlp остаётся CLI) |
| `telegram_upload_bot.py` (только MLBB callbacks) | HTTP long-poll или webhook |

### Контракт с Python ML (`POST /mlbb/score` или `python3 -m mlbb_score`)

```json
// Request
{"path": "/root/datasets/mlbb/youtube_shorts/yt_xxx.mp4", "title": "...", "search_query": "mlbb savage shorts"}

// Response
{
  "gameplay_ok": true,
  "gameplay_score": 0.72,
  "owner_score": 0.15,
  "clip_score": 0.18,
  "reason": "window@3.0s"
}
```

```json
// POST /mlbb/train
{"profile": "mobile_legends"} → {"samples": 625, "accuracy": 0.58}

// POST /mlbb/rescore
{"limit": 50} → {"updated": 34}
```

Go **обязан** вызывать `rescore` + `cache/clear` после каждого 👍/👎 (синхронно, не fire-and-forget).

## Что оставить в Python

- `highlight_scorer.py` — CLIP exemplar, owner_score
- `highlight_train.py` — обучение на 👍/👎
- `gameplay_gate.py` — OpenCV HUD + MLBB window
- `highlight_scorer.clear_exemplar_cache()`

## Критичные инварианты (уроки из продакшена)

1. **Один сигнал ранжирования для send:** `owner_score` (CLIP vs exemplars), не путать с classifier.joblib.
2. **После каждого vote:** train → clear cache → rescore pending → только потом feed.
3. **Никогда:** `FAST_INGEST=1` (flat score 0.18), `OWNER_EMERGENCY=1`, `FEED_RE_GATE=0` в normal mode.
4. **Exemplars на диске:** `HIGHLIGHT_EXEMPLAR_ROOT` должен совпадать с `copy_exemplar` path.
5. **Correspondence до download:** результат поиска должен соответствовать MLBB-запросу.
6. **Без owner_score — не в очередь** когда `owner_rank` включён.

## Данные на VPS (не трогать при миграции)

```
/root/data/mlbb/calibration_labels.json      # 500+ votes
/root/data/mlbb/youtube_shorts_index.json
/root/data/mlbb/calibration_ever_delivered.json
/root/content_bot_ml/data/highlight_exemplars/mobile_legends/{good,bad}/
/root/datasets/mlbb/youtube_shorts/yt_*.mp4
```

Go store должен читать **тот же JSON формат** (обратная совместимость).

## Фазы

### Фаза 1 (минимальный рабочий Go)
- store + worker + feed + correspondence
- Python scorer как subprocess
- Telegram callbacks на 👍/👎 → sync learn cycle
- **Критерий готовности:** 15 Shorts/час, 0 non-MLBB, `learn=` в caption растёт после 👍

### Фаза 2
- HTTP scorer service, метрики, health
- Убрать shell wrappers

### Фаза 3 (опционально)
- smart_video_editor, VOD — только если Shorts стабильны

## Оценка объёма для одного Go-разработчика

| Фаза | Объём | Зависимости |
|------|-------|-------------|
| Фаза 1 | ~4–6k строк Go + тонкий Python wrapper | yt-dlp, ffmpeg, Telegram API |
| Фаза 2 | +2k Go, HTTP scorer | CLIP на CPU |
| Полный repo | 36k+ Python + риск регрессии | Не рекомендуется как первый шаг |

## Тесты приёмки

```bash
# owner_rank должен быть true
python3 -c "from mlbb_calibration_store import stats; print(stats())"

# после 👎 на non-MLBB — похожие не в pending
# caption содержит learn=0.xxx
# exemplars good+bad >= 50
```
