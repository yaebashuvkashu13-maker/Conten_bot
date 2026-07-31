# MLBB hero portrait icons (killer banner matching)

Place square hero portraits here to verify **own-kill** banners:

```
mlbb_hero_icons/
  chou/icon.png
  gusion/icon.png
  hayabusa/default.png
  ...
```

The bot crops the **killer portrait** (right circle) from the kill-notification
strip and compares it to these icons. When the VOD title names a hero (e.g.
"Chou ranked gameplay"), only banners whose killer portrait matches that hero
are accepted — enemy kills and coordination popups are rejected.

Env:
- `MLBB_HERO_ICON_ROOT` — this directory
- `MLBB_BANNER_HERO_MATCH=1` — enable portrait check when icons exist
- `MLBB_BANNER_HERO_ICON_MIN_SIM=0.42` — match threshold

You can drop PNGs from the in-game hero select / wiki portraits (48×48 or larger).
