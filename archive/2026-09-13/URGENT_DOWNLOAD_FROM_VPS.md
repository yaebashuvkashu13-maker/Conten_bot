# СРОЧНО — скачать с VPS до отключения

```bash
scp root@95.182.99.85:/root/backups/content_bot_learning_20260913.tar.gz .
scp root@95.182.99.85:/root/backups/content_bot_pause_20260913.bundle .
scp -r root@95.182.99.85:/root/backups/content_bot_archive_20260913 .
# проверить:
sha256sum content_bot_learning_20260913.tar.gz
# ожидание: 6cc768410cd7f2a5d1a7a4ad87aeab8d8a3ec96de0b923b47e1e145b6f464f04
```

Потом (с ноутбука, после `gh auth login`):

```bash
gh release create pause-2026-09-13 ./content_bot_learning_20260913.tar.gz \
  --repo yaebashuvkashu13-maker/Conten_bot \
  --title "Pause archive 2026-09-13" \
  --notes "Full learning data for 6-month resume"
```

Документы уже в `archive/2026-09-13/` на `main`. Полное дерево + learning parts также в локальном коммите сервера `f85dfd3` (не запушен — нет git creds на VPS).
