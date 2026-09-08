# PUBG sent-parameter ranges

Goal the owner asked for:

1. **Remember** numeric params of every sent clip (`quality_metrics` on send + ledger).
2. **Build** allowed ranges (percentiles) from that database (prefer 👍 when enough).
3. **Only send** future clips that fall inside those ranges.

```bash
# rebuild from ledger + segment labels
python3 scripts/pubg_sent_param_ranges.py build --game pubg

# inspect
python3 scripts/pubg_sent_param_ranges.py stats --game pubg
```

Stored at `/root/data/vod_adaptive_thresholds/pubg_sent_ranges.json`.

Gate: `PUBG_SENT_RANGE_GATE=1` (default on) in `shooter_vod_segment_feed` after presend.
Env knobs: `PUBG_SENT_RANGE_LOW_Q` (0.10), `PUBG_SENT_RANGE_HIGH_Q` (0.90),
`PUBG_SENT_RANGE_MIN_SAMPLES` (25), `PUBG_SENT_RANGE_MAX_VIOLATIONS` (1).
