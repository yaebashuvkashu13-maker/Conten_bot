# INCIDENT_LOG — 2026-09-13 (день паузы / архива)

## Контекст

Владелец останавливает проект ~на 6 месяцев; подписка VPS заканчивается. Нужен полный перенос на GitHub без потери обучения.

## Таймлайн (сжатый)

1. **Прод-ветка** `cursor/pubg-cut-quality-probe-cap-a016` на сервере: незапушенные фиксы hang/drought/short-fight/low-gun loot + локальные dirty правки calibration/shape/ranker.
2. Обнаружено **расхождение** с `origin` (ahead/behind): на remote — probe8 dedupe / flat labels / longer fight tails; локально — anti-hang + short shootouts.
3. **Merge** `origin` → local; конфликт в `pubg_montage_bounds.py` (pre/post и max sec). **Решено в пользу прод:** короткие клипы (0.8/3.0, max 18/20).
4. Закоммичены uncommitted prod-правки + `youtube_download.py` из `/usr/local/bin` (единственный bin новее repo) + `pubg_owner_probe8_batch.py`.
5. Собран архив `/root/backups/content_bot_archive_20260913` + tar.gz learning (~1.7M compressed).
6. Secrets **не** коммитились; `ENV.example` с redacted placeholders.
7. Push на GitHub через **GitHub MCP** (на сервере нет git creds; `gh` на box не залогинен) → **`main`**.
8. systemd **не** останавливали для копирования.

## Известные проблемы на момент паузы

- `content-bot-pubg-shorts-autolearn.service` — **failed** (чинить при resume).
- Feed/Telegram — **active**.
- Диск: pubg data tree ~11G (много media/cache); в git только learning JSON/joblib.
- Main ветка до архива сильно отставала (июль 2026 merge); архив и актуальные scripts догружены поверх.

## Фиксы «сегодняшнего контура» (коммиты)

| SHA | Суть |
|-----|------|
| ffb105d | hang: silence classify + backoff на reject drought |
| 8900135 | tighten fight windows ~6–12s |
| f50793b | anti-hang heartbeats + stage timeouts |
| 4b3a536 | ffmpeg timeout только на encode |
| da82a6b | reject silent low-gun loot до Telegram |
| abb0ec2 | archive commit uncommitted prod drift |
| 8d8e780 | merge remote probe8; keep short-fight defaults |
