# PUBG moment ranker and regression checks

Production flow:

1. `shooter_vod_fast_scan.py` generates a high-recall peak pool.
2. `pubg_moment_ranker.py` ranks peaks using owner timestamps and per-part 👍/👎.
3. `pubg_fight_segment.py` expands each peak to contact → fight → payoff → finale.
4. `pubg_quality_score.py` hard-rejects only known junk and scores ambiguous windows.

The model is trained nightly by `content-bot-pubg-ranker.timer`. A model is used
only when leave-one-VOD-out balanced accuracy meets
`PUBG_RANKER_MIN_OOF_BALANCED_ACCURACY`.

```bash
python3 scripts/pubg_moment_ranker.py --train
python3 scripts/pubg_regression_benchmark.py --restore-missing
python3 scripts/pubg_regression_benchmark.py \
  --baseline /root/data/pubg/regression_baseline.json
python3 scripts/benchmark_panns_workers.py \
  --vod /root/data/pubg/regression_vods/yt_VIDEO_ID.mp4 \
  --workers 1 2 4 --limit-sec 5400
```

`data/pubg_regression_labels.json` is immutable. Online owner feedback may
override old labels for training, but must not rewrite the regression set.
