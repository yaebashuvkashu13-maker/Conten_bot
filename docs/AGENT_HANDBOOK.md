# Справочник проекта Conten_bot (для людей и AI-агентов)

> **Если вы новый агент:** сначала **`docs/SESSION_HANDOFF_2026-06-08.md`** (последняя сессия, P0, промпт), затем этот файл целиком.  
> После прочтения вы должны понимать: **что строим**, **что уже есть**, **где что лежит**, **что трогать нельзя**, **что делать дальше** — без 2 часов устных объяснений.

**Триггер для владельца:** *«Открой `docs/SESSION_HANDOFF_2026-06-08.md` + `docs/AGENT_HANDBOOK.md`»*.

---

## 1. Миссия проекта

**Полуавтономная → автономная** система контента по мобильным играм.

| Приоритет | Блок | Статус (на июнь 2026) |
|-----------|------|------------------------|
| **1** | Видео: нарезки, скоринг, обучение, Smart Edit → Telegram | **В работе (прод на VPS)** |
| **2** | Instagram: дайджест 12 блогеров, 19:00 МСК, текст на русском | Старый workflow в **n8n Cloud**; новый — в **git**, ещё не заменил Cloud |
| **3** | Ассистент покупателям в Telegram (прокачка, валюта) | Backlog |
| **4** | VPN + оплата | Только в roadmap, не трогать |

**Игры (видео):** сначала **MLBB / Hayabusa**, затем Genshin, PUBG, Standoff, WoT; цель — до **25 игр** и своя площадка услуг (далёкое будущее).

**Принцип стека:** оркестрация — **n8n** (по возможности) + тяжёлая логика — **Python на VPS** + конфиги в **git**. Секреты **не в git**.

---

## 2. Три мира: не путать

```text
┌─────────────────┐     ┌──────────────────────┐     ┌─────────────────┐
│  GitHub repo    │     │  VPS (root, Docker)   │     │  n8n Cloud      │
│  conten_bot     │     │  основной прод        │     │  kotletashop…   │
├─────────────────┤     ├──────────────────────┤     ├─────────────────┤
│ Python skeleton │     │ smart_video_editor   │     │ Instagram 19:00 │
│ configs, CSV    │     │ telegram_upload_bot  │     │ (выключить после│
│ scripts/        │     │ cron, datasets       │     │  миграции)      │
│ docs/           │     │ /root/.video_bot.env │     │ API на trial    │
└─────────────────┘     └──────────────────────┘     └─────────────────┘
```

| Вопрос | Ответ |
|--------|--------|
| Где живёт Smart Edit? | **VPS:** `/usr/local/bin/smart_video_editor.py` (копия в git: `scripts/smart_video_editor.py`) |
| Где Instagram в 19:00? | Сейчас **n8n Cloud** → бот в личку. В git: `config.instagram-mlbb.yaml` (12 блогеров), `content_bot/` — код для замены |
| Где секреты? | `/root/.video_bot.env`, Cloud Agent env, **не коммитить** |
| Репозиторий GitHub | `yaebashuvkashu13-maker/Conten_bot` (имя с большой C в UI) |

**Важно:** путь `e:\Users\...` с ПК владельца **агент на сервере не видит**. CSV/файлы — только через **git push**, **вложение в чат Cursor**, или **scp на VPS**.

---

## 3. VPS: что установлено

### 3.1 Сервисы

| Компонент | Путь / unit | Назначение |
|-----------|-------------|------------|
| Smart Edit | `/usr/local/bin/smart_video_editor.py` | Анализ → 3–4 сцены → склейка → Telegram |
| Telegram upload bot | `/usr/local/bin/telegram_upload_bot.py` | Приём видео, очередь, вызов Smart Edit |
| systemd | `telegram-upload-bot.service` | Бот всегда запущен |
| TikTok download | `/usr/local/bin/tiktok_download_batch.py` | Докачка по CSV через прокси |
| Gameplay filter | `/usr/local/bin/gameplay_gate.py` | Отсев промо/мемов |
| Hourly cycle | `/usr/local/bin/mlbb_hourly_cycle.sh` | download → hayabusa edit → отчёт |
| Progress report | `/usr/local/bin/mlbb_progress_report.py` | Текст + клип в Telegram |
| Hayabusa hourly | `/usr/local/bin/hourly_hayabusa_progress.sh` | Очередь из датасета → превью/видео |
| ML train | `/usr/local/bin/nightly_hayabusa_ml.sh` | `content_bot_ml.hero_classifier` |
| n8n Docker | контейнер `n8n-new` | Self-hosted n8n (мало данных) |

