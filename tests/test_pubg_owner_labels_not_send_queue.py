
"""Owner timestamps calibrate the bot — they must not drive Telegram sends."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def test_owner_peaks_empty_when_seed_sends_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHOOTER_VOD_OWNER_ANCHOR_MONTAGE", "1")
    monkeypatch.setenv("PUBG_OWNER_LABEL_SEED_SENDS", "0")
    from shooter_owner_montage import owner_good_fight_peaks

    vod = tmp_path / "yt_6mWLqNBX1pE.mp4"
    vod.write_bytes(b"")
    assert owner_good_fight_peaks("pubg", vod) == []


def test_peak_near_owner_good_reads_labels_even_when_seed_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """👍 must still teach trust/ranking when labels are not the send queue."""
    monkeypatch.setenv("SHOOTER_VOD_OWNER_ANCHOR_MONTAGE", "1")
    monkeypatch.setenv("PUBG_OWNER_LABEL_SEED_SENDS", "0")
    import shooter_owner_montage as m

    vod = tmp_path / "yt_DGsoEt9znls.mp4"
    vod.write_bytes(b"")
    monkeypatch.setattr(m, "owner_labeled_good_times", lambda game, path: [606.0])
    assert m.peak_near_owner_good("pubg", vod, 610.0) is True
    assert m.peak_near_owner_good("pubg", vod, 100.0) is False
    # Seeding still off — fight_peaks stay empty.
    assert m.owner_good_fight_peaks("pubg", vod) == []


def test_boost_pool_near_owner_labels(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHOOTER_VOD_OWNER_ANCHOR_SCORE_BOOST", "0.20")
    monkeypatch.setenv("SHOOTER_VOD_OWNER_ANCHOR_RADIUS_SEC", "45")
    import shooter_owner_montage as m

    vod = tmp_path / "yt_DGsoEt9znls.mp4"
    vod.write_bytes(b"")
    monkeypatch.setattr(m, "owner_labeled_good_times", lambda game, path: [606.0])
    monkeypatch.setattr(m, "_is_owner_rejected_peak", lambda *a, **k: False)
    pool = [
        {"start": 100.0, "score": 0.50},
        {"start": 610.0, "score": 0.50},
    ]
    out = m.boost_pool_near_owner_labels("pubg", vod, pool)
    by_start = {float(c["start"]): c for c in out}
    assert by_start[610.0]["score"] == pytest.approx(0.70)
    assert by_start[610.0].get("owner_anchor") is True
    assert by_start[100.0]["score"] == pytest.approx(0.50)


def test_owner_neighborhood_probe_peaks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHOOTER_VOD_OWNER_PROBE_OFFSETS_SEC", "0,30,-30")
    import shooter_owner_montage as m

    vod = tmp_path / "yt_zRQC8jkxbXQ.mp4"
    vod.write_bytes(b"")
    monkeypatch.setattr(m, "owner_labeled_good_times", lambda game, path: [2141.5])
    monkeypatch.setattr(m, "_is_owner_rejected_peak", lambda *a, **k: False)
    peaks = m.owner_neighborhood_probe_peaks("pubg", vod)
    assert 2141.5 in peaks
    assert any(abs(p - 2171.5) < 0.1 for p in peaks)
    assert any(abs(p - 2111.5) < 0.1 for p in peaks)


def test_build_owner_neighborhood_send_rows_locks_bounds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PUBG_OWNER_NEIGHBORHOOD_DIRECT", "1")
    monkeypatch.setenv("PUBG_OWNER_NEIGHBORHOOD_OFFSETS_SEC", "28,-28")
    monkeypatch.setenv("PUBG_OWNER_NEIGHBORHOOD_LEAD_SEC", "8")
    monkeypatch.setenv("PUBG_OWNER_NEIGHBORHOOD_DUR_SEC", "45")
    import shooter_owner_montage as m
    import shooter_vod_segment_store as store

    vod = tmp_path / "yt_zRQC8jkxbXQ.mp4"
    vod.write_bytes(b"")
    monkeypatch.setattr(m, "owner_labeled_good_times", lambda game, path: [2141.5])
    monkeypatch.setattr(m, "_is_owner_rejected_peak", lambda *a, **k: False)
    monkeypatch.setattr(store, "segment_id", lambda vid, start: f"{vid}_{int(start)}")
    rows = m.build_owner_neighborhood_send_rows("pubg", vod, blocked_ids=set(), used_peaks=[])
    assert rows
    assert all(r.get("owner_neighborhood_direct") for r in rows)
    assert all(r["clip"].get("bounds_locked") for r in rows)
    assert all(float(r["clip"]["input_duration"]) >= 35.0 for r in rows)
    # Exact 👍 peak must not be re-queued (offset 0 excluded).
    assert all(abs(float(r["peak_start"]) - 2141.5) >= 12.0 for r in rows)


def test_resolve_owner_neighborhood_bounds_trims_run_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import shooter_owner_montage as m

    vod = tmp_path / "yt_zRQC8jkxbXQ.mp4"
    vod.write_bytes(b"")
    monkeypatch.setattr(m, "_is_owner_rejected_peak", lambda *a, **k: False)

    def _probe(_vod, t, _dur):
        # Run-in junk before 3970; gun act 3970-4070.
        if float(t) < 3968:
            return {"gunfire_density": 0.02, "burst_ratio": 3.0, "center_motion": 0.16}
        if float(t) <= 4070:
            return {"gunfire_density": 0.09, "burst_ratio": 4.5, "center_motion": 0.08}
        return {"gunfire_density": 0.01, "burst_ratio": 2.0, "center_motion": 0.05}

    import types, sys

    fake = types.ModuleType("pubg_shooting_gate")
    fake.pubg_probe_segment = _probe
    monkeypatch.setitem(sys.modules, "pubg_shooting_gate", fake)
    start, dur = m.resolve_owner_neighborhood_bounds(vod, 3991.0)
    assert start >= 3965.0
    assert start <= 3972.0
    assert (start + dur) >= 4065.0


def test_prescore_owner_neighborhood_keeps_passers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import shooter_owner_montage as m

    vod = tmp_path / "yt_zRQC8jkxbXQ.mp4"
    vod.write_bytes(b"")

    def _score(path, start, dur, single=True, use_cache=True):
        if float(start) == 100.0:
            return True, "quality_ok=0.5", {"quality_score": 0.5}
        return False, "hard_loot_walk", {"quality_score": 0.1}

    import types, sys

    fake = types.ModuleType("pubg_quality_score")
    fake.score_pubg_window = _score
    monkeypatch.setitem(sys.modules, "pubg_quality_score", fake)
    rows = [
        {
            "segment_id": "z_100",
            "start": 100.0,
            "peak_start": 108.0,
            "score": 0.97,
            "clip": {"start": 100.0, "input_duration": 24.0},
        },
        {
            "segment_id": "z_200",
            "start": 200.0,
            "peak_start": 208.0,
            "score": 0.97,
            "clip": {"start": 200.0, "input_duration": 24.0},
        },
    ]
    kept = m.prescore_owner_neighborhood_rows(vod, rows, max_score=8, keep=4)
    assert len(kept) == 1
    assert kept[0]["segment_id"] == "z_100"
    assert kept[0]["score"] >= 0.90


def test_owner_peaks_available_only_with_explicit_seed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHOOTER_VOD_OWNER_ANCHOR_MONTAGE", "1")
    monkeypatch.setenv("PUBG_OWNER_LABEL_SEED_SENDS", "1")
    import shooter_owner_montage as m

    monkeypatch.setattr(m, "_peaks_from_pubg_calibration", lambda vod: [6.0, 147.0, 718.0])
    monkeypatch.setattr(m, "style_reference_peaks", lambda vod: [], raising=False)
    # patch import path used inside function
    monkeypatch.setitem(__import__("sys").modules, "pubg_owner_style", type("X", (), {"style_reference_peaks": staticmethod(lambda vod: [])})())
    vod = tmp_path / "yt_6mWLqNBX1pE.mp4"
    vod.write_bytes(b"")
    peaks = m.owner_good_fight_peaks("pubg", vod)
    assert 6.0 in peaks
    assert 718.0 in peaks


def test_singles_used_gap_default_blocks_near_duplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PUBG_SINGLES_USED_GAP_SEC", raising=False)
    from vod_peak_gap import peak_too_close
    import os
    gap = float(os.environ.get("PUBG_SINGLES_USED_GAP_SEC", "20"))
    assert gap >= 20
    # Near-dupe of a prior send (~15s) must stay blocked.
    assert peak_too_close(36.8, [52.0], gap) is True
    # Distinct fight ~46s later must remain eligible.
    assert peak_too_close(6.0, [52.0], gap) is False
    assert peak_too_close(337.5, [288.0], gap) is False
