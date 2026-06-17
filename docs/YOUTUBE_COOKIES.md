# YouTube cookies для yt-dlp (снять 403)

**Сам я cookies получить не могу** — нужен твой залогиненный аккаунт YouTube в браузере.

## Зачем

YouTube режет поиск с сервера (`HTTP 403 Forbidden`). С cookies yt-dlp ходит «как браузер» и снова находит Shorts.

## Что сделать (5 минут)

### 1. Установи расширение

В Chrome / Firefox:

- **Get cookies.txt LOCALLY** (рекомендуется), или  
- **cookies.txt** export

### 2. Экспорт

1. Открой [youtube.com](https://www.youtube.com) — будь залогинен.
2. Расширение → Export → сохрани файл `youtube_cookies.txt`.

### 3. Залей на сервер

```bash
scp youtube_cookies.txt root@ТВОЙ_СЕРВЕР:/root/.youtube_cookies.txt
chmod 600 /root/.youtube_cookies.txt
```

### 4. Включи в env

В `/root/.video_bot.env` добавь или раскомментируй:

```bash
YOUTUBE_COOKIES_FILE=/root/.youtube_cookies.txt
```

Перезапуск не обязателен — worker подхватит при следующем ingest.

## Проверка

```bash
yt-dlp --cookies /root/.youtube_cookies.txt "ytsearch3:mlbb savage #shorts" --flat-playlist
```

Если видишь список id — ок.

## Безопасность

- Файл = доступ к YouTube аккаунту. Не коммить в git.
- Раз в 1–2 месяца обновляй (сессия протухает).

## Ссылки на видео

**Не нужны.** Cookies + поисковые запросы (`mlbb savage`, каналы Betosky и т.д.) — бот сам ищет Shorts.

Ссылки пригодятся только если захочешь **whitelist каналов** — это отдельная настройка, не обязательна.
