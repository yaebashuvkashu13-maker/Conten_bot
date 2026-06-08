# VK MLBB: токен и ошибка «another ip address»

## Симптом

```
vk api error 5: User authorization failed: access_token was given to another ip address
```

**Причина:** токен получен с одного IP (домашний ПК, браузер, Cursor), а `video.save` и заливка файла идут с **VPS** — другой IP. VK привязывает user/community token к IP выдачи.

Это **не баг нашего кода** — так работает защищённая авторизация VK.

---

## Что должно быть на одном IP

Весь цикл заливки с **IP VPS**:

1. `video.save` → получить `upload_url`
2. `curl -F video_file=@...` на `upload_url`
3. (опционально) `wall.post` / привязка к сообществу

Если токен выдан с ПК — любой шаг с VPS даст error 5.

---

## Решение A (рекомендуется): токен выдать с VPS

### Вариант A1 — OAuth на VPS (один раз)

1. В [приложении VK](https://dev.vk.com/) (Standalone): redirect URI = HTTPS вашего VPS, например  
   `https://ВАШ_ДОМЕН/vk/oauth/callback`  
   (можно временно cloudflared tunnel, как для callback API).

2. **На VPS** (SSH), не с ПК:

```bash
cd /root/content_bot_ml
python3 scripts/vk_mlbb_oauth_token.py --print-url
# Открыть URL в браузере (с любого устройства), войти, разрешить доступ
# После редиректа скопировать code=... из адресной строки

python3 scripts/vk_mlbb_oauth_token.py --code ВАШ_CODE
# Пишет VK_MLBB_ACCESS_TOKEN в /root/.video_bot.env
```

3. Проверка:

```bash
python3 /usr/local/bin/vk_mlbb_token_check.py
# OK: users.get / video.save dry-run с IP VPS
```

### Вариант A2 — ключ сообщества с VPS

Если в интерфейсе VK ключ создаётся в браузере с ПК — он **привязан к ПК**.

Обход: OAuth/community token через обмен **на сервере** (A1), либо отключить привязку к IP в настройках приложения (решение B).

---

## Решение B: отключить привязку к IP в приложении VK

В [dev.vk.com](https://dev.vk.com/) → ваше приложение → **Настройки**:

- снять **«Защищённая авторизация»** / привязку access_token к IP (если доступно для типа приложения)

После этого токен с ПК может работать с VPS. **Не все типы приложений** это позволяют — проверить `vk_mlbb_token_check.py` с VPS.

---

## Решение C: прокси (не рекомендуем)

Гонять **все** запросы к `api.vk.ru` и upload через **статический residential IP**, с которого когда-то взяли токен. Хрупко, дорого, ломается при смене IP.

---

## Проверка перед продакшеном

```bash
# На VPS:
set -a && source /root/.video_bot.env && set +a
python3 /usr/local/bin/vk_mlbb_token_check.py
```

| Результат | Действие |
|-----------|----------|
| `OK token_valid_from_this_host` | Можно заливать из cron |
| `error 5 ... another ip` | Перевыпустить токен по решению A или B |
| `error 15` / access denied | Не хватает scope: `video`, `groups`, `offline` |

**Scopes для заливки в сообщество:** `video`, `groups`, `wall` (по необходимости), лучше `offline` (без срока).

---

## Связь с очередью `/upload_vkmlbb`

Очередь и cron **уже на VPS** — как только токен валиден **с IP VPS**, слоты 09:00 / 13:30 / 18:00 МСК заработают без изменений кода.

См. также: `docs/SESSION_HANDOFF_2026-06-08.md`
