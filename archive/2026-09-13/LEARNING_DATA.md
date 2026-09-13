# Learning data inventory (archived 2026-09-13)

Paths under `archive/2026-09-13/`. Checksums: `SHA256SUMS.txt`. Large files also in `learning_compressed/*.gz.b64` and `tarball_parts/*.b64.txt`.

## How ratings feed the ranker

1. Owner rates in Telegram → learning writers persist events.
2. Aggregated into `data/pubg/pubg_owner_labels.json` (`videos` + `labels` + `label_history`).
3. Calibration / probe batches update `calibration_labels.json`, `owner_probe8_sent.json`.
4. Ledger: `vod_quality_ledger/pubg_clip_ledger.jsonl` (`decision`: sent|reject|feedback|heartbeat).
5. Adaptive gates: `vod_adaptive_thresholds/pubg_sent_ranges.json`.
6. `pubg_moment_ranker.py --train-if-changed` → `pubg_moment_ranker.joblib`.
7. Shorts silver: `shorts_autolearn/silver_ranges.json`.

## Counts at archive

- PUBG owner labels: **434 good / 666 bad** (1150 events)
- MLBB owner labels: **697 good / 1036 bad**
- Ledger: 4106 lines (reject 2156, heartbeat 1480, feedback 332, sent 134)
- `pubg_sent_ranges.json`: ready=true, 88 good samples

## Key files

See full table in repo; restore via `RESTORE.md`. Excluded: raw VODs, secrets, viewport_cache, ranker_features, gun montage mp4s, shorts vod_probes.
