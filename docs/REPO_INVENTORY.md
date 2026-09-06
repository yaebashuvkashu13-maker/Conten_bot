# Repository inventory (PUBG / MLBB VOD focus)

What is **active on VPS** vs **dormant but kept** for multi-game / future work.
Do not delete dormant paths without owner sign-off.

## Active (production)

| Path | Role |
|------|------|
| `scripts/deploy_unified_production.sh` | **Only** supported prod deploy (systemd feed + env pin + cron purge) |
| `scripts/shooter_vod_segment_feed.py` | Main PUBG loop: discover → scan → render → presend → Telegram |
| `scripts/mlbb_vod_segment_feed.sh` | Supervisor wrapper (flock; sole owner under systemd) |
| `scripts/content_bot_vod_feed.service` | systemd unit (`Restart=on-failure`) |
| `scripts/daily_cycle_runner.py` | Game dispatcher (PUBG-only when pinned) |
| `scripts/vod_hang_detector.py` | Hang tick + drought soften (no menu/presend bypass) |
| `scripts/vod_feed_owner_health.py` | Single-owner / TG alert health |
| `scripts/vod_telegram_env.py` | TG_* / TELEGRAM_* credential resolve for ops alerts |
| `scripts/telegram_upload_bot.py` | Owner bot + HQ file |

Compat wrappers (delegate to unified deploy only):

| Path | Role |
|------|------|
| `scripts/install_mlbb_vod_only.sh` | DEPRECATED → `deploy_unified_production.sh` |
| `scripts/vps_apply_vod_only.sh` | git pull unified branch + unified deploy |

## Dormant (multi-game / Shorts; must not fight systemd VOD)

| Path | Role |
|------|------|
| `scripts/smart_video_editor.py` | Shared motion/audio montage engine |
| `scripts/pubg_mlbb_pipeline.py` | 5-game overnight queue |
| `scripts/mlbb_continuous_worker.py` | Shorts calibration worker (paused) |
| `scripts/mlbb_continuous_worker_watchdog.sh` | Legacy Shorts watchdog (refuses when VOD unit present) |
| `scripts/mlbb_vod_health_watchdog.sh` | Legacy health tick (systemd restart only; cron purged on deploy) |
| `scripts/mlbb_emergency_restore.sh` | Shorts-era restore (REFUSED when VOD_ONLY / unit present) |
| `scripts/run_owner_then_feed.sh` | Owner batch → starts feed via **systemd only** |

## Data / labels (per-game, do not merge)

- `data/mlbb/vod_segment_labels.json` — owner 👍/👎 on VOD cuts
- `data/highlight_exemplars/{mobile_legends,pubg,standoff,...}/` — CLIP exemplars
- `pubg_owner_labels.json`, `standoff_owner_labels.json` — multi-game learning

## Ops invariants

- Deploy only via `bash scripts/deploy_unified_production.sh`
- Never re-enable `VOD_FORCE_PRESEND_BYPASS` / menu keepalive / slim feed
- Gun bypass only under drought soften (explicit `1`)
- Systemd is the sole feed owner; no parallel nohup supervisors unless `VOD_FEED_ALLOW_NOHUP=1`
