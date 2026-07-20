# WoT Blitz — YouTube discovery & moment detection

## Длительность (аудит 2026-07-20, 120 результатов)

| Метрика | Значение |
|---------|----------|
| Медиана | **6.4 мин** |
| P25 / P75 | 4.6 / 12.8 мин |
| Среднее | 23.4 мин (из-за стримов) |
| Бакет 4–10 мин | **54%** — лучший пул |
| > 60 мин | 11.7% — стримы/турниры, отсекаем |

**Матч в игре:** 3–7 мин (макс 7). Источник: Wargaming, ACM study.

## Рекомендуемые пороги (env)

```bash
# Скачивание VOD
WOT_VOD_MIN_SEC=120          # было 180 (MLBB default) — ловим короткие ace/clutch
WOT_VOD_MAX_SEC=1500         # 25 мин — 2-3 матча, не 3-часовые стримы
WOT_VOD_TARGET_DUR_SEC=390   # ранжирование: ~6.5 мин (1 матч + интро)

# YouTube search
WOT_VOD_YOUTUBE_DURATION_FILTER=1
WOT_VOD_YOUTUBE_DURATION_SP=EgQQARgB   # 4–20 min (YouTube UI)

# Fast probe (до полного скана)
WOT_VOD_FAST_SKIP_INTRO=45   # было 90 — матч короче MLBB
WOT_VOD_FAST_IMPACT_MIN=0.08
```

## База запросов

Файл: `data/wot/youtube_query_bank.yaml`

- **core** — ranked/rating/supremacy full match
- **combat** — ace, clutch, frag, damage
- **angle** — классы танков, mad games (матч, не гайд)
- **reject** — LIVE, stream, tournament, guide, shorts

Ротация: `youtube_extended_vod_prefs.vod_discovery_search_cycle()`.

## Поиск моментов (мировая практика)

| Сигнал | Метод в проекте |
|--------|-----------------|
| Выстрел / взрыв (audio) | PANNs gunshot + explosion (`highlight_scorer`) |
| Hit flash / impact (visual) | `wot_brawl_segment`, `strict_segment_gate` |
| Kill feed (OCR) | backlog — Intel case study для WoT PC |
| Отсев garage/cruise | `gameplay_gate`, `WOT_BRAWL_GATE` |
| Популярные паттерны | CLIP exemplars + `config/highlight_queries.yaml` |

## Аудит

```bash
python3 scripts/wot_youtube_duration_audit.py --env /root/.video_bot.env \
  --out /root/data/wot/youtube_duration_audit.json
```

## Что НЕ качать

- Стримы > 60 мин (`LIVE`, `stream`, `chill chatting`)
- Турнирные трансляции (`grand final`, `summer cup`)
- Гайды / обзоры танков / Mad Games ability showcase
