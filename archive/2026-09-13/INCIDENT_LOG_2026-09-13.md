# INCIDENT_LOG — 2026-09-13 (день паузы / архива)

## Контекст

Владелец останавливает проект ~на 6 месяцев; подписка VPS заканчивается. Нужен полный перенос на GitHub без потери обучения.

## Таймлайн

1. Прод-ветка `cursor/pubg-cut-quality-probe-cap-a016`: незапушенные фиксы hang/drought/short-fight/low-gun loot + dirty calibration/shape/ranker.
2. Расхождение с origin (ahead/behind): remote — probe8 dedupe / flat labels / longer tails; local — anti-hang + short shootouts.
3. Merge origin→local; конфликт в `pubg_montage_bounds.py`. **Решено в пользу прод:** 0.8/3.0, max 18/20.
4. Закоммичены prod-правки + `youtube_download.py` из `/usr/local/bin` + `pubg_owner_probe8_batch.py`.
5. Архив `/root/backups/content_bot_archive_20260913` + tar.gz learning (~1.7M).
6. Secrets не коммитились; `ENV.example` redacted.
7. Push через GitHub MCP → **main** (на сервере нет git creds; gh на box не залогинен).
8. systemd не останавливали для копирования.

## На момент паузы

- `content-bot-pubg-shorts-autolearn.service` — **failed**
- Feed/Telegram — **active**
- pubg data ~11G (media); в git только learning JSON/joblib

## Коммиты контура

| SHA | Суть |
|-----|------|
| ffb105d | hang: silence classify + backoff |
| 8900135 | tighten fight windows ~6–12s |
| f50793b | anti-hang heartbeats + stage timeouts |
| 4b3a536 | ffmpeg timeout только на encode |
| da82a6b | reject silent low-gun loot |
| abb0ec2 | archive uncommitted prod drift |
| 8d8e780 | merge remote probe8; keep short-fight defaults |
