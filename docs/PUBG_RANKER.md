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

Server benchmark on `n97cHIR9Qow` (first 5400 seconds, 88 windows):

- 1 worker: `0.1852` accepted clips / wall-clock minute;
- 2 workers: `0.1879`;
- 4 workers: `0.2270` (selected, about 23% above one worker).

`data/pubg_regression_labels.json` is immutable. Online owner feedback may
override old labels for training, but must not rewrite the regression set.
Generator recall uses a ±45 second event tolerance; exact clip boundaries are
evaluated after `pubg_fight_segment.py`.

Release validation (2026-09-01):

- generator recall: `10/10` current/immutable good events;
- candidate ranker: 295 augmented windows, 26 VOD groups;
- leave-one-VOD-out balanced accuracy: `0.638`;
- conflict-aware accepted recall: `5/9` known good events;
- conflict-aware bad accept rate: `0/9`;
- one immutable `good` timestamp was superseded by later owner `bad` feedback.

The timestamp set is intentionally sparse and does not claim that all other
top-10 windows are bad. Ranker quality is gated primarily by held-out grouped
accuracy and bad-accept regression, not by sparse-label precision@10.