### 3.2 Cron (актуальный)

```cron
0 10,18 * * * /usr/local/bin/auto_video_bot.sh
40 * * * * /usr/local/bin/nightly_hayabusa_ml.sh
12 * * * * /usr/local/bin/mlbb_hourly_cycle.sh
```

`hourly_hayabusa_progress.sh` на **:05** убран — он вызывается **изнутри** `mlbb_hourly_cycle.sh`, чтобы не дублировать.

### 3.3 Данные на диске

| Путь | Содержимое |
|------|------------|
| `/root/videos/` | Готовые Smart Edit `.mp4` + `.json` |
| `/root/hourly_previews/` | Почасовые превью Hayabusa |
| `/root/hero_datasets/hayabusa/` | Positive-датасет для Hayabusa |
| `/root/telegram_uploads/pending/<chat_id>/` | Загрузки от пользователей бота |
| `/root/datasets/tiktok/mlbb/` | Скачанные TikTok (только gameplay) |
| `/root/data/mlbb/*.csv` | Таблицы обучения (копия из git `data/mlbb/`) |
| `/root/.smart_edit_segment_history.json` | Уже использованные сцены (анти-повтор) |
| `/root/data/mlbb/download_state.json` | Состояние докачки TikTok |

**Диск:** ~76 GB всего, обычно ~11 GB занято; автоочистка старых превью — желательна (см. backlog).

---

## 4. Telegram

### 4.1 Бот

- **Username:** `@programofloyalbot` (имя в UI: «Контент бот»)
- **Токен / chat:** `/root/.video_bot.env` → `TG_BOT_TOKEN`, `TG_CHAT_ID`

### 4.2 Пользователи и доступ

| Chat ID | Роль | Поведение |
|---------|------|-----------|
| `TG_CHAT_ID` в env | Владелец (Антон) | Загрузка видео + команда **`/make`** для сборки; почасовые **отчёты** |
| `6366727522` | Коллега (PUBG стрим) | `TG_ALLOWED_CHAT_IDS` + `AUTO_MAKE_CHAT_IDS` + **`LIMITED_NOTIFY_CHAT_IDS`** + **`PUBG_CHAT_IDS`** |

**Сообщения коллеге (`LIMITED_NOTIFY_CHAT_IDS`, по умолчанию = `AUTO_MAKE_CHAT_IDS`):**

- только **«Видео получено. Идёт нарезка…»** после загрузки;
- **готовый ролик** (`sendVideo` из Smart Edit);
- кратко **«Не удалось сделать нарезку»** при ошибке (детали — **владельцу**).

**Не получает:** почасовые отчёты, «Принял задачу», «завершен», `/status`, длинные ошибки.

Команды бота: `/start`, `/status`, `/clear`, `/make` — **полный набор только у владельца** (`TG_CHAT_ID`).

Формат очереди: `путь|метка|chat_id` — Smart Edit шлёт готовое видео **в chat_id из очереди**.

### 4.3 Подпись готового ролика

`Smart Edit v1.1 | Mobile Legends | 44s` — это **не отдельный продукт**, а caption из `smart_video_editor.py` (`profile` → `mobile_legends`).

---

## 5. Strict peak montage (5 игр) — единственный путь

С **июня 2026** для **MLBB, PUBG, Genshin, Standoff 2, WoT** в Telegram уходит **только** контент через **strict peak**:

| Компонент | Путь |
|-----------|------|
| Env | `montage_env.strict_peak_env(profile)` → `STRICT_PEAK_MONTAGE=1` |
| Гейт сегментов | `scripts/strict_segment_gate.py` + `pubg_shooting_gate.py` (PUBG) |
| Сборка | `strict_montage_direct.py`, `pubg_brawl_direct.py`, `investor_demo_batch.py`, `action_showcase_2x5.py` |
| Pre-send | Acceptance table в логе: `ALL_PASS=true` — **обязательно** перед `sendVideo` |

