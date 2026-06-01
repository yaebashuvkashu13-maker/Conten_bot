# MLBB video pipeline (Smart Edit v1.1)

## Rules enforced

1. No repeated scenes (`/root/.smart_edit_segment_history.json`)
2. Final montage uses **3–4** scenes (`MIN_HIGHLIGHTS=3`, `MAX_HIGHLIGHTS=4`)
3. Duration **33–57** seconds (`MIN_FINAL_DURATION`, `MAX_FINAL_DURATION`)
4. Scenes end on quiet boundaries (Smart Edit peak/sustain detection)
5. Training downloads: **gameplay only** (`gameplay_gate.py` + CSV)
6. Montage segments: reject **non-gameplay** (cartoon/low HUD) and **on-screen text** (`segment_is_valid_for_montage` in Smart Edit)

**TikTok downloads** go to the **VPS only**: `/root/datasets/tiktok/mlbb/` (not the agent PC).
6. Goal: clips suitable for real viewers (engagement-weighted TikTok CSV)

## VPS paths

| Path | Purpose |
|------|---------|
| `/usr/local/bin/smart_video_editor.py` | Montage + Telegram |
| `/usr/local/bin/tiktok_download_batch.py` | Proxy TikTok download |
| `/usr/local/bin/mlbb_progress_report.py` | Hourly Telegram report |
| `/usr/local/bin/mlbb_hourly_cycle.sh` | Cron entrypoint |
| `/root/data/mlbb/*.csv` | Training tables |
| `/root/datasets/tiktok/mlbb/` | Downloaded gameplay |

## Burst download (paid proxy window)

See **[mlbb_parallel_burst.md](./mlbb_parallel_burst.md)** — mass harvest 4000–5000 clips + parallel Instagram/audio workers:

```bash
bash /usr/local/bin/run_parallel_stack.sh
```

## Cron

```cron
12 * * * * /usr/local/bin/mlbb_hourly_cycle.sh
```

## Env (`/root/.video_bot.env`)

- `TG_BOT_TOKEN`, `TG_CHAT_ID`
- `PROXY_URL` / `YTDLP_PROXY` for TikTok
- `MIN_FINAL_DURATION=33`, `MAX_FINAL_DURATION=57`
- `MIN_HIGHLIGHTS=3`, `MAX_HIGHLIGHTS=4`
