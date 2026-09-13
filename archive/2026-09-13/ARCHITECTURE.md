# Architecture — key file map

## Orchestration

| File | Role |
|------|------|
| `scripts/mlbb_vod_segment_feed.sh` | systemd entry; launches feed loop |
| `scripts/shooter_vod_segment_feed.py` | PUBG/shooter VOD feed: inbox → peaks → gates → send |
| `scripts/mlbb_vod_segment_feed.py` | MLBB-oriented feed helpers |
| `scripts/daily_game_cycle.py` | multi-game daily quotas |
| `scripts/vod_hang_detector.py` | stall/silence/reject-drought heal |
| `scripts/vps_disk_cleanup.sh` | disk watermark cleanup |

## PUBG moment pipeline

| File | Role |
|------|------|
| `scripts/youtube_download.py` | yt-dlp download + client/format fallbacks |
| `scripts/shooter_vod_fast_scan.py` | fast peak discovery |
| `scripts/vod_audio_batch.py` | batch audio / DSP |
| `scripts/pubg_fast_peak_rank.py` | cheap shortlist |
| `scripts/pubg_moment_ranker.py` | train/infer joblib ranker |
| `scripts/pubg_fight_segment.py` | fight window from gunfire clusters |
| `scripts/pubg_montage_bounds.py` | pre/post pad, single vs montage max |
| `scripts/pubg_clip_shape_gate.py` | reject run/loot/fight-at-end shapes |
| `scripts/pubg_shooting_gate.py` | gun coverage / combat evidence |
| `scripts/pubg_quality_score.py` | composite quality |
| `scripts/gameplay_gate.py` | generic gameplay/HUD checks |
| `scripts/dislike_reason_gates.py` | map owner dislike reasons → hard rejects |
| `scripts/clip_hook_gate.py` | opening-hook / menu ceiling |
| `scripts/smart_video_editor.py` | render/assemble clips |
| `scripts/shooter_owner_montage.py` | owner OK montage assemble |
| `scripts/pubg_owner_calibration.py` | calibration / probe labeling |
| `scripts/pubg_owner_probe8_batch.py` | 8s probe slices from OK parents |
| `scripts/vod_owner_learning.py` | persist ratings → label stores |
| `scripts/pubg_shorts_autolearn.py` | YouTube Shorts silver ranges |

## Telegram

| File | Role |
|------|------|
| `scripts/telegram_delivery.py` | sendVideo / documents |
| `scripts/telegram_upload_bot.py` | long-running bot process |
| `scripts/telegram_owner_controls.py` | inline buttons, /recover, ratings |

## Data paths (runtime)

```
/root/data/pubg/pubg_owner_labels.json
/root/data/pubg/pubg_moment_ranker.joblib
/root/data/pubg/vod_segment_labels.json
/root/data/pubg/vod_segment_state.json
/root/data/pubg/shorts_autolearn/
/root/data/vod_quality_ledger/pubg_clip_ledger.jsonl
/root/data/vod_adaptive_thresholds/pubg_sent_ranges.json
/root/data/mlbb/*
```

## Deploy note

Production often runs copies in `/usr/local/bin`. After pull, sync scripts there and restart `content-bot-vod-feed.service` + `telegram-upload-bot.service`.
