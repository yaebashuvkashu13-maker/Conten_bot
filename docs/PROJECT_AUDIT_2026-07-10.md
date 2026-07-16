# Аудит качества и производительности video highlight pipeline — 2026-07-10

## Краткий вывод

Главная проблема проекта — не нехватка разметки и не слабое железо. В production одновременно существуют несколько контуров обучения, но активный VOD-пайплайн обходит значительную часть накопленных сигналов:

- кэшированные пики считаются уже прошедшими правила и при `MLBB_VOD_SKIP_REVALIDATE=1` не пересчитываются после нового feedback;
- `MLBB_AUTO_TRAIN=0`, `MLBB_FEED_RESCORE=0`, owner anchors выключены;
- размеченные таймкоды почти не участвуют в поиске кандидатов;
- screenshot-калибровка kill banner отделена от общей оценки интересности сцены;
- adaptive fallback после серии пустых VOD отключает строгие banner-гейты и начинает пропускать motion-only сцены.

Поэтому добавление новых меток само по себе почти не меняет отправляемые клипы. Сначала необходимо замкнуть цикл `label → invalidate → train/calibrate → eval → deploy`.

## Что проверено

- Код и тесты ветки `main` на `d0438a3`.
- Production VPS в read-only режиме; никаких изменений на сервере не выполнялось.
- Production-код: `cursor/mlbb-banner-calibration-6cbd`, commit `6af0a7c`.
- Состояние, env-флаги, процессы, ресурсы, датасеты и агрегаты логов.
- Практики long-video understanding и gameplay highlight detection 2024–2026.

## Факты с production

| Сигнал | Значение |
|---|---:|
| CPU / RAM | 8 vCPU / 32 GB |
| Load average | 2.43 / 2.52 / 3.45 |
| Свободно на диске | 80 GB |
| Циклы, найденные в последних 100k строк лога | 387 |
| Циклы с `sent > 0` | 42 (10.9%) |
| Циклы с `sent = 0` | 345 (89.1%) |
| Отправлено клипов в этих циклах | 47 |
| Текущий `zero_cut_streak` | 7 |
| `adaptive soften` в последних 50k строк | 603 |
| Переиспользование cached peak pool | 424 |
| `fast-skip` | 33 |
| Good / bad exemplar mp4 | 493 / 331 |
| Owner timestamp labels | 26 good / 2 bad |
| Screenshot dataset | 149 banner crops |
| Общие reject screenshots | 0 |
| Обученные classifier/threshold artifacts | не найдены |
| VOD в registry / exhausted | 161 / 59 |

Ресурсы сервера не являются текущим узким местом: памяти и диска достаточно, средняя загрузка умеренная. Основная потеря скорости — повторная обработка неверных кандидатов, forced rerender и несогласованные cron/watchdog-процессы.

## Критические слабые места

### P0 — качество

1. **Feedback не применяется немедленно.** `vod_scan_state.minimal_pool_from_entry()` помечает кэш как `rule_pass=True`, а `MLBB_VOD_SKIP_REVALIDATE=1` разрешает не запускать повторный scorer. Новый dislike может не повлиять на тот же pool до истечения TTL (6 часов).
2. **Adaptive fallback оптимизирует количество, а не качество.** При серии нулей L1/L2 отключают обязательный banner, разрешают motion anchor, понижают score/motion thresholds и уменьшают gap до 28 секунд. На production streak=7 при threshold=4, то есть активируется L2. Это прямой источник «мусора».
3. **Разметка разорвана на несовместимые контуры.** VOD labels, Shorts labels, owner timestamps, banner screenshots и exemplars сохраняются раздельно. Production VOD path использует лишь часть этих данных.
4. **Обучение выключено, модели нет.** В env `MLBB_AUTO_TRAIN=0`, `MLBB_FEED_RESCORE=0`, `MLBB_USE_CLASSIFIER=1`; model artifacts отсутствуют. Логи показывают `classifier_low=0.500`, то есть нейтральный fallback принимается как реальный сигнал.
5. **Таймкоды почти не управляют inference.** `HIGHLIGHT_USE_OWNER_ANCHORS=0`; готовая функция owner kill anchors не подключена к основному поиску banner/peak.
6. **Скриншоты учат только узкую задачу.** 149 кадров лежат в `mlbb_kill_banners`, преимущественно как negatives. Это полезно для «есть ли нужная надпись», но не отвечает на вопрос «интересен ли весь клип». Общий `reject_examples` на VPS пуст.
7. **Нет честного baseline и holdout.** Нельзя доказать, что новая разметка улучшает precision/recall. Оценка на тех же примерах, на которых подбирался порог, создаёт ложный прогресс.
8. **Поиск может пропускать события.** Sparse fast probe способен исчерпать VOD без полного scan; top-N Stage1/PANN обрезает хвост; минимальный timestamp исключает ранние события; cached pool фиксирует старый набор кандидатов.
9. **Выбирается первый допустимый, а не лучший глобально.** `send-one` и ранние выходы исторически делали качество зависимым от порядка кандидатов. На production флаг уже выключен, но это требуется зафиксировать тестом.
10. **YouTube source discovery использует жёсткие фильтры и слишком быстро наказывает uploader.** Один zero-yield VOD не должен означать, что канал плохой. Это увеличивает пустые прогоны и снижает разнообразие.

