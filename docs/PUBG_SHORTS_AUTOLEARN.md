# PUBG Shorts Autolearn

Автономный watcher YouTube Shorts → таблица параметров → сужающиеся диапазоны → отчёт в Telegram каждые 100 роликов → опциональные пробы нарезки из VOD.

## Зачем

Silver-слой: бот сам смотрит популярные Metro Shorts и собирает те же числовые признаки, которыми режется длинный VOD (`gunfire_density`, `burst_ratio`, PANNs, motion, kill signals, …). Понимание кадра = уже существующий стек (PANNs + combat visual + kill OCR + CLIP/hook), а не «магическая» отдельная нейросеть с нуля.

## Данные

| Путь | Содержимое |
|------|------------|
| `/root/data/pubg/shorts_autolearn/features.jsonl` | все строки |
| `/root/data/pubg/shorts_autolearn/features.csv` | таблица для глаз |
| `/root/data/pubg/shorts_autolearn/silver_ranges.json` | текущий диапазон |
| `/root/data/pubg/shorts_autolearn/reports/` | тексты отчётов |
| `/root/datasets/pubg/shorts_autolearn/` | скачанные mp4 |

Диапазон стартует широким (q05–q95), после 100/300/600 Shorts сужается (forming → tightening → minimal).

## Антибан

## Платформы (параллельно)

| Источник | Как | Лимит |
|----------|-----|--------|
| YouTube Shorts + highlights | `content-bot-pubg-shorts-autolearn.timer` | 385/сутки |
| Локальный пул на диске | `…-local.timer` | без IP-лимита |
| **TikTok** | `content-bot-pubg-multi-harvest.timer` + proxy | ~4 за запуск |
| **Instagram Reels** | тот же harvest, нужен `/root/instagram_cookies.txt` | ~4 за запуск |
| **VK** | тот же harvest | ~2 за запуск |

Instagram без cookies **не стартует** (скажи — подскажу как выгрузить Netscape cookies).
Скачанное сразу попадает в локальный скорер → таблица параметров.

## Команды

```bash
python3 scripts/pubg_shorts_autolearn.py run --max 2
python3 scripts/pubg_shorts_autolearn.py status
python3 scripts/pubg_shorts_autolearn.py report --force
python3 scripts/pubg_shorts_autolearn.py probe-vod --count 3
```

## Systemd

```bash
install -m 0644 scripts/content_bot_pubg_shorts_autolearn.service /etc/systemd/system/
install -m 0644 scripts/content_bot_pubg_shorts_autolearn.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now content-bot-pubg-shorts-autolearn.timer
```

Env в `/root/.video_bot.env`: `PUBG_SHORTS_AUTOLEARN=1` (документально), `PUBG_SHORTS_REPORT_EVERY=100`, `PUBG_SHORTS_VOD_PROBE=1`.

## Важно

Silver-диапазоны **пока не** подменяют owner sent-ranges в прод-гейте. Сначала отчёты + твои 👍/👎 на пробах. Подключение к `PUBG_SENT_RANGE_GATE` — отдельным шагом после того, как пробы начнут попадать в цель.