**Пороги strict (не ослаблять без явного запроса владельца):**

| Игра | Условие PASS |
|------|----------------|
| PUBG | **`pubg_combat_gate`**: audio + PANNs ≥0.24 + visual 3/3 + hit_flash/weapon; audio floor gun≥0.068, burst≥5.2 |
| Standoff | `gun >= 0.10`, `burst >= 8`, `motion >= 0.12` |
| Genshin | `boss_ok` + `motion >= 0.18`, `boss_score >= 0.35` |
| WoT / MLBB | `strict_segment_gate` + extra-reject (cruise / overlay) |

**Legacy / rescue / relaxed env** (`profile_montage_env`, `relaxed_montage_env`, rescue tiers в `smart_video_editor.py`) — **запрещены** для отправки 5 игр в Telegram.

- Включить legacy только с явным флагом: `ALLOW_LEGACY_MONTAGE_SEND=1` (ручной отладочный прогон, не cron).
- `send_telegram_video()` в `smart_video_editor.py` **блокирует** send для 5 профилей без `STRICT_PEAK_MONTAGE=1`.
- Watchdog cron перезапускает только `investor_demo_batch` и `action_showcase_2x5` (strict).

**Честный отказ:** лучше не слать ролик, чем слать filler. В логе — таблица сегментов с метриками и `ALL_PASS=false`.

---

## 6. Viral Highlight Engine + Smart Edit v1.1

### 6.1 Highlight Engine (PUBG → Standoff → MLBB → Genshin → WoT)

**Цель:** находить **интересные** моменты (стрельба, тимфайт, босс) — не бег/лут/меню.

| Стадия | Модуль | Что делает |
|--------|--------|------------|
| Stage0 | `youtube_heatmap_peaks.py` | YouTube Most Replayed — weak labels (top 20, gap ≥60s) |
| Stage1 | `intelliclip_scorer` + motion bins | Дешёвый скан пиков |
| Stage2 | PANNs (`panns-inference`) | gunshot / machine_gun / explosion |
| Stage3 | CLIP | exemplars `data/highlight_exemplars/{game}/` + `config/highlight_queries.yaml` |
| Stage4 | `rule_gate` + `visual_action_check` | AND-гейт: audio + clip + **независимый visual** |
| Stage5 | `viral_scorer.py` | hook (первые 0.3–2с), payoff timing, menu penalty |
| Stage6 | `select_montage_segments` | 3–4 сегмента, gap ≥90s, 33–57s |
| Stage7 | `segment_preview.py` | **только preview** → `sendVideo` после `/approve_preview` |

**Owner labels (`pubg_owner_labels.json`):** только train/calibrate (`highlight_train.py`, `calibrated_pann_gun_min`).  
**Inference:** `HIGHLIGHT_USE_OWNER_ANCHORS=0` (default) — **никогда** не подмешивать таймкоды владельца в `stage1_candidates`.

**Env (прод):**
```bash
HIGHLIGHT_SCORER=1
HIGHLIGHT_USE_OWNER_ANCHORS=0
OWNER_PREVIEW_REQUIRED=1
HIGHLIGHT_QUERY_CONFIG=/root/content_bot_ml/config/highlight_queries.yaml
```

**Viral rules:** первый кадр монтажа = action in motion (hook ≥0.42); сегмент 15–34s; montage 33–57s; trim start к первому gun spike (+3s max).

**Деплой:** `bash scripts/deploy_highlight_scorer.sh`

### 6.2 Smart Edit v1.1 — правила (обязательные)

Владелец зафиксировал **6 требований** — они уже заложены в код/env:

1. **Не повторять сцены** — `SEGMENT_HISTORY_FILE`, env `EXCLUDED_SEGMENT_KEYS`, логика в `hourly_hayabusa_progress.py`.
2. **3–4 сцены** в финале — `MIN_HIGHLIGHTS=3`, `MAX_HIGHLIGHTS=4` (сначала ищется комбо из **4**, потом 3).
3. **Длительность 33–57 с** — `MIN_FINAL_DURATION`, `MAX_FINAL_DURATION`.
4. **Сцена закончена** — детект пиков + «тихие» границы в `build_candidates()`.
5. **Учиться только на геймплее** — `gameplay_gate.py` + `gameplay_filter_latest.csv`; промо по тексту/эвристике отбрасывается.
6. **Контент для зрителей** — обучение на `current_mlbb_ranked_videos.csv` (score, views, likes); в будущем — API метрик площадок.