### P0/P1 — корректность и надёжность

1. В `mlbb_vod_segment_feed._normalize_clip()` вызывается `detect_fight_bounds`, но функция не импортирована. Motion fallback может завершиться `NameError`.
2. JSON store для VOD labels/index/sent пишется без lock и atomic replace. Одновременный bot callback и feed могут потерять данные или повредить файл.
3. Telegram send фиксируется в durable state не сразу. Падение после успешной отправки, но до `mark_feed_sent`, создаёт дубль.
4. Main feed возвращает exit code 0 даже при полностью пустом запуске. Мониторинг не отличает успех от деградации.
5. `MLBB_FORCE_RERENDER=1` повторно кодирует уже готовые сегменты.
6. Presend повторяет дорогие freeze/OCR/visual проверки без versioned cache.
7. Fast probe может пометить целый VOD exhausted по нескольким sparse samples.
8. Watchdog регулярно убивает `mlbb_continuous_worker`; параллельно cron продолжает его запускать. Это лишняя нагрузка и риск гонок.
9. Сервисы systemd не активны, процессы запускаются shell/cron. Наблюдаемость и корректный restart сложнее.
10. Логи неструктурированы; нет агрегатов по reject reason, latency, cost и версии модели.

### P1 — состояние репозитория и deployment

1. Production branch на 93 commit впереди `main`. Аудит `main` не полностью описывает реально исполняемый код.
2. GitHub Actions при push на несколько веток всегда пытается checkout другой фиксированной ветки. Код, вызвавший deploy, и код на VPS могут отличаться.
3. Deploy выполняется как root; approval environment и минимальные permissions не заданы.
4. Документация содержит устаревшие ветки, режимы и противоречивые требования к banner gate.
5. Нет единого version manifest: commit, env hash, model version, dataset version и threshold version не записываются рядом с каждым решением.

### P1 — безопасность

Текущий VPS снаружи слушает только SSH, env имеет mode `0600`; nginx/n8n/webhook-сервисы не активны. Это хорошее текущее состояние. Но код содержит опасные dormant-конфигурации:

1. n8n nginx catch-all проксирует `/` на UI; при включении UI станет публичным по HTTP.
2. Webhook-сервисы bind на `0.0.0.0:8787/8788`.
3. VK confirmation string находится в репозитории и должен считаться скомпрометированным.
4. Deploy по SSH использует root и широкие branch triggers.
5. `.gitignore` не покрывает `.video_bot.env`, cookies, OAuth tokens, generated secret YAML и recovery journals.
6. Telegram verify передаёт token в URL процесса/лога.
7. Нет автоматического off-site backup labels, state, model artifacts и n8n volume.
8. JSON data stores не защищены от конкурентной записи.

## Архитектура, которая подходит этому проекту

### 1. Дешёвый high-recall candidate scan

Не пытаться сразу «понять» весь VOD тяжёлой моделью. Один раз построить дешёвую временную карту в 360p:

- audio RMS + onset/novelty;
- motion и scene change;
- изменение HUD/minimap/skill zones;
- OCR/event-banner confidence;
- YouTube Most Replayed, если heatmap существует;
- равномерные exploration-пробы по всей временной шкале.

Heatmap — только дополнительный weak signal: у новых и малопросматриваемых VOD её часто нет. Fast probe не имеет права exhaust целый VOD; при слабом сигнале он уменьшает приоритет, но сохраняет exploration budget.

Разделить VOD на временные buckets (например, 1–2 минуты), сохранить top-2 кандидата каждого bucket и затем сделать diversity-aware global top-K. Это почти не увеличивает compute, но резко снижает риск пропуска позднего боя.

### 2. Дорогой scorer только для 2–6% видео

Принципы FOCUS, QCA и LongVU применимы без переноса больших Video-LLM:

