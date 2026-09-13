# Content Bot — Project Handoff (2026-09-13)

**Status:** Owner pausing ~6 months. VPS subscription ending. Canonical snapshot below.  
**Repo:** https://github.com/yaebashuvkashu13-maker/Conten_bot  
**Production git tip (server):** `8d8e7808f6b43ea1d64dee4380c6a5cd9188409e` on branch `cursor/pubg-cut-quality-probe-cap-a016`  
**Default branch to resume from:** `main` (this archive landed here) + production scripts under `archive/2026-09-13/code_snapshot/` and synced `scripts/`.

## What the bot does (end-to-end)

1. **Discover / download VODs** — YouTube (yt-dlp) search for PUBG Metro Royale (and historically MLBB etc.). Inbox under `/root/data/...` media dirs. Script: `youtube_download.py`, feed orchestration in `shooter_vod_segment_feed.py` / `mlbb_vod_segment_feed.sh`.
2. **Scan for combat peaks** — Audio DSP + PANNs gunshot cues + visual motion; caches peaks in `vod_segment_state`.
3. **Rank moments** — `pubg_moment_ranker.py` joblib model (`/root/data/pubg/pubg_moment_ranker.joblib`) scores candidates; trained from owner 👍/👎 labels.
4. **Segment / clip bounds** — `pubg_fight_segment.py`, `pubg_montage_bounds.py`: short shootouts (~5–12s gun), **short pre-roll**, **longer post-kill tail**.
5. **Quality gates (presend)** — `pubg_clip_shape_gate.py`, `pubg_shooting_gate.py`, `gameplay_gate.py`, `dislike_reason_gates.py`, low-gun loot reject, silence/false-gun classifiers. Rejects logged to `vod_quality_ledger/pubg_clip_ledger.jsonl`.
6. **Telegram delivery** — `telegram_delivery.py` / `telegram_upload_bot.py` → bot **programofloyalbot** to owner Anton. Inline 👍/👎 (+ reasons).
7. **Owner ratings → learning** — Labels land in `pubg_owner_labels.json` / MLBB equivalents; calibration feeds; adaptive `pubg_sent_ranges.json`; nightly/timer ranker retrain (`content-bot-pubg-ranker.timer`).
8. **Aux pipelines** — Shorts silver autolearn (`pubg_shorts_autolearn.py`), hang detector heal (`vod_hang_detector.py`), disk cleanup watermark.

## Production topology (VPS at pause)

| Piece | Path / unit |
|-------|-------------|
| Repo | `/root/content_bot_ml` |
| Secrets env | `/root/.video_bot.env` (**NEVER commit**) + optional `/root/.video_bot.drought.env` |
| Runtime mirrors | `/usr/local/bin/*.py` (often copied from `scripts/`; PYTHONPATH=`/root/content_bot_ml/scripts:/usr/local/bin`) |
| Learning data | `/root/data/pubg`, `/root/data/mlbb`, `/root/data/vod_quality_ledger`, `/root/data/vod_adaptive_thresholds` |
| Main feed | `content-bot-vod-feed.service` → `/usr/local/bin/mlbb_vod_segment_feed.sh` |
| Hang heal | `content-bot-vod-hang.timer` → `vod_hang_detector.py --tick --game pubg` |
| Disk cleanup | `content-bot-vod-cleanup.timer` → `vps_disk_cleanup.sh` |
| Telegram bot | `telegram-upload-bot.service` |
| Ranker train | `content-bot-pubg-ranker.timer` |
| Shorts learn | `content-bot-pubg-shorts-autolearn.timer` (was **failed** at archive time) |

Unit file copies: `archive/2026-09-13/systemd/`.

## Owner taste (summary)

- Russian-speaking owner; focus **PUBG Mobile Metro Royale**.
- Wants **short real shootouts** (≈5–12s), **longer END tail** (kill aftermath), **not** long loot/AFK/run intros.
- Hates silent low-gun “loot” clips and early-cut fights.
- See `OWNER_PREFERENCES.md`.

## Learning snapshot counts (at archive)

| Source | 👍 good | 👎 bad | notes |
|--------|---------|--------|-------|
| `pubg_owner_labels.json` | 434 | 666 | 1150 labeled events (+50 unknown) |
| `pubg/calibration_labels.json` | 22 | 72 | |
| `mlbb/mobile_legends_owner_labels.json` | 697 | 1036 | historical |
| `mlbb/calibration_labels.json` | 425 | 251 | |
| Ledger decisions | — | — | 4106 lines: reject 2156, heartbeat 1480, feedback 332, sent 134 |

## Commits preserved in this pause window (server, not all previously on origin)

```
8d8e780 merge: integrate remote probe8/label fixes; keep prod short-fight defaults
abb0ec2 chore(archive): preserve prod PUBG cut/calibration fixes before pause
da82a6b fix(pubg): reject silent low-gun loot before Telegram (DGso_775)
4b3a536 fix(feed): scope short ffmpeg timeout to clip encode paths
f50793b fix(feed): anti-hang heartbeat touches + stage timeouts + light advance
8900135 fix(pubg): tighten fight windows to short shootouts (~6–12s)
ffb105d fix(hang): classify silence and backoff on presend reject drought
```

Plus remote-side probe8/label commits merged in: `864ca4e` … `affe456`.

## Known failure modes & fixes already applied

| Failure | Symptom | Fix / location |
|---------|---------|----------------|
| Disk full | Downloads fail, feed stalls | `vps_disk_cleanup.sh` + timer; do not rely on keeping multi-GB VODs |
| Reject drought | Many presend rejects, zero Telegram sends | Hang detector silence classify + backoff; drought overlay env; combat-gated escape |
| Hang thrash | Feed stuck on one VOD / dead locks | `vod_hang_detector.py` tick; pin/unpin logic in feed; heartbeats |
| Long VODs | Slow scans, timeouts | Stage timeouts, ffmpeg timeout scoped to encode paths |
| Early-cut fights | Owner 👎 “ended early” | Montage bounds: longer post-kill; fight cluster extend while gun hot |
| Loot / run false kills | Silent low gun | `da82a6b` low-gun loot gate before Telegram |
| Branch divergence | Local vs origin probe8 | Merge kept **prod short defaults** (pre=0.8, single_max=18) over remote longer tails |

## What was NOT archived

- Raw multi-GB VOD `.mp4` files
- `/root/.video_bot.env` secrets (tokens/passwords)
- Large media caches: `viewport_cache/`, `like_gun_montages/`, `dislike_gun_montages/`, `ranker_features/`, shorts `vod_probes/`
- Highlight exemplar `.mp4` (listed in `EXCLUDED_MP4.txt` only)

## Resume pointer

Follow `RESTORE.md`. Learning pack: `archive/2026-09-13/learning_data/` + compressed blobs under `learning_compressed/`.
