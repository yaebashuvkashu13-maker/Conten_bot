# Learning data inventory (archived 2026-09-13)

All paths below are under `archive/2026-09-13/` unless noted. Full checksums: `SHA256SUMS.txt`.  
Compressed duplicates for large files: `learning_compressed/*.gz` (+ `.b64` for API-safe copies).  
Complete tarball: `content_bot_learning_20260913.tar.gz` (sha256 in `TARBALL.sha256` / handoff).

## How ratings feed the ranker

1. Owner taps 👍/👎 in Telegram → `vod_owner_learning` / upload bot writes events.
2. Aggregated into `data/pubg/pubg_owner_labels.json` (`videos` map + `labels` + `label_history`).
3. Calibration / probe batches update `calibration_labels.json`, `owner_probe8_sent.json`.
4. Ledger append-only log: `vod_quality_ledger/pubg_clip_ledger.jsonl` (`decision`: sent|reject|feedback|heartbeat).
5. Adaptive gates: good-cohort quantiles → `vod_adaptive_thresholds/pubg_sent_ranges.json`.
6. `pubg_moment_ranker.py --train-if-changed` reads labels + features → writes `pubg_moment_ranker.joblib` (+ `.json` metadata).
7. Shorts silver path separately fills `shorts_autolearn/silver_ranges.json` from harvested short features.

## PUBG artifacts

| File | Size (approx) | Schema / notes |
|------|---------------|----------------|
| `data/pubg/pubg_owner_labels.json` | 144K | `{videos, labels, label_history, updated_at}` — **434 good / 666 bad** |
| `data/pubg/pubg_moment_ranker.joblib` | 1.1M | sklearn/joblib champion model |
| `data/pubg/pubg_moment_ranker.json` | 1K | model card / metrics |
| `data/pubg/pubg_moment_ranker.candidate.*` | small | last candidate before promote |
| `data/pubg/vod_segment_labels.json` | 3.3M | per-segment labels (gz in learning_compressed) |
| `data/pubg/vod_segment_state.json` | 4K | active pools / pins |
| `data/pubg/calibration_labels.json` | 96K | good/bad calibration lists |
| `data/pubg/owner_feedback_manifest.json` | 1.5K | manifest versioning |
| `data/pubg/owner_rejected_peaks.json` | <1K | hard-rejected peaks |
| `data/pubg/owner_probe8_sent.json` | 33K | 8s probe send log |
| `data/pubg/learning_audit_report.json` | 12K | missed anchors audit |
| `data/pubg/regression_*.json` | ~500K | regression fixtures |
| `data/pubg/shorts_autolearn/features.csv\|jsonl` | ~1M | silver features |
| `data/pubg/shorts_autolearn/silver_ranges.json` | 2K | Shorts quantile gates |
| `data/pubg/shorts_autolearn/state.json` | 27K | harvest state |
| `data/vod_quality_ledger/pubg_clip_ledger.jsonl` | 6.0M | 4106 decisions |
| `data/vod_adaptive_thresholds/pubg_sent_ranges.json` | 2K | **ready=true**, 88 good samples |
| `data/vod_adaptive_thresholds/game_thresholds.json` | <1K | multi-game thresholds |

## MLBB artifacts (historical, still useful)

| File | Size | Notes |
|------|------|-------|
| `data/mlbb/mobile_legends_owner_labels.json` | 278K | 697 good / 1036 bad |
| `data/mlbb/calibration_labels.json` | 636K | |
| `data/mlbb/banner_calibration_*.json` | varies | banner OCR/ref learning |
| `data/mlbb/vod_segment_labels.json` | 436K | |

## Highlight exemplars

- Metadata manifests only; **mp4 excluded** (see `data/highlight_exemplars/EXCLUDED_MP4.txt`).
- Two owner-ref mp4s existed on VPS (~19M total) — re-export from Telegram/owner if needed.

## Excluded (on purpose)

- Raw VOD mp4 multi-GB
- `viewport_cache/`, `ranker_features/` (42M), `like_gun_montages/` (381M), shorts `vod_probes/` (31M)
- Secrets in `.video_bot.env`