**Типичная поломка:** монтаж **собирается**, но **не уходит в Telegram** — была ошибка `curl sendVideo` (код 22). Исправлено: urllib multipart + retries в `send_telegram_video()`.

---

## 7. Обучение и CSV

### 7.1 Файлы в репозитории (`data/mlbb/`)

| Файл | Строк | Назначение |
|------|-------|------------|
| `current_mlbb_ranked_videos.csv` | ~806 | Топ TikTok MLBB: score, url, views, likes — **что «заходит»** |
| `gameplay_filter_latest.csv` | ~1923 | `is_gameplay`, HUD-метрики, пути к mp4 — **фильтр геймплея** |

На VPS: `/root/data/mlbb/` (те же имена).

**Проблема:** в CSV пути вида `datasets/tiktok/mlbb/....mp4` — на VPS файлы **появляются только после** `tiktok_download_batch.py` (прокси обязателен для TikTok).

### 7.2 Герои

| Этап | Герои |
|------|--------|
| Сейчас | **Hayabusa** (датасет `/root/hero_datasets/hayabusa`, nightly ML) |
| Далее | Lancelot, Gusion, Fanny, Ling, Chou, Pharsa, Yu Zhong, Franco, Kagura (+ Hayabusa) |

Цель модели: **узнавать героя** → **монтаж с одним героем** → по паттернам улучшать качество нарезок (генерация «с нуля» — отдельный далёкий этап).

### 7.3 ML в git vs на сервере

| Где | Пакет |
|-----|--------|
| Git `content_bot/` | Skeleton: `video_features`, `hero_classifier`, `tiktok_dataset` |
| VPS `/root/content_bot_ml/` | То же, используется cron ML |

---

## 8. Прокси (TikTok)

- Хранится в `/root/.video_bot.env`: `PROXY_URL`, `YTDLP_PROXY`, `HTTP_PROXY`, …
- **Не коммитить.** Владелец выдавал CyberYozh (HTTP + SOCKS5); срок обычно **24 ч**.
- Проверка: `yt-dlp --proxy "$YTDLP_PROXY" --print title '<tiktok url>'`

### 8.1 Burst: 4000–5000 роликов за окно прокси

Не только 805 URL из CSV — **`tiktok_mass_download.py`** качает каналы, хештеги и поиск MLBB (8 потоков).

```bash
bash scripts/deploy_parallel_burst.sh   # на VPS после git pull
bash /usr/local/bin/run_parallel_stack.sh
```

Параллельно: Instagram worker (тик), audio wav extract, индекс скринов рекламы `/root/data/mlbb/ad_examples/`.

**Скрины рекламы в Telegram (только владелец):** `/ad` → фото → `/ad_done` (алиас `/реклама`). Без `/ad` фото не путаются с видео для нарезки.

**PUBG для коллеги** (`PUBG_CHAT_IDS=6366727522` в `.video_bot.env`):

- Видео → нарезка с профилем `pubg` (без жёсткого MLBB HUD-фильтра).
- Параллельно: `pubg_stream_learn_worker.py` копирует в `/root/datasets/telegram/pubg/stream/` и пишет фичи в `/root/data/pubg/stream_features.csv`.

Подробно: `docs/mlbb_parallel_burst.md`. Пока идёт mass download, почасовой `tiktok_download_batch` **пропускается** (см. `mlbb_hourly_cycle.sh`).

Без прокси докачка с VPS часто падает; Hayabusa-датасет на диске **уже есть** (~1.9 GB в `/root/videos` и hero_datasets).

---

## 9. Instagram (приоритет 2)

### 9.1 Конфиг в git

`config.instagram-mlbb.yaml` — **12 блогеров** MLBB, `dry_run: true` в примере.

### 9.2 Прод сейчас

