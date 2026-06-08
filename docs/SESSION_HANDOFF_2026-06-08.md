# Handoff: сессия 2026-06-08 (Cloud Agent)

> **Для нового чата:** прочитай этот файл + `docs/AGENT_HANDBOOK.md`.  
> **Быстрый старт:** скопируй промпт из раздела 12 в конец файла.

**Репозиторий:** https://github.com/yaebashuvkashu13-maker/Conten_bot  
**Рабочая ветка:** `cursor/mlbb-video-pipeline-e712`  
**PR:** #4 → `main` (draft)

---

## 1. Что хотел владелец (Антон)

| Задача | Статус |
|--------|--------|
| MLBB нарезки — качество, без залипания на интро | Частично: owner-label montage OK; auto-discovery дорабатывался |
| VK Callback API для сообщества MLBB | ✅ Подтверждён (`group_id=234820335`, code `c3de1fe9`) |
| **Очередь VK MLBB:** `/upload_vkmlbb` → 3 клипа × 3 раза/день (09:00, 13:30, 18:00 МСК) | ⚠️ Код + cron OK; токен manage-only — ждёт user OAuth |
| Пока бот плохо режет — владелец кидает свои клипы в ТГ, бот публикует в VK | ✅ Очередь работает (9 видео в pending) |
| PUBG ночной монтаж — не беготня, только стрельба | ✅ Ужесточены гейты (см. §5) |
| Товары к клипам VK | ❌ Отменено владельцем — «ерунда получится» (API не прикрепляет товары) |

**Владелец:** Telegram `@PMAntonShapkin`, chat в `TG_CHAT_ID`.  
**Бот:** `@programofloyalbot`.

---

## 2. Git: что сделано в этой ветке

### VK MLBB upload queue
| Файл | Назначение |
|------|------------|
| `scripts/vk_mlbb_queue.py` | Очередь `/root/data/mlbb/vk_mlbb_queue/pending` + mirror exemplars |
| `scripts/vk_mlbb_upload.py` | ffmpeg → 9:16 vertical ≤90s → `video.save` + upload |
| `scripts/vk_mlbb_publish_slot.py` | Batch 3, слоты morning/afternoon/evening, TG notify |
| `scripts/install_vk_mlbb_scheduler.sh` | Cron: 06:00 / 10:30 / 15:00 UTC (= 09/13:30/18 МСК) |
| `scripts/telegram_upload_bot.py` | `/upload_vkmlbb`, `_status`, `_done` — `BOT_VERSION=2026-06-08-vkmlbb-upload-v1` |
| `scripts/sync_vk_mlbb_token.sh` | Синк `VK_MLBB_ACCESS_TOKEN` из env → `/root/.video_bot.env` |

### VK Callback
| Файл | Назначение |
|------|------------|
| `scripts/vk_callback_webhook.py` | Confirmation + events на :8788 |
| `scripts/install_vk_public_endpoint.sh` | nginx :80 `/vk/callback` |
| `scripts/vk_nginx_proxy.conf` | Прокси конфиг |
| `scripts/install_vk_callback.sh` | Установка webhook + env |

HTTPS tunnel: **cloudflared** (`vk-cloudflared` systemd) — URL может меняться при рестарте trycloudflare.

### MLBB montage / highlight
- `owner_label_montage.py` — fast path по таймкодам владельца
- MLBB-specific gates в `visual_action_check.py`, `preview_gate.py`, `viral_scorer.py`, `montage_env.py`
- `highlight_scorer.py` — auto-discovery без owner anchors (`HIGHLIGHT_USE_OWNER_ANCHORS=0`)

### PUBG / preview fixes
- `smart_video_editor.py` — при блоке `sendVideo` шлёт owner preview (`segment_preview`)
- `strict_segment_gate.py` — PUBG через **полный** `pubg_combat_gate` (PANNs + visual)
- `visual_action_check.py` — `run_no_shots` (бег без вспышки/оружия)
- `pubg_shooting_gate.py` — выше пороги gun/burst, `owner_bad_window`
- `data/pubg_owner_labels.json` — bad метки на `zv3JymSZOb0` (4 таймкода)
- `scripts/resend_montage_preview.py` — досылка превью для застрявших монтажей

### Прочее
- `data/vk_game_products.json` — заготовка (не используется, владелец отказался от товаров)
- `scripts/vk_game_products.py` — helper (описание клипа, опционально)

---

## 3. VPS: текущее состояние

