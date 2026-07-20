# Заливка клипов в YouTube / Instagram / TikTok

После нажатия **👍 Ок** в Telegram появляется кнопка **📤 В соцсети** → выбор площадки.

Код: `scripts/social_publish.py`. Лог: `/root/data/mlbb/publish/social_publish_log.jsonl`.  
Статус в боте: `/social_status`.

Секреты только в `/root/.video_bot.env` (не коммитить).

---

## 1. Общее

```bash
# /root/.video_bot.env
SOCIAL_PUBLISH_ENABLED=1
SOCIAL_YT_ENABLED=1
SOCIAL_IG_ENABLED=1
SOCIAL_TT_ENABLED=1
SOCIAL_VK_ENABLED=1

# опционально
SOCIAL_YT_PRIVACY=public          # public|unlisted|private
SOCIAL_TT_PRIVACY=PUBLIC_TO_EVERYONE
SOCIAL_PUBLIC_VIDEO_BASE=https://your.cdn.example/clips   # для Instagram, если resumable не сработает
```

Проверка на VPS:

```bash
python3 /usr/local/bin/social_publish.py status
# или
python3 /root/content_bot_ml/scripts/social_publish.py status
```

---

## 2. YouTube Shorts (самый простой путь)

Уже почти готово в репо (`youtube_oauth_setup.py`).

1. [Google Cloud Console](https://console.cloud.google.com/) → проект → **APIs & Services** → включить **YouTube Data API v3**.
2. **Credentials** → OAuth client ID (тип Desktop / Web с redirect `http://localhost:8080/`).
3. В `.video_bot.env`:

```bash
GOOGLE_OAUTH_CLIENT_ID=....apps.googleusercontent.com
GOOGLE_OAUTH_CLIENT_SECRET=...
GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8080/
# YOUTUBE_CHANNEL_ID=UC...   # опционально
```

4. На VPS один раз (нужен браузер + SSH-туннель на :8080):

```bash
ssh -L 8080:127.0.0.1:8080 root@YOUR_VPS
python3 /usr/local/bin/youtube_oauth_setup.py
```

Скрипт сохранит `GOOGLE_OAUTH_REFRESH_TOKEN` и `/root/youtube_oauth_token.json`.

5. Перезапуск бота: `systemctl restart telegram-upload-bot`.

---

## 3. Instagram Reels (Meta Graph API)

Нужен **Instagram Professional** (Business/Creator), привязанный к **Facebook Page**.

1. [Meta for Developers](https://developers.facebook.com/) → приложение → продукт **Instagram** / Graph API.
2. Права (scopes): `instagram_basic`, `instagram_content_publish`, `pages_show_list`, `pages_read_engagement` (и связанные page permissions).
3. Получить **Page Access Token** (долгоживущий) и **Instagram Business Account ID** (`IG_USER_ID`).

```bash
IG_ACCESS_TOKEN=EAAB...          # или FACEBOOK_PAGE_ACCESS_TOKEN=
IG_USER_ID=17814...              # Instagram business user id
# IG_GRAPH_API_VERSION=v21.0
```

Если бинарный resumable upload не пройдёт у Meta — выложи клипы на публичный HTTPS и укажи:

```bash
SOCIAL_PUBLIC_VIDEO_BASE=https://cdn.example.com/clips
```

Файл должен открываться как `{BASE}/{filename}`.

---

## 4. TikTok

1. [TikTok for Developers](https://developers.tiktok.com/) → создать приложение.
2. Подключить **Content Posting API** (Direct Post). Для продакшена обычно нужна audit/review приложения.
3. OAuth пользователя канала → `access_token` с правом публикации видео.

```bash
TIKTOK_ACCESS_TOKEN=act....
SOCIAL_TT_PRIVACY=PUBLIC_TO_EVERYONE
# для тестов без audit часто только SELF_ONLY
```

Пока приложение не прошло audit, TikTok часто разрешает только приватную заливку (`SELF_ONLY`).

---

## 5. VK (уже было)

Если есть `VK_MLBB_ACCESS_TOKEN` с правом **video** — кнопка VK тоже появится. См. `docs/VK_MLBB_TOKEN.md`.

---

## 6. Как пользоваться в боте

1. Получаешь клип → жмёшь **👍 Ок**.
2. Появляются **📁 HQ файл** и **📤 В соцсети**.
3. Выбираешь YouTube / Instagram / TikTok (/ VK).
4. Бот пишет результат (ссылка или id) или понятную ошибку про недостающие токены.

Ручная заливка файла:

```bash
python3 /usr/local/bin/social_publish.py upload \
  --platform youtube \
  --path /root/datasets/mlbb/vod_segments/seg_XXXX.mp4 \
  --game mlbb
```

---

## 7. Аналитика (следующий шаг)

Сейчас модуль **только публикует**. Подтянуть метрики можно отдельно:

| Площадка | API | Что даёт |
|----------|-----|----------|
| YouTube | YouTube Analytics API (+ OAuth scope `yt-analytics.readonly`) | views, watch time, CTR, audience |
| Instagram | Instagram Insights (`/media/{id}/insights`) | plays, reach, likes, comments, saves |
| TikTok | Video Query / Analytics API | views, likes, shares, avg watch |

Имеет смысл: после заливки сохранять `platform_id` в лог (уже пишется) → раз в день cron тянет insights → сводка в Telegram. Это отдельная задача; без доп. scopes/токенов сейчас не заработает.

---

## 8. Деплой

Скопировать на VPS `social_publish.py` в `/usr/local/bin/` и `/root/content_bot_ml/scripts/`, перезапустить `telegram-upload-bot`.
