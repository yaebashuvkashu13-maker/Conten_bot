# CODE_SYNC — 2026-09-13

- URL: `https://github.com/yaebashuvkashu13-maker/Conten_bot.git`
- Handoff branch: **main**
- Prod branch: `cursor/pubg-cut-quality-probe-cap-a016`
- Server HEAD: **`8d8e7808f6b43ea1d64dee4380c6a5cd9188409e`**

## Server commits archived

```
8d8e780 merge: integrate remote probe8/label fixes; keep prod short-fight defaults
abb0ec2 chore(archive): preserve prod PUBG cut/calibration fixes before pause
da82a6b fix(pubg): reject silent low-gun loot before Telegram (DGso_775)
4b3a536 fix(feed): scope short ffmpeg timeout to clip encode paths
f50793b fix(feed): anti-hang heartbeat touches + stage timeouts + light advance
8900135 fix(pubg): tighten fight windows to short shootouts (~6–12s)
ffb105d fix(hang): classify silence and backoff on presend reject drought
```

## Bin sync

Only `/usr/local/bin/youtube_download.py` was newer than repo — copied into git before commit.

## Excluded

- `/root/.video_bot.env` secrets
- `*.bak_earlyend_*`
- Raw VODs / large montage mp4 caches