| Параметр | Значение |
|----------|----------|
| Repo path | `/root/content_bot_ml` |
| Branch на VPS | `cursor/mlbb-video-pipeline-e712` (после `git pull`) |
| Env | `/root/.video_bot.env` |
| Telegram bot | `telegram-upload-bot.service`, версия `2026-06-08-vkmlbb-upload-v1` |
| VK queue | `/root/data/mlbb/vk_mlbb_queue/pending/` — **9 mp4** (загружены владельцем) |
| VK cron | `vk_mlbb_publish_slot.sh` morning/afternoon/evening |

### Env на VPS (что есть / чего нет)

| Переменная | Статус |
|------------|--------|
| `TG_BOT_TOKEN`, `TG_CHAT_ID` | ✅ |
| `VK_MLBB_GROUP_ID=234820335` | ✅ |
| `VK_MLBB_CONFIRMATION=c3de1fe9` | ✅ |
| **`VK_MLBB_ACCESS_TOKEN`** | ⚠️ **Есть, но manage-only (Callback)** — `video.save` не работает, нужен user OAuth |
| `HTTP_PROXY` / `HTTPS_PROXY` | ✅ (для yt-dlp; Telegram API — **без прокси**) |

### Сервисы VK
- `vk-callback-webhook` — active
- `vk-cloudflared` — HTTPS tunnel
- `nginx` — :80 `/vk/callback`

### SSH для Cloud Agent
Секреты агента (если настроены): `SSH_HOST`, `SSH_USER`, `SSH_PASSWORD`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.  
**`VK_MLBB_ACCESS_TOKEN` в список секретов Cloud Agent НЕ входил** — поэтому агент его не видел.

---

## 4. VK MLBB: как пользоваться

### Владелец в Telegram
```
/upload_vkmlbb          — режим приёма видео (7 дней)
[кидает mp4 клипы]
/upload_vkmlbb_status   — сколько в очереди
/upload_vkmlbb_done     — выключить приём
```

### Автопубликация
- **09:00, 13:30, 18:00 МСК** — по **3 клипа** из очереди
- Пустая очередь → уведомление в ТГ
- Клип: вертикаль 1080×1920, max 90s (VK сам решает Clips feed — отдельного API флага нет)

### ⚠️ КРИТИЧНО: привязка токена к IP (error 5)

Владелец уже сталкивался с этим через другого разработчика:

```
vk api error 5: access_token was given to another ip address
```

**Токен с ПК / Cursor / дома ≠ токен для VPS.** Заливка идёт с IP сервера — токен нужно **выдать или обменять на VPS**, либо отключить «Защищённую авторизацию» в приложении VK.

**Полная инструкция:** `docs/VK_MLBB_TOKEN.md`  
**Проверка:** `scripts/vk_mlbb_token_check.py`  
**OAuth на VPS:** `scripts/vk_mlbb_oauth_token.py`

### После валидного токена с IP VPS (P0)
```bash
# На VPS:
python3 /usr/local/bin/vk_mlbb_token_check.py   # must print OK token_valid_from_this_host

# Тест 1 клипа:
set -a && source /root/.video_bot.env && set +a
python3 /usr/local/bin/vk_mlbb_publish_slot.py morning
```

---

## 5. PUBG: инцидент и фикс

**Проблема:** ночной батч написал «✅ PUBG готово», видео не пришло (блок `sendVideo` без превью), потом дослали — **мусор** (беготня без стрельбы).

**Причина:** сегменты прошли только **аудио** gunfire (ложные срабатывания). Полный `pubg_combat_gate` не был в `strict_segment_gate`.

**Фикс (задеплоен):**
- `strict_segment_gate` → `pubg_passes_combat_gate` (audio + PANNs ≥0.24 + visual 3/3 + hit_flash/weapon)
- Visual prefilter в `smart_video_editor` при `SMART_PUBG_STRICT_SHOOTING=1`
- `run_no_shots`, `run_fake_gun` эвристики
- Bad labels: `zv3JymSZOb0` @ 1794, 2140, 4976, 6308 сек
- Плохой ролик `pubg_metro_20260608_183207.mp4` → quarantine

**Workflow монтажа 5 игр:** всегда **preview** → `/approve_preview` → только потом `sendVideo`.

---

## 6. MLBB pipeline (фон)

