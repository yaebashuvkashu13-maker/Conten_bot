# Labeled video layout

Put training videos here before running hero/skin classifiers.

## Heroes only

```
datasets/labeled/hayabusa/*.mp4
datasets/labeled/gusion/*.mp4
```

## Heroes + skins

```
datasets/labeled/hayabusa/lane_default/*.mp4
datasets/labeled/hayabusa/skin_flaming/*.mp4
```

If you already have ~2300 videos elsewhere, either move/copy them into this tree
or run indexing from the parent folder:

```bash
python3 -m content_bot.dataset_index --root /path/to/your/videos
python3 -m content_bot.batch_features --from-index datasets/index/videos.csv
```