- **n8n Cloud:** `https://kotletashop123.app.n8n.cloud/...` (workflow Instagram, 19:00 МСК, ~7 постов).
- **Нужно:** владелец **выключает Active** в Cloud после запуска нового пайплайна из git — иначе **двойной дайджест**.

### 9.3 Требования к новому дайджесту

- Cookies Instagram (файл на VPS, не в git).
- Текст: **смысл на русском**, не копипаст.
- Фильтр рекламы — позже (владелец пришлёт примеры).
- Публикация в каналы — **следующий этап**.

---

## 10. Переменные окружения (`/root/.video_bot.env`)

**Шаблон (без значений):**

```bash
TG_BOT_TOKEN=...
TG_CHAT_ID=...
TG_ALLOWED_CHAT_IDS=ВАШ_CHAT_ID,6366727522
# ВАЖНО: владелец (TG_CHAT_ID) пускается всегда, но лучше явно добавить в ALLOWED
AUTO_MAKE_CHAT_IDS=6366727522
LIMITED_NOTIFY_CHAT_IDS=6366727522
PUBG_CHAT_IDS=6366727522
# или: CHAT_GAME_PROFILES=6366727522:pubg

PROXY_URL=http://user:pass@host:port
YTDLP_PROXY=...
HTTP_PROXY=...
HTTPS_PROXY=...

DEFAULT_GAME_PROFILE=mobile_legends
MIN_FINAL_DURATION=33
MAX_FINAL_DURATION=57
MIN_HIGHLIGHTS=3
MAX_HIGHLIGHTS=4
SMART_TARGET_DURATION=45
TRANSITION_DURATION=0.28
BLUR_NICKNAME=1
YTDLP_IMPERSONATE=chrome-131

# YouTube upload (OAuth, только на VPS — не коммитить)
GOOGLE_OAUTH_CLIENT_ID=....apps.googleusercontent.com
GOOGLE_OAUTH_CLIENT_SECRET=...
GOOGLE_OAUTH_REFRESH_TOKEN=...
GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8080/
YOUTUBE_CHANNEL_ID=...   # optional, UC...
```

Один раз: `python3 /usr/local/bin/youtube_oauth_setup.py` (нужен client secret + браузер/SSH tunnel на :8080).

При **401 Unauthorized** в Telegram — проверить, что `TG_BOT_TOKEN` не испорчен (только `cut -d= -f2-`, без лишних кавычек).

---

## 11. Репозиторий: структура

```text
conten_bot/
├── content_bot/           # Python-пакет (Instagram CLI, ML utilities)
├── scripts/               # Прод-скрипты (зеркало /usr/local/bin на VPS)
│   ├── smart_video_editor.py
│   ├── telegram_upload_bot.py
│   ├── tiktok_download_batch.py
│   ├── tiktok_mass_download.py
│   ├── run_parallel_stack.sh
│   ├── gameplay_gate.py
│   ├── mlbb_hourly_cycle.sh
│   └── mlbb_progress_report.py
├── data/mlbb/             # CSV для обучения (коммитятся)
├── config.instagram-mlbb.yaml
├── config.example.yaml
├── docs/
│   ├── AGENT_HANDBOOK.md  # ← этот файл
│   └── mlbb_video_pipeline.md
└── README.md
```

**Деплой на VPS:** tar/scp в `/usr/local/bin/` и `/root/data/mlbb/`, `chmod +x`, `systemctl restart telegram-upload-bot`, не перезаписывать токен битым base64 в одну строку SSH.

---

## 12. Почасовой цикл (что пишет владельцу)

`mlbb_hourly_cycle.sh` каждый час в **:12 UTC** (~:15 МСК):

1. `tiktok_download_batch.py --limit 45` → файлы на VPS `/root/datasets/tiktok/mlbb/`
2. `hourly_new_sources_montage.py` — нарезка **только из новых** (не старый `hero_datasets`)
3. `mlbb_progress_report.py --attach-latest-video`

**Источники нарезок (с июня 2026):**

| Откуда | Правило |
|--------|---------|
| Бот Telegram | Только **только что загруженное** видео (коллега) или свежие из очереди (`source_freshness.py`) |
| Почасовой цикл | TikTok, скачанные за последние ~36 ч + свежие upload, **не** использованные ранее |
| ~~Старый датасет~~ | `/root/hero_datasets/hayabusa` — **только для обучения ML**, не для нарезок в Telegram |

