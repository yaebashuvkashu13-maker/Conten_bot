# MLBB kill-banner reference bank

Visual templates for kill-notification frames (wiki skins + crops from labeled VODs).

## Layout

```
mlbb_kill_banners/
  manifest.json          # index of all refs
  wiki/                  # notification frame previews (Fandom wiki)
  vod_crops/
    double/ triple/ maniac/ savage/ unknown/
```

## Build on VPS

```bash
# Wiki notification frames (safe, no VOD needed)
python3 scripts/mlbb_banner_ref_ingest.py --wiki

# Plus crops from owner-labeled kill timestamps (needs inbox VODs)
python3 scripts/mlbb_banner_ref_ingest.py --all --vod-root /root/data/mlbb/youtube_nightly/inbox
```

## Detection

When OCR fails but the top-center HUD patch matches a reference (`MLBB_BANNER_REF_MATCH=1`),
`mlbb_kill_banner.py` accepts the hit as `source=ref`.

Env:
- `MLBB_BANNER_REF_ROOT` — this directory
- `MLBB_BANNER_REF_MATCH=1` — enable visual fallback
- `MLBB_BANNER_REF_MIN_SIM=0.38` — histogram correlation threshold

## Note on “all skins”

Moonton does not publish a full official asset dump. The wiki covers ~15 classic/event
notification frames; VOD crops grow the bank from real streamer gameplay (custom skins).
