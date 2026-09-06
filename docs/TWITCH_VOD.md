# Twitch VOD для PUBG Metro Royale

Бот может нарезать моменты из **архивов стримов Twitch** тем же пайплайном, что и YouTube VOD (dense PANNs → ×3 склейка → те же гейты качества). Скачивание идёт через **yt-dlp** — отдельный Twitch API не обязателен.

## Что нужно от вас

| Что | Обязательно? | Пример |
|-----|--------------|--------|
| Включить Twitch | да | `TWITCH_VOD_ENABLED=1` в `/root/.video_bot.env` |
| Список каналов | желательно | `TWITCH_PUBG_CHANNELS=by_owl,leva2k,Levinho` (логины из URL `twitch.tv/<login>`) |
| Фокус на Metro | да (контент) | Каналы, где реально играют Metro Royale, а не Just Chatting |
| Twitch Client ID / Secret | нет* | Нужны только если позже добавим Helix (поиск «последний VOD» без yt-dlp) |
| OAuth токен стримера | нет | Публичные архивы качаются без логина |
| Sub-only VOD | — | Такие записи **не скачаются** без подписки на канал |

\*Без API бот обходит страницы `twitch.tv/<login>/videos?filter=archives` — этого достаточно для популярных стримеров с открытыми записями.

## Включение на VPS

```bash
# В /root/.video_bot.env
TWITCH_VOD_ENABLED=1
TWITCH_PUBG_CHANNELS=by_owl,leva2k,Levinho,Paraboy,Jonathan_Gaming
TWITCH_VOD_SEARCH_BATCH=4
TWITCH_VOD_SEARCH_LIMIT=12
```

Затем:

```bash
CONTENT_BOT_REPO=/root/content_bot_ml bash /root/content_bot_ml/scripts/deploy_unified_production.sh
```

Архивы попадают в тот же inbox PUBG (`/root/data/pubg/youtube_nightly/inbox/`) с префиксом `tw_<video_id>.mp4` и обрабатываются `shooter_vod_segment_feed.py pubg`.

## Каналы по умолчанию (RU Metro, owner list)

`shifuwoe`, `aderrtheman`, `karat_pm`, `b1_kitty`, `zzzerbin`, `tw_lexa`, `tagav23`, `amazonka_aa`, `essko21`, `lada2oo`, `spulae111`

Полностью заменить: `TWITCH_PUBG_CHANNELS=login1,login2,...`

## Live vs VOD

| Режим | Сейчас | Комментарий |
|-------|--------|-------------|
| Архивы (VOD) | ✅ | Основной режим |
| Прямой эфир | ❌ | Live не качаем (зависания, нет полного файла) |

Захват live в будущем потребует отдельный recorder (streamlink/ffmpeg) и другой inbox — это не включено.

## Качество

Twitch VOD проходят **те же strict-гейты**, что YouTube:

- `VOD_PUBG_QUALITY_STRICT=1`
- ×3 склейка, Metro gate, combat presend на каждый кусок
- До `SHOOTER_VOD_MONTAGES_PER_VOD` склеек с одного файла за визит

## Проверка вручную

```bash
yt-dlp --flat-playlist --print "%(id)s|%(title)s|%(duration)s" \
  "https://www.twitch.tv/by_owl/videos?filter=archives&sort=time" | head
```

Если список пустой — канал без архивов или регион/возраст ограничения.