Отчёт: сколько скачано/отброшено, размер датасета, история сцен, место на диске.

---

## 13. Backlog (не делать без запроса)

- [ ] Новый Instagram-дайджест из `content_bot` + cron (замена n8n Cloud)
- [ ] Кнопка «Опубликовать» → YouTube, TikTok, Instagram, VK, Rutube, Дзен
- [ ] Ассистент продаж (эскалация на оплату)
- [ ] VPN + биллинг
- [ ] Автоочистка `/root/hourly_previews`, старых исходников
- [ ] Синхронизация `smart_video_editor.py` только через git (один source of truth)
- [ ] Удаление музыки, сохранение звука игры (FFmpeg / stem separation)
- [ ] 10 героев: отдельные папки датасета

---

## 14. Чеклист для нового агента

1. Прочитать **этот файл** и `docs/mlbb_video_pipeline.md`.
2. `git pull` — проверить `data/mlbb/*.csv`.
3. SSH на VPS — **не спрашивать** «где CSV на E:\».
4. Проверить: `systemctl status telegram-upload-bot`, `tail /root/data/mlbb/hourly_cycle.log`.
5. Не коммитить `.video_bot.env`, cookies, прокси-пароли.
6. Перед правкой Instagram — убедиться, что Cloud workflow **inactive**.
7. Любое изменение Smart Edit — тест `sendVideo` на малый файл.
8. Сообщать владельцу в **Telegram** (`TG_CHAT_ID`) при крупных этапах — он просил видеть прогресс.

---

## 15. Частые ошибки агентов (из реального чата)

| Ошибка | Правда |
|--------|--------|
| «Отправил CSV в чат» с путём `e:\...` | Агент **не видит** локальный диск |
| «76 GB занято видео» | **76 GB — размер диска**, занято ~11 GB |
| «Нет API n8n» | На **trial Cloud** API нет; есть **self-hosted** на VPS или ручной export JSON |
| «Smart Edit — отдельный сервис» | Это **наш скрипт** на VPS, подпись в Telegram |
| Дублировать cron :05 и :12 | Только **`mlbb_hourly_cycle.sh`** |
| Перезаписать `.video_bot.env` одной командой | Легко **сломать токен** — править точечно |

---

## 16. Контакты и доступы (мета)

- **Владелец:** Telegram `@PMAntonShapkin`, chat в `TG_CHAT_ID`.
- **Коллега с доступом к боту:** `6366727522`.
- **GitHub:** `yaebashuvkashu13-maker/Conten_bot`.
- **n8n Cloud:** `kotletashop123.app.n8n.cloud` (логин у владельца).

---

## 17. Одно предложение для эскалации

**Conten_bot** — это VPS-пайплайн **Smart Edit** (MLBB, 3–4 сцены, 33–57 с, без повторов) + Telegram-бот для загрузки + докачка TikTok по CSV через прокси + будущий Instagram из git; секреты на сервере, код в GitHub, n8n Cloud для Instagram пока устаревающий прод.

---

---

## 18. VK MLBB (июнь 2026)

Очередь клипов от владельца → автопубликация в сообщество VK.

| Компонент | Путь |
|-----------|------|
| Очередь | `/root/data/mlbb/vk_mlbb_queue/pending/` |
| Загрузка | `scripts/vk_mlbb_upload.py` (9:16, ≤90s) |
| Cron | `install_vk_mlbb_scheduler.sh` — 09:00 / 13:30 / 18:00 МСК, 3 шт. |
| Telegram | `/upload_vkmlbb`, `/upload_vkmlbb_status`, `/upload_vkmlbb_done` |
| Токен | `VK_MLBB_ACCESS_TOKEN` в `/root/.video_bot.env` — **обязателен**, **с IP VPS** (см. `docs/VK_MLBB_TOKEN.md`, error 5) |
| Callback | `vk_callback_webhook.py`, group `234820335` |

Подробности и P0: **`docs/SESSION_HANDOFF_2026-06-08.md`**.

---

*Последнее обновление справочника: 2026-06-08. При изменении cron, strict_peak порогов, chat ID или путей — обновляйте этот файл в том же PR.*
