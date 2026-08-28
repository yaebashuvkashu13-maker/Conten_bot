# Дневной цикл: MLBB → PUBG → Standoff 2

## Логика

```
00:00 MSK ──► сброс счётчиков (Europe/Moscow)
         │
         ▼
    MLBB (10 клипов/день) ──► PUBG Metro Royale (10) ──► Standoff 2 (10) ──► idle до 00:00 MSK
```

| Этап | Скрипт | Квота (env) |
|------|--------|-------------|
| MLBB | `mlbb_vod_segment_feed.py` | `DAILY_MLBB_QUOTA=10` |
| PUBG | `shooter_vod_segment_feed.py pubg` | `DAILY_PUBG_QUOTA=10` |
| Standoff | `shooter_vod_segment_feed.py standoff` | `DAILY_STANDOFF_QUOTA=10` |

Диспетчер: `daily_cycle_runner.py` (вызывается из `mlbb_vod_segment_feed.sh`).

Состояние: `/root/data/mlbb/daily_game_cycle.json`

Включение: `DAILY_GAME_CYCLE_ENABLED=1` (ставится `install_mlbb_vod_only.sh`).

## PUBG-only (безлимит)

Для сервера только с PUBG Metro Royale:

```bash
VOD_PUBG_ONLY=1
DAILY_PUBG_QUOTA=-1          # безлимит (∞)
DAILY_MLBB_QUOTA=0
DAILY_STANDOFF_QUOTA=0
DAILY_GENSHIN_QUOTA=0
DAILY_WOT_QUOTA=0
SHOOTER_VOD_SEARCH_BATCH=10  # запросов за цикл
SHOOTER_VOD_SEARCH_LIMIT=80  # результатов на запрос
SHOOTER_VOD_PREFER_RUSSIAN=1
```

Маркер `/root/data/mlbb/EU_PUBG_ONLY` включает эти настройки через `install_mlbb_vod_only.sh`.

## PUBG: перестрелка от лица стримера (не фон)

Используем мультимодальный стек (уже в репо + новый `pubg_pov_engagement_ok`):

| Сигнал | Модуль | Зачем |
|--------|--------|-------|
| Gunfire density + burst ratio | `pubg_shooting_gate` | Есть стрельба, не тишина/бег |
| PANNs gun class | `highlight_scorer` | Звук выстрелов vs речь/музыка |
| Center motion | `gameplay_gate.score_segment_combat` | Движение прицела = POV engagement |
| Hit flash / weapon edge | `visual_action_check` | Вспышки, оружие в кадре |
| Gunfire по кварталам + кластеры | `_gunfire_pvp_shape` | Отсекает одиночный дальний перестрел |
| Killfeed OCR | `_pubg_killfeed_hits` | Подтверждение боя в UI |
| Bot farm / training reject | `pubg_rejects_bot_farm` | Полигон, PvE, односторонний спрей |
| **POV gate (новое)** | `pubg_pov_engagement_ok` | `gunfire высокий + motion низкий + 1 quarter` → reject |

### Research (интернет)

- **Audio-only ловит фоновые выстрелы** — исследования stream highlights (arXiv 1807.09715): in-game audio без face/view даёт много false positive action clips.
- **Мультимодальность точнее** — audio + visual + (опционально) transcript/reaction.
- **FPS HUD** — ammo decrement / crosshair region motion (recoil-analyser, valoscribe): POV-сигналы в центре viewport, не по краям.
- **X-CLIP / event prompts** (arXiv 2505.07721) — классификация клипов по семантике события; у нас аналог через PANNs + visual gates + owner labels.

### Env для тюнинга PUBG POV

```
PUBG_POV_GATE=1
PUBG_POV_MIN_CENTER_MOTION=0.028
PUBG_POV_MIN_GUN_FOR_MOTION=0.055
PUBG_POV_MIN_SPAN_RATIO=0.25
PUBG_PVP_MIN_ACTIVE_QUARTERS=2
PUBG_PVP_MIN_BURST_CLUSTERS=2
```

## Без поломки текущего MLBB

- `DAILY_GAME_CYCLE_ENABLED=0` → только MLBB feed (старое поведение).
- MLBB pipeline, env, exemplars, adaptive gate — без изменений логики скана.
- Добавлены только проверки квоты перед send.

## Деплой на EU

```bash
cd /root/content_bot_ml
git pull origin cursor/daily-multi-game-cycle-6cbd
export MLBB_VOD_INSTALL_RESTART_FEED=1
bash scripts/install_mlbb_vod_only.sh
```
