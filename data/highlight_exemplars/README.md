# Highlight exemplars (owner 👍 / 👎)

CLIP/PANNs scoring uses short video or image clips in per-game folders:

```
highlight_exemplars/
  mobile_legends/good/   mobile_legends/bad/
  pubg/good/             pubg/bad/
  standoff/good/         standoff/bad/
  genshin/good/          genshin/bad/
  wot/good/              wot/bad/
```

## Bootstrap from VPS

On a machine with existing exemplars under `/root/content_bot_ml/data/highlight_exemplars`:

```bash
bash scripts/sync_highlight_vps.sh
```

Or copy manually:

```bash
rsync -av root@VPS:/root/content_bot_ml/data/highlight_exemplars/ ./data/highlight_exemplars/
```

## Inference vs training

- **Training / bootstrap:** `HIGHLIGHT_USE_OWNER_ANCHORS=1` (optional stage1 seeds from owner labels).
- **Production VOD inference:** `HIGHLIGHT_USE_OWNER_ANCHORS=0` — exemplar CLIP only, no label-window injection.

Minimum good exemplars per game: 5 (see `highlight_scorer.exemplars_sufficient`).