- распределять frame budget по сегментам динамически;
- больше кадров давать relevant и uncertain buckets;
- удалять почти одинаковые кадры по embedding similarity;
- сохранять временное разнообразие, а не брать только глобальный top-N.

Для этого проекта достаточно frozen image/audio embeddings + маленького classifier/ranker. Большая генеративная модель в hot path не нужна.

### 3. Две отдельные модели

Не смешивать:

1. **Event detector:** есть ли собственный Double/Triple/Maniac/Savage, teamfight, kill/death.
2. **Highlight ranker:** стоит ли отправлять клип владельцу/зрителю.

149 banner screenshots следует использовать для первого классификатора: frozen MobileNet/CLIP/DINO embedding + LogisticRegression/LightGBM, class balancing и split по VOD. Для ranker нужны clip-level good/bad и reasons.

### 4. Active learning вместо случайной разметки

Показывать владельцу не случайные кадры, а:

- uncertainty samples около decision threshold;
- disagreement между banner, motion, CLIP и audio;
- hard negatives, которые scorer хотел отправить;
- по одному diverse примеру из каждого visual cluster.

Каждый dislike должен немедленно:

1. записаться durable/atomic;
2. заблокировать окно ±60–90 секунд;
3. инвалидировать pool и feature/model decision cache этого VOD;
4. попасть в reason-specific hard-negative set;
5. инициировать лёгкий retrain/calibration;
6. пройти holdout eval до production deploy.

### 5. Source selection без пустых полных загрузок

До full download вычислять source score:

- title/channel/game/hero/rank confidence;
- duration и freshness;
- исторические yield, precision и owner approval данного uploader;
- thumbnail/HUD confidence;
- наличие heatmap;
- небольшой exploration bonus для новых источников.

Сначала скачивать 360p proxy + audio. Оригинальное качество загружать/рендерить только после нахождения временных интервалов. Не блокировать uploader после одного zero-yield VOD; использовать сглаженную статистику минимум по нескольким VOD.

### 6. Метрики и eval gate

Разделять train/validation **по VOD**, а не по соседним кадрам одного видео. Обязательные метрики:

- event recall на owner-good;
- false-pass rate на owner-bad;
- precision отправленных клипов;
- owner approval rate;
- event-level recall с tolerance window;
- доля пустых VOD;
- минуты CPU на час входного видео;
- кандидаты на каждой стадии и reject reasons;
- дубли и near-duplicates.

При текущей жалобе приоритет — precision: лучше честный `no clip`, чем filler. Начальная production gate: precision ≥ 0.85, bad false-pass ≤ 0.10, event recall ≥ 0.70 на VOD-level holdout. Threshold нельзя ослаблять из-за zero streak.

## Приоритет реализации

1. Запретить quality-degrading adaptive fallback; пустой результат считать нормальным.
2. Исправить import, atomic state и durable delivery.
3. Инвалидировать pool/cache при каждом feedback; убрать unconditional skip revalidation.
4. Собрать один manifest всех VOD labels, timestamp labels, screenshot labels и exemplars.
5. Построить VOD-level baseline и holdout; не менять thresholds до отчёта.
6. Обучить дешёвый banner classifier и clip ranker отдельно.
7. Заменить sparse exhaust на bucketed high-recall scan + exploration.
8. Добавить source score и 360p proxy analysis.
9. Версионировать model/config/cache и писать structured decision JSONL.
10. Только после shadow eval включать автоматическую отправку.

## Практики и источники

- FOCUS (ICLR 2026): training-free exploration/exploitation keyframe selection, менее 2% frames — https://github.com/Slezge/FOCUS
- QCA (2026): query/content-aware динамическое распределение frame budget — https://arxiv.org/abs/2607.00983
- LongVU (2024): temporal redundancy removal и query-guided compression — https://arxiv.org/abs/2410.17434
- SVHighlights / TF-SELECTOR: segment-level scoring для многочасовых видео — https://github.com/leedongkyu2019/SVHighlights
- Audio-visual recurrence for highlight detection (WACV 2025) — https://openaccess.thecvf.com/content/WACV2025/html/Islam_Unsupervised_Video_Highlight_Detection_by_Learning_from_Audio_and_Visual_WACV_2025_paper.html
- yt-dlp YouTube heatmap support — https://github.com/yt-dlp/yt-dlp/pull/7100

Эти работы не нужно копировать целиком. Для данного VPS оптимальна их общая идея: дешёвый high-recall temporal selection, ограниченный adaptive frame budget, frozen embeddings, маленький ranker и строгий human/eval gate.
