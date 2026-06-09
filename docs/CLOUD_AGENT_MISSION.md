# Cloud Agent Mission — Highlight Training System

> **Скопируй содержимое этого файла (или промпт ниже) в новый чат Cloud Agent.**

---

```
Ты Cloud Agent для репозитория yaebashuvkashu13-maker/Conten_bot.
ПЕРВЫМ ДЕЛОМ прочитай:
- docs/AGENT_HANDBOOK.md (§6 Viral Highlight Engine, §5 strict peak)
- docs/SESSION_HANDOFF_2026-06-08.md (контекст качества, без VK)
- docs/CLOUD_AGENT_MISSION.md (этот файл — полная миссия)

Ветка: cursor/mlbb-video-pipeline-e712, PR #4 → main.
VPS: /root/content_bot_ml, env /root/.video_bot.env.
Бот: @programofloyalbot, владелец @PMAntonShapkin (TG_CHAT_ID).

══════════════════════════════════════════════════════════════
МИССИЯ
══════════════════════════════════════════════════════════════
Построить систему, которая из длинных YouTube VOD находит те же типы
сцен, что зрители уже доказали популярностью в коротких роликах
(Shorts, TikTok, топ-клипы).
Цель — не «любое движение», а ПОПУЛЯРНЫЕ паттерны:

| Игра      | Что считать популярной сценой                          |
|-----------|--------------------------------------------------------|
| PUBG      | Автоматная перестрелка, burst fire, kill moment        |
| Standoff 2| Firefight, spray, multi-kill, не лобби/инвентарь       |
| MLBB      | Teamfight 5v5, ultimates, clutch, не фарм линии      |
| Genshin   | Boss fight с HP bar, elemental burst, не exploration    |
| WoT       | Выстрел танка + impact/explosion, не круиз по карте   |

Hard reject везде: беготня, лут, меню, лобби, AFK, map screen,
streamer talk без боя, intro/credits.

══════════════════════════════════════════════════════════════
СТРАТЕГИЯ ОБУЧЕНИЯ (мировая практика)
══════════════════════════════════════════════════════════════
Используй 4 источника сигнала в порядке надёжности:

  TIER 1 — GOLD (владелец)
    • Готовые mp4 «как надо»
    • Таймкоды good/bad на длинных VOD
    • /approve_preview и /reject_preview

  TIER 2 — SILVER (популярный контент)
    • YouTube Shorts / топ короткие ролики по игре (views, engagement)
    • TikTok top по игре (current_mlbb_ranked_videos.csv + mass download)
    • YouTube Most Replayed heatmap на длинных VOD (weak labels)

  TIER 3 — BRONZE (синтетика)
    • CLIP text queries из config/highlight_queries.yaml
    • PANNs audio classes (gunshot, machine_gun, explosion)
    • Motion/HUD heuristics (gameplay_gate.py)

  TIER 4 — HARD NEGATIVES (обязательно)
    • Owner bad timestamps
    • Rejected preview segments
    • «Почти прошло» окна рядом с bad labels (±30s)

Принцип: SILVER учит «что зрители любят», GOLD учит «что любит
владелец», HARD NEGATIVES учат «что НЕ слать». Без tier 4 система
вечно путает беготню с боем.

Методы (реализовать по порядку):
  1. EXEMPLAR RETRIEVAL (few-shot, самый быстрый ROI)
     — Нарезать 4–8s клипы из gold + silver → highlight_exemplars/
     — CLIP embedding bank: cosine(good) − cosine(bad)
     — k-NN retrieval при scoring (не только fixed 24 clips)

  2. WEAK SUPERVISION (YouTube + viral)
     — youtube_heatmap_peaks.py: Most Replayed → candidate starts
     — Новый: youtube_shorts_ingest.py — топ Shorts по game query
     — Из каждого viral short: hook frame, audio peak, duration, HUD profile
     — Сохранять в data/viral_patterns/{game}.jsonl

  3. CONTRASTIVE PAIRS (good vs bad на одном VOD)
     — Owner labels: good window vs bad window на том же видео
     — highlight_train.py: per-game classifier на 6–10 features
     — Feature diff: PANNs gun, CLIP score, motion, HUD delta, hook

  4. ACTIVE LEARNING LOOP (human-in-the-loop)
     — Preview → approve/reject → auto-append labels → retrain → eval
     — Не деплоить модель, пока eval не прошёл

  5. CALIBRATED THRESHOLDS (не magic numbers)
     — calibrated_pann_gun_min pattern для каждой игры
     — Пороги = перцентиль owner good минус margin до bad

  6. VIRAL STRUCTURE LEARNING (из коротких роликов)
     — viral_scorer.py: hook в первые 0.3–2s, payoff ≤12s, no menu start
     — Обучить hook_min per game из silver clips (median hook score топ-100)

══════════════════════════════════════════════════════════════
ФАЗА 0 — АУДИТ (день 1, без правок логики)
══════════════════════════════════════════════════════════════
1. Инвентаризация данных:
   - data/*_owner_labels.json — сколько good/bad per VOD per game
   - data/highlight_exemplars/{game}/{good,bad}/ — count на VPS
   - data/mlbb/current_mlbb_ranked_videos.csv — топ TikTok
   - /root/data/mlbb/youtube_nightly/inbox/ — какие VOD скачаны

2. Baseline eval (обязательный отчёт владельцу):
```bash
python3 scripts/score_owner_windows.py  # per labeled VOD
python3 scripts/eval_owner_labels.py --profile all
```
Метрики: recall@good, false_pass@bad, avg CLIP/PANNs on good vs bad
Зафиксировать baseline CSV: data/training/eval_baseline_{date}.csv
Не менять пороги, пока нет baseline.

══════════════════════════════════════════════════════════════
ФАЗА 1 — SILVER DATASET (вирусные короткие ролики)
══════════════════════════════════════════════════════════════
Создать: scripts/viral_reference_ingest.py

Для каждой игры (pubg, standoff, mobile_legends, genshin, wot):

A) YouTube Shorts / top clips
   - Поиск: "{game} highlights", "{game} best moments", game-specific
   - yt-dlp: top 50–100 по views за 90 дней, duration ≤60s
   - Фильтр: gameplay_gate.py is_gameplay=True
   - Сохранить: /root/datasets/viral_reference/{game}/*.mp4
   - Метаданные: views, likes, title → data/viral_reference/{game}.csv

B) TikTok (если прокси жив)
   - tiktok_mass_download.py / current_mlbb_ranked_videos.csv
   - Только gameplay rows из gameplay_filter_latest.csv

C) Feature extraction на каждом silver clip
   - PANNs peaks, CLIP embedding, hook score, duration, aspect ratio
   - HUD metrics via gameplay_gate
   - Запись: data/viral_reference/{game}_features.csv

D) Clustering популярных паттернов
   - K-means или HDBSCAN на CLIP embeddings (k=5–10 per game)
   - Центроид каждого кластера → «viral archetype»
   - Топ-3 клипа кластера → exemplars/{game}/good/viral_*.mp4

E) Negative mining из silver
   - Shorts с низким engagement И низким combat score → exemplars/bad/
   - Shorts с title "funny/meme/intro" → bad/

Cron: раз в неделю viral_reference_refresh.sh (не блокирует montage).

══════════════════════════════════════════════════════════════
ФАЗА 2 — GOLD DATASET (владелец + feedback loop)
══════════════════════════════════════════════════════════════
A) Ingest готовых видео от владельца (Telegram)
Команды (реализовать в telegram_upload_bot.py):
  /learn_start {game} — режим обучения 24h
  [владелец кидает mp4] — good exemplar
  /learn_bad — следующее видео = bad exemplar
  /learn_label 25:23 good — таймкод на текущем VOD
  /learn_label 35:40 bad
  /learn_done — bootstrap + train + eval report

При получении mp4:
  - gameplay_gate → reject non-gameplay
  - extract features → append viral_reference features CSV
  - copy → highlight_exemplars/{game}/good|bad/owner_{timestamp}.mp4
  - если режим learn + VOD в inbox → append owner_labels.json

B) Feedback от preview
  /approve_preview → optional good labels на segment centers
  /reject_preview → MANDATORY bad labels + bad exemplar cut
Файлы: segment_preview.py, highlight_bootstrap_exemplars.py

C) Bootstrap exemplars из всех labels
```bash
python3 scripts/highlight_bootstrap_exemplars.py --all-games
```
Минимум per game: 10 good, 5 bad (иначе CLIP unreliable)

══════════════════════════════════════════════════════════════
ФАЗА 3 — МОДЕЛЬ И SCORING (переобучение)
══════════════════════════════════════════════════════════════
A) Per-game training — scripts/highlight_train.py:
  --profile pubg|standoff|mobile_legends|genshin|wot|all
  --silver-csv data/viral_reference/{game}_features.csv
  --labels data/{game}_owner_labels.json

Output:
  - data/mlbb/highlight_classifier_{game}.joblib
  - data/mlbb/calibrated_thresholds_{game}.json

B) highlight_scorer.py (частично сделано в ветке e712):
  - classifier_missing НЕ блокирует rule_gate
  - HIGHLIGHT_SOFT_ANCHOR=1: owner good ±90s boost, bad ±60s exclude
  - Per-game classifier + calibrated thresholds
  - CLIP: k-NN over full exemplar bank (не cap 24)
  - Gate window = FULL segment duration

C) Per-game rule gates (не ослаблять без eval):
  PUBG/Standoff: pubg_combat_gate
  MLBB: minimap_delta + skill_delta + CLIP teamfight + hook
  Genshin: boss_bar + center_motion + CLIP boss query
  WoT: panns_explosion/artillery + CLIP tank query, reject cruise-only

D) viral_scorer.py — per-game hook_min из silver → viral_thresholds.json

══════════════════════════════════════════════════════════════
ФАЗА 4 — EVAL GATE
══════════════════════════════════════════════════════════════
Создать: scripts/eval_highlight_model.py

Pass criteria per game BEFORE deploy:
  recall@good ≥ 0.70
  precision@bad ≥ 0.80
  montage_segments_found ≥ 3 on ≥2 labeled VODs

CI: pytest + eval перед deploy.

══════════════════════════════════════════════════════════════
ФАЗА 5 — PRODUCTION MONTAGE (только после eval pass)
══════════════════════════════════════════════════════════════
Единый путь:
  VOD → discover_highlight_candidates → preview_gate → segment_preview
  → /approve_preview → sendVideo

Fallback: owner_label_montage.py если discovery пустой на labeled VOD.

Запрещено:
  - legacy rescue tiers для 5 игр
  - sendVideo без /approve_preview
  - «✅ готово» без preview в Telegram

══════════════════════════════════════════════════════════════
УЖЕ СДЕЛАНО В ВЕТКЕ e712 (не переделывать с нуля)
══════════════════════════════════════════════════════════════
- HIGHLIGHT_SOFT_ANCHOR=1 + bad exclude (highlight_scorer.py)
- highlight_train.py --profile all, per-game classifiers
- eval_owner_labels.py (baseline recall/bad_hits)
- pubg_combat_gate в strict_segment_gate
- /approve_preview в фоновом потоке (бот не зависает)
- MLBB bad labels 1930/2920 на E4Dsp53yvv4
- PUBG bad labels на zv3JymSZOb0

══════════════════════════════════════════════════════════════
ПЛАН ИТЕРАЦИЙ
══════════════════════════════════════════════════════════════
ИТЕРАЦИЯ 1: Фаза 0 baseline + viral_reference_ingest PUBG+MLBB + eval v1
ИТЕРАЦИЯ 2: /learn_* + reject→bad loop + eval pass PUBG+MLBB
ИТЕРАЦИЯ 3: viral ingest 5 игр + k-NN exemplars + calibrated thresholds
ИТЕРАЦИЯ 4: all games eval pass + docs/HIGHLIGHT_TRAINING.md

После КАЖДОЙ итерации: commit → push → SSH deploy → eval в Telegram → PR #4

══════════════════════════════════════════════════════════════
DEFINITION OF DONE
══════════════════════════════════════════════════════════════
eval_highlight_model.py --profile all → ALL PASS
2+ labeled VOD per game: discover ≥3 combat segments
Zero bad-label overlap в montage
Владелец approve ≥3 montage подряд без reject

══════════════════════════════════════════════════════════════
КОММУНИКАЦИЯ
══════════════════════════════════════════════════════════════
Пиши владельцу по-русски, кратко.
После каждой фазы: таблица eval (recall/precision per game).
При fail: конкретные time_sec где модель ошиблась.
Не обещать «готово» без eval JSON и preview в Telegram.

Начни с Фазы 0 (baseline eval) + Фазы 1 pilot (PUBG + MLBB viral ingest).

Самый быстрый старт: PUBG + MLBB — там уже labels, exemplars (30/29 good)
и VOD на VPS (n97cHIR9Qow, zv3JymSZOb0, E4Dsp53yvv4).
```

---

## Почему именно так

| Метод | Зачем |
|-------|--------|
| Silver (Shorts/TikTok) | Зрители проголосовали просмотрами — proxy популярной сцены |
| Gold (mp4 + таймкоды) | Калибрует silver под вкус владельца |
| Hard negatives | Без них CLIP путает бег с боем |
| Eval gate 70/80% | Нельзя «обучиться на словах» и слать мусор |
| Preview loop | Каждый reject = новый урок |
