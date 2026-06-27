# Вставка в серую консоль (без подчёркиваний в командах)

Если при вставке пропадают `_` и символы с Shift — **не копируйте длинные имена файлов**.

## Один раз: короткая ссылка на папку

```bash
ln -sf /root/content_bot_ml /root/b
```

Дальше только **`/root/b`** — без подчёркиваний.

## Запуск скачивания (одна строка, без `_` в команде)

```bash
cd /root/b && bash scripts/burst.sh
```

Имя **`burst.sh`** — без подчёркиваний.

## Проверка (цифры и точки)

```bash
find /root/datasets/tiktok/mlbb -name '*.mp4' | wc -l
```

```bash
tail -5 /root/data/mlbb/mass_download.log
```

## Если `burst.sh` нет

```bash
cd /root/b && git pull
```

Потом снова `bash scripts/burst.sh`.

## YouTube в Telegram-боте (обязательно после git pull)

```bash
cd /root/b && bash scripts/deploy_telegram_bot.sh
```

В боте: `/ping` — версия `youtube-v2`, `yt-dlp=ok`. Ссылка Shorts или `/yt https://youtube.com/shorts/...`

Если бот молчит: `/whoami` — скопируйте `chat_id` в `/root/.video_bot.env` как `TG_CHAT_ID=...`, снова `deploy_telegram_bot.sh`.
