# Repository inventory (MLBB VOD focus)

What is **active on VPS** vs **dormant but kept** for multi-game / future work.
Do not delete dormant paths without owner sign-off.

## Active (production)

| Path | Role |
|------|------|
| `scripts/mlbb_vod_segment_feed.py` | Main loop: discover → scan → render → presend → Telegram |
| `scripts/mlbb_kill_banner.py` | OCR kill-banner anchor + presend verify |
| `scripts/mlbb_fight_segment.py` | Fight sustain window bounds |
| `scripts/highlight_scorer.py` | Stage1 + PANN + clip scoring |
| `scripts/youtube_mlbb_vod_prefs.py` | YouTube search queries + title rank |
| `scripts/mlbb_vod_segment_store.py` | Segment index, 👍/👎 labels |
| `scripts/telegram_upload_bot.py` | Owner bot + HQ file |
| `scripts/run_owner_then_feed.sh` | Owner 8-URL batch → base feed |
| `scripts/install_mlbb_vod_only.sh` | VPS install / env defaults |

## Dormant (multi-game, disabled by `install_mlbb_vod_only.sh`)

| Path | Role |
|------|------|
| `scripts/smart_video_editor.py` | Shared motion/audio montage engine |
| `scripts/pubg_mlbb_pipeline.py` | 5-game overnight queue |
| `scripts/pubg_combat_gate.py` | PUBG killfeed / combat presend |
| `scripts/morning_pubg_standoff_catchup.py` | PUBG + Standoff montages |
| `scripts/standoff_exemplar_ingest.py` | Standoff CLIP exemplars |
| `config/overnight_games.yaml` | 5-game discovery config |
| `scripts/mlbb_continuous_worker.py` | Shorts calibration worker (paused) |

## Data / labels (per-game, do not merge)

- `data/mlbb/vod_segment_labels.json` — owner 👍/👎 on VOD cuts
- `data/highlight_exemplars/{mobile_legends,pubg,standoff,...}/` — CLIP exemplars
- `pubg_owner_labels.json`, `standoff_owner_labels.json` — multi-game learning

## Ops scripts (safe to keep)

- `scripts/vps_apply_vod_only.sh` — git pull + light verify (cron)
- `scripts/mlbb_vod_health_watchdog.sh` — feed supervisor
- `scripts/mlbb_job_watchdog.py` — orphan process cleanup
