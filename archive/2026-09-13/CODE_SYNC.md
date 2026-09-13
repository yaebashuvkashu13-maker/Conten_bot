# CODE_SYNC — что запушено при архиве 2026-09-13

## Remotes

- URL: `https://github.com/yaebashuvkashu13-maker/Conten_bot.git`
- Target branch for handoff: **`main`**
- Long-running prod branch: `cursor/pubg-cut-quality-probe-cap-a016`
- Server HEAD at archive: **`8d8e7808f6b43ea1d64dee4380c6a5cd9188409e`**

## Unpushed → archived commits (server)

```
8d8e780 merge: integrate remote probe8/label fixes; keep prod short-fight defaults
abb0ec2 chore(archive): preserve prod PUBG cut/calibration fixes before pause
da82a6b fix(pubg): reject silent low-gun loot before Telegram (DGso_775)
4b3a536 fix(feed): scope short ffmpeg timeout to clip encode paths
f50793b fix(feed): anti-hang heartbeat touches + stage timeouts + light advance
8900135 fix(pubg): tighten fight windows to short shootouts (~6–12s)
ffb105d fix(hang): classify silence and backoff on presend reject drought
```

## Files synced from server tip into repo (scripts/tests)

See tree under `scripts/` updates from server + `archive/2026-09-13/code_snapshot/`.

Notable: `vod_hang_detector.py`, `pubg_montage_bounds.py`, `pubg_fight_segment.py`, `pubg_clip_shape_gate.py`, `pubg_moment_ranker.py`, `pubg_owner_calibration.py`, `pubg_owner_probe8_batch.py`, `youtube_download.py`, gates, feed, telegram controls, related tests.

## Bin sync

- Compared `/usr/local/bin` vs `scripts/`.
- Copied into git before commit: **`youtube_download.py`** (bin newer).
- Other key PUBG scripts: repo ≥ bin.

## Excluded from git

- `/root/.video_bot.env` and tokens
- `*.bak_earlyend_*` backup scripts
- Raw VODs / large montage mp4 caches