- `pubg_mlbb_pipeline.py --resume` — auto-discovery на VOD без owner timestamps
- Баг «залипание на интро» — фикс в `highlight_scorer.py` (action peaks, skip 5 min, no owner vicinity)
- Owner-label montage на `E4Dsp53yvv4` — владелец **approve** один раз; seg2/seg3 (1930s, 2920s) — **reject** по запросу
- Exemplars: `data/highlight_exemplars/mobile_legends/good/` + `vk_owner_*.mp4` из очереди VK

---

## 7. Деплой

```bash
# Локально: push в cursor/mlbb-video-pipeline-e712

# На VPS:
cd /root/content_bot_ml && git pull --ff-only
bash scripts/deploy_telegram_bot.sh
bash scripts/install_vk_mlbb_scheduler.sh
systemctl restart telegram-upload-bot
```

GitHub Actions: `.github/workflows/deploy-vps.yml` (нужны secrets `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`).

Подробнее: `docs/vps-autodeploy.md`

---

## 8. Секреты для нового агента

Добавить в **Cursor Cloud Agent Secrets** (чтобы агент видел с первого сообщения):

| Secret | Обязательно |
|--------|-------------|
| `SSH_HOST`, `SSH_USER`, `SSH_PASSWORD` | ✅ SSH на VPS |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | ✅ |
| **`VK_MLBB_ACCESS_TOKEN`** | ✅ **P0 — без него VK не заливает** |

Опционально GitHub Actions: `VPS_SSH_KEY`, `VPS_REPO_PATH`.

---

## 9. P0 для следующего агента

1. ~~**Синхронизировать `VK_MLBB_ACCESS_TOKEN`** на VPS~~ ✅ Синхронизирован; **нужен user OAuth** (не Callback manage-only) → тестовая заливка 1 клипа
2. Подтвердить владельцу ссылку на VK-пост / формат клипа
3. MLBB auto-discovery: дождаться preview без owner timestamps или дожать hook threshold в discover loop
4. Стабильный HTTPS для VK callback (свой домен vs rotating trycloudflare)

## 10. P1 / backlog

- Genshin/WoT VODs + exemplars
- Reject seg2/seg3 MLBB (1930s, 2920s) — bad labels + remontage если не сделано
- Стабильный автодеплой без ручного SSH

---

## 11. Уроки из чата (не повторять)

| Ошибка | Как правильно |
|--------|----------------|
| Писать «готово» без видео в ТГ | Монтаж ≠ отправка; нужен preview или явный текст |
| PUBG только по audio gunfire | Всегда `pubg_combat_gate` |
| Telegram notify через curl с HTTP_PROXY | `ProxyHandler({})` или `telegram_curl_env()` |
| Секрет добавлен в Cursor, но не в Cloud Agent list | Агент не увидит — нужен новый чат **или** явная передача токена |
| Токен VK взят с ПК и вставлен на VPS | **error 5** — перевыпустить на VPS (`docs/VK_MLBB_TOKEN.md`) |
| Товары к VK клипам через API | Невозможно — только ручное прикрепление в приложении VK |

---

## 12. Промпт для нового чата (скопировать целиком)

```
Ты Cloud Agent для репозитория yaebashuvkashu13-maker/Conten_bot.

ПЕРВЫМ ДЕЛОМ прочитай:
- docs/SESSION_HANDOFF_2026-06-08.md  (контекст этой сессии)
- docs/AGENT_HANDBOOK.md              (общая архитектура)

Ветка: cursor/mlbb-video-pipeline-e712, PR #4 → main.
VPS: /root/content_bot_ml, env /root/.video_bot.env, SSH через секреты.

P0 СЕЙЧАС:
1) VK_MLBB_ACCESS_TOKEN должен быть в секретах — синхронизируй на VPS (scripts/sync_vk_mlbb_token.sh), сделай ТЕСТОВУЮ заливку 1 клипа из очереди (/root/data/mlbb/vk_mlbb_queue/pending/, там 9 видео). Расписание: 09:00/13:30/18:00 МСК по 3 шт. Команды ТГ: /upload_vkmlbb.
2) Не трогай товары VK — владелец отказался.

Контекст качества:
- PUBG: ужесточены гейты (pubg_combat_gate), bad labels на zv3JymSZOb0. Монтажи 5 игр только через /approve_preview.
- MLBB: owner uploads → VK queue; ботные нарезки пока слабые — владелец сам кидает клипы.
- VK callback подтверждён, group 234820335.

После деплоя: commit, push, SSH deploy, обнови PR. Пиши владельцу по-русски, кратко.
```

---

*Создано: 2026-06-08. Автор сессии: Cloud Agent (cursor/mlbb-video-pipeline-e712).*
