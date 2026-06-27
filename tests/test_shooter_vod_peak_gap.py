"""PUBG peak gap after owner 👎 — must not block next fight in same VOD."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


def test_bad_label_does_not_block_nearby_peak():
    """Regression: ICE7afoNgUA_112 👎 blurry must not block peak @124s."""
    from shooter_vod_segment_feed import _peak_too_close, _used_peak_times

    game = "pubg"
    vod_id = "ICE7afoNgUA"
    # Simulate: only bad label at 112, sent list still has _112 from delivery
    peaks = _used_peak_times_from_labels(
        game,
        vod_id,
        good=[],
        bad=[{"segment_id": f"{vod_id}_112"}],
        sent=[f"{vod_id}_112"],
    )
    assert peaks == []
    assert _peak_too_close(124.0, peaks, gap_sec=45) is False


def _used_peak_times_from_labels(game, vod_id, good, bad, sent):
    """Inline replica of gap logic for unit test without filesystem."""
    from shooter_vod_segment_store import load_feed_sent, load_labels
    import shooter_vod_segment_feed as feed

    class FakeLabels:
        @staticmethod
        def side_effect(g):
            return {"good": good, "bad": bad, "feedback": []}

    orig_labels = feed.load_labels
    orig_sent = feed.load_feed_sent
    feed.load_labels = lambda g: {"good": good, "bad": bad, "feedback": []}  # type: ignore
    feed.load_feed_sent = lambda g: sent  # type: ignore
    try:
        return feed._used_peak_times(game, vod_id)
    finally:
        feed.load_labels = orig_labels
        feed.load_feed_sent = orig_sent
