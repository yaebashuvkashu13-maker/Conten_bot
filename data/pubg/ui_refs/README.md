# PUBG Mobile UI reference pack

Run on server or locally:

```bash
python3 pubg_ui_refs_download.py
# optional: extract frame from gameplay video
python3 pubg_ui_refs_download.py --extract-video
```

Outputs under `PUBG_UI_REFS_ROOT` (default `/root/datasets/pubg/ui_refs/`):

- `layout.json` — normalized HUD ROIs (minimap, joystick, kill feed, health)
- `frames/` — sample gameplay screenshots
- `crops/` — per-region PNG crops for template matching / YOLO bootstrap

Sources: Roboflow public PUBG datasets + IEEE shooter-GUI taxonomy (see `docs/OVERNIGHT_RESEARCH.md`).
