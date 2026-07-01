# Автодеплой без ручной консоли

Агент в Cursor **не видит ваш VPS** напрямую. Чтобы **не печатать команды руками**, один раз настраивается автодеплой.

## Вариант A — GitHub Actions (рекомендуется)

После настройки: **любой push в репозиторий** → скрипты на сервере обновляются сами.

### Один раз (5–10 минут)

1. На VPS сгенерировать ключ **только для деплоя**:

```bash
ssh-keygen -t ed25519 -f /root/.deploy_key -N ""
cat /root/.deploy_key.pub >> /root/.ssh/authorized_keys
cat /root/.deploy_key
```

2. GitHub → репозиторий **Conten_bot** → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**:

| Secret | Значение |
|--------|----------|
| `VPS_HOST` | IP сервера |
| `VPS_USER` | `root` |
| `VPS_SSH_KEY` | содержимое `/root/.deploy_key` (приватный ключ) |
| `VPS_REPO_PATH` | `/root/content_bot_ml` (опционально) |

3. **Actions** → workflow **Deploy to VPS** → **Run workflow** (или сделайте push).

Дальше агент пушит в git — **вы в консоль не заходите**.

---

## Вариант B — cron на VPS (без GitHub)

Один раз вставить (серый экран — одна строка с `burst`):

```bash
(crontab -l 2>/dev/null; echo "*/30 * * * * /root/content_bot_ml/scripts/vps_auto_update.sh") | crontab -
```

Каждые 30 минут: `git pull` + `burst.sh` + рестарт бота.

---

## Что уже можно без консоли

| Задача | Кто делает |
|--------|------------|
| Код, скрипты | Агент → git push |
| Деплой на VPS | GitHub Action или cron |
| Прокси, env | Один раз в `/root/.video_bot.env` (или через панель хостинга) |
| Видео PUBG / реклама | Вы и коллега в Telegram |

---

## Почему сейчас приходилось писать в консоль

Пока **нет SSH-секрета в GitHub** и **не стоял cron**, сервер не был «подключён» к агенту. Это не ограничение «не хочу», а **нет канала до VPS**.

После секретов — пишу я через git, вы только Telegram.
