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
