#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from pubg_vod_singles_first import (  # noqa: E402
    after_owner_label_keyboard,
    assemble_eligible,
    pick_next_single_row,
    pubg_singles_first_enabled,
    should_show_assemble_button,
    singles_keyboard,
)


def _mock_good_rows(monkeypatch, rows: list[dict]) -> None:
    monkeypatch.setattr(
        "pubg_vod_singles_first.good_rows_for_vod",
        lambda _game, _vod: list(rows),
    )


def test_singles_first_enabled_by_default(monkeypatch):
    monkeypatch.delenv("PUBG_VOD_SINGLES_FIRST", raising=False)
    assert pubg_singles_first_enabled() is True


def test_pick_next_single_marks_final_when_one_left():
    rows = [
        {"segment_id": "vid_100", "peak_start": 100.0, "start": 95.0, "score": 0.9},
        {"segment_id": "vid_200", "peak_start": 200.0, "start": 195.0, "score": 0.8},
    ]

    def _close(peak, used, gap):
        return any(abs(peak - u) <= gap for u in used)

    row, is_final = pick_next_single_row(
        rows,
        blocked_ids=set(),
        rejected_peaks=[],
        gap_sec=45.0,
        used_peaks=[],
        peak_too_close=_close,
    )
    assert row["segment_id"] == "vid_100"
    assert is_final is False

    row2, is_final2 = pick_next_single_row(
        rows,
        blocked_ids={"vid_100"},
        rejected_peaks=[],
        gap_sec=45.0,
        used_peaks=[100.0],
        peak_too_close=_close,
    )
    assert row2["segment_id"] == "vid_200"
    assert is_final2 is True


def test_assemble_keyboard_when_two_ok(monkeypatch):
    monkeypatch.setenv("PUBG_VOD_SINGLES_FIRST", "1")
    _mock_good_rows(
        monkeypatch,
        [
            {"segment_id": "vid_100", "peak_start": 100.0},
            {"segment_id": "vid_200", "peak_start": 200.0},
        ],
    )
    markup = singles_keyboard("pubg", "vid_200", "vid", show_assemble=True)
    texts = [btn["text"] for row in markup["inline_keyboard"] for btn in row]
    assert "🔧 Собрать склейку" in texts


def test_no_assemble_when_only_one_ok(monkeypatch):
    monkeypatch.setenv("PUBG_VOD_SINGLES_FIRST", "1")
    _mock_good_rows(monkeypatch, [{"segment_id": "vid_100", "peak_start": 100.0}])
    assert assemble_eligible("pubg", "vid") is False
    assert should_show_assemble_button("pubg", "vid", singles_final=True) is False
    markup = singles_keyboard("pubg", "vid_100", "vid", show_assemble=True)
    texts = [btn["text"] for row in markup["inline_keyboard"] for btn in row]
    assert "🔧 Собрать склейку" not in texts


def test_assemble_after_label_when_two_ok_and_final(monkeypatch):
    monkeypatch.setenv("PUBG_VOD_SINGLES_FIRST", "1")
    _mock_good_rows(
        monkeypatch,
        [
            {"segment_id": "vid_100", "peak_start": 100.0},
            {"segment_id": "vid_200", "peak_start": 200.0},
        ],
    )

    def _find(_game, sid):
        return {"segment_id": sid, "vod_id": "vid", "singles_final": sid == "vid_200"}

    monkeypatch.setattr("shooter_vod_segment_store.find_segment", _find)
    markup = after_owner_label_keyboard("pubg", "vid_200", "good")
    texts = [btn["text"] for row in markup["inline_keyboard"] for btn in row]
    assert "🔧 Собрать склейку" in texts


def test_no_assemble_after_label_on_non_final(monkeypatch):
    monkeypatch.setenv("PUBG_VOD_SINGLES_FIRST", "1")
    _mock_good_rows(
        monkeypatch,
        [
            {"segment_id": "vid_100", "peak_start": 100.0},
            {"segment_id": "vid_200", "peak_start": 200.0},
        ],
    )

    def _find(_game, sid):
        return {"segment_id": sid, "vod_id": "vid", "singles_final": False}

    monkeypatch.setattr("shooter_vod_segment_store.find_segment", _find)
    markup = after_owner_label_keyboard("pubg", "vid_100", "good")
    texts = [btn["text"] for row in markup["inline_keyboard"] for btn in row]
    assert "🔧 Собрать склейку" not in texts


def test_no_assemble_after_bad_when_only_one_ok_left(monkeypatch):
    monkeypatch.setenv("PUBG_VOD_SINGLES_FIRST", "1")
    _mock_good_rows(monkeypatch, [{"segment_id": "vid_100", "peak_start": 100.0}])

    def _find(_game, sid):
        return {"segment_id": sid, "vod_id": "vid", "singles_final": True}

    monkeypatch.setattr("shooter_vod_segment_store.find_segment", _find)
    markup = after_owner_label_keyboard("pubg", "vid_200", "bad", reason="loot_walk")
    texts = [btn["text"] for row in markup["inline_keyboard"] for btn in row]
    assert "🔧 Собрать склейку" not in texts


def test_pin_inbox_to_active_vod():
    from pubg_vod_singles_first import pin_inbox_to_active_vod, set_active_vod

    state: dict = {}
    files = [Path("yt_AAA111aaa11.mp4"), Path("yt_BBB222bbb22.mp4")]
    registry = [{"id": "AAA111aaa11", "path": str(files[0]), "exhausted": False}]
    set_active_vod(state, "AAA111aaa11")
    pinned = pin_inbox_to_active_vod(state, files, registry)
    assert pinned == [files[0]]


def test_refuse_pin_steal_while_active():
    from pubg_vod_singles_first import get_active_vod_id, set_active_vod

    state: dict = {}
    set_active_vod(state, "AAA111aaa11")
    set_active_vod(state, "BBB222bbb22")
    assert get_active_vod_id(state) == "AAA111aaa11"


def test_clear_active_when_exhausted():
    from pubg_vod_singles_first import get_active_vod_id, pin_inbox_to_active_vod, set_active_vod

    state: dict = {}
    files = [Path("yt_AAA111aaa11.mp4"), Path("yt_BBB222bbb22.mp4")]
    registry = [{"id": "AAA111aaa11", "path": str(files[0]), "exhausted": True}]
    set_active_vod(state, "AAA111aaa11")
    pinned = pin_inbox_to_active_vod(state, files, registry)
    assert len(pinned) == 2
    assert get_active_vod_id(state) == ""


def test_resolve_vod_path_finds_parked(tmp_path, monkeypatch):
    from pubg_vod_singles_first import resolve_vod_path

    inbox = tmp_path / "inbox"
    parked = tmp_path / "parked"
    inbox.mkdir()
    parked.mkdir()
    vod = parked / "yt_Tovruh33adY.mp4"
    vod.write_bytes(b"x")
    monkeypatch.setenv("PUBG_VOD_INBOX", str(inbox))
    assert resolve_vod_path("Tovruh33adY") == vod


def test_segment_belongs_to_vod_owner_prefix():
    from pubg_vod_singles_first import segment_belongs_to_vod

    assert segment_belongs_to_vod("Tovruh33adY_1526", "Tovruh33adY")
    assert segment_belongs_to_vod("owner_yt_Tovruh33adY_6604", "Tovruh33adY")
    assert not segment_belongs_to_vod("6tBEG4XXXP8_1065", "Tovruh33adY")


def test_enqueue_assemble_dedupes(tmp_path, monkeypatch):
    from pubg_vod_singles_first import enqueue_assemble_job, load_pending_assemble

    path = tmp_path / "pending.json"
    monkeypatch.setattr("pubg_vod_singles_first.PENDING_ASSEMBLE_PATH", path)
    first = enqueue_assemble_job("pubg", "Tovruh33adY", "1")
    second = enqueue_assemble_job("pubg", "Tovruh33adY", "1")
    assert first["id"] == second["id"]
    assert len(load_pending_assemble()) == 1


def test_singles_used_gap_keeps_adjacent_fight():
    """Montage gap (~55s) must not hide the next real fight after a prior send."""
    from pubg_vod_singles_first import pick_next_single_row
    from vod_peak_gap import peak_too_close

    rows = [
        {"segment_id": "vid_337", "peak_start": 337.5, "start": 322.0, "score": 0.95},
        {"segment_id": "vid_155", "peak_start": 155.8, "start": 142.0, "score": 0.89},
        {"segment_id": "vid_22", "peak_start": 22.0, "start": 18.0, "score": 0.83},
    ]
    # Already sent a fight near 288s — with used_gap=12 the 337s fight stays eligible.
    row, _ = pick_next_single_row(
        rows,
        blocked_ids=set(),
        rejected_peaks=[],
        gap_sec=12.0,
        used_peaks=[288.0],
        peak_too_close=peak_too_close,
    )
    assert row is not None
    assert row["peak_start"] == 337.5

    # Legacy montage-sized gap wrongly skips it.
    row_blocked, _ = pick_next_single_row(
        rows,
        blocked_ids=set(),
        rejected_peaks=[],
        gap_sec=55.0,
        used_peaks=[288.0],
        peak_too_close=peak_too_close,
    )
    assert row_blocked is None or row_blocked["peak_start"] != 337.5


def test_full_scan_inspects_all_peaks_budget(monkeypatch):
    from pubg_vod_singles_first import (
        singles_max_sends_per_cycle,
        singles_peak_try_budget,
        singles_zero_send_exhaust_limit,
    )

    monkeypatch.setenv("PUBG_FULL_PEAK_SCAN", "1")
    monkeypatch.delenv("PUBG_SINGLES_PEAK_TRIES_PER_RUN", raising=False)
    monkeypatch.delenv("PUBG_SINGLES_ZERO_SEND_EXHAUST", raising=False)
    monkeypatch.delenv("PUBG_SINGLES_MAX_SENDS_PER_CYCLE", raising=False)
    assert singles_peak_try_budget(40) == 40
    assert singles_zero_send_exhaust_limit() == 20
    assert singles_max_sends_per_cycle() == 0

    monkeypatch.setenv("PUBG_SINGLES_PEAK_TRIES_PER_RUN", "0")
    assert singles_peak_try_budget(25) == 25

    monkeypatch.setenv("PUBG_SINGLES_MAX_SENDS_PER_CYCLE", "3")
    assert singles_max_sends_per_cycle() == 3

    monkeypatch.setenv("PUBG_FULL_PEAK_SCAN", "0")
    monkeypatch.delenv("PUBG_SINGLES_PEAK_TRIES_PER_RUN", raising=False)
    monkeypatch.delenv("PUBG_SINGLES_MAX_SENDS_PER_CYCLE", raising=False)
    assert singles_peak_try_budget(40) == 4
    assert singles_zero_send_exhaust_limit() == 6
    assert singles_max_sends_per_cycle() == 1


def test_quality_flood_sends_multiple_gate_passes(monkeypatch, tmp_path):
    """Full scan ships every gate-pass in one cycle (not stop-after-first)."""
    import pubg_vod_singles_first as sf

    monkeypatch.setenv("PUBG_FULL_PEAK_SCAN", "1")
    monkeypatch.delenv("PUBG_SINGLES_MAX_SENDS_PER_CYCLE", raising=False)
    monkeypatch.delenv("PUBG_SINGLES_PEAK_TRIES_PER_RUN", raising=False)
    monkeypatch.delenv("PUBG_SINGLES_ZERO_SEND_EXHAUST", raising=False)

    rows = [
        {"segment_id": "vid_100", "peak_start": 100.0, "start": 95.0, "score": 0.9},
        {"segment_id": "vid_200", "peak_start": 200.0, "start": 195.0, "score": 0.8},
        {"segment_id": "vid_300", "peak_start": 300.0, "start": 295.0, "score": 0.7},
    ]
    sent_ids: list[str] = []

    def _send_batch(_game, _token, _chat, _vod, batch, *_a, **_k):
        sid = str(batch[0]["segment_id"])
        sent_ids.append(sid)
        return 1

    class _Mod:
        @staticmethod
        def _peak_too_close(peak, used, gap):
            return any(abs(peak - u) <= gap for u in used)

        @staticmethod
        def _remember_dense_rejections(*_a, **_k):
            return None

        @staticmethod
        def _send_batch(*a, **k):
            return _send_batch(*a, **k)

        @staticmethod
        def _used_peak_times(_game, _vid, sent_set):
            peaks = []
            for sid in sent_set:
                if sid.endswith("_100"):
                    peaks.append(100.0)
                elif sid.endswith("_200"):
                    peaks.append(200.0)
                elif sid.endswith("_300"):
                    peaks.append(300.0)
            return peaks

        @staticmethod
        def labeled_ids(_game):
            return set()

        @staticmethod
        def load_feed_sent(_game):
            return set(sent_ids)

    import sys

    monkeypatch.setitem(sys.modules, "shooter_vod_segment_feed", _Mod())
    monkeypatch.setattr(sf, "singles_keyboard", lambda *a, **k: {"inline_keyboard": []})
    monkeypatch.setattr(sf, "should_show_assemble_button", lambda *a, **k: False)

    state: dict = {}
    entry: dict = {}
    marks: list[str] = []

    total = sf.singles_first_send_cycle(
        game="pubg",
        token="t",
        chat_id="1",
        vod=tmp_path / "yt_vid.mp4",
        vid="vid",
        state=state,
        entry=entry,
        rows=rows,
        gap_sec=45.0,
        rejected_peaks=[],
        sig="abc",
        mark_exhausted_fn=lambda *_a, **_k: marks.append("ex"),
        save_state_fn=lambda *_a, **_k: None,
        record_scan_fn=lambda *_a, **_k: None,
    )
    assert total == 3
    assert sent_ids == ["vid_100", "vid_200", "vid_300"]
    assert marks  # exhausted/complete after pool done


def test_quality_flood_respects_send_cap(monkeypatch, tmp_path):
    import pubg_vod_singles_first as sf
    import sys

    monkeypatch.setenv("PUBG_FULL_PEAK_SCAN", "1")
    monkeypatch.setenv("PUBG_SINGLES_MAX_SENDS_PER_CYCLE", "2")
    monkeypatch.delenv("PUBG_SINGLES_PEAK_TRIES_PER_RUN", raising=False)

    rows = [
        {"segment_id": "vid_100", "peak_start": 100.0, "start": 95.0, "score": 0.9},
        {"segment_id": "vid_200", "peak_start": 200.0, "start": 195.0, "score": 0.8},
        {"segment_id": "vid_300", "peak_start": 300.0, "start": 295.0, "score": 0.7},
    ]
    sent_ids: list[str] = []

    class _Mod:
        @staticmethod
        def _peak_too_close(peak, used, gap):
            return any(abs(peak - u) <= gap for u in used)

        @staticmethod
        def _remember_dense_rejections(*_a, **_k):
            return None

        @staticmethod
        def _send_batch(_game, _token, _chat, _vod, batch, *_a, **_k):
            sent_ids.append(str(batch[0]["segment_id"]))
            return 1

        @staticmethod
        def _used_peak_times(_game, _vid, sent_set):
            mapping = {"vid_100": 100.0, "vid_200": 200.0, "vid_300": 300.0}
            return [mapping[s] for s in sent_set if s in mapping]

        @staticmethod
        def labeled_ids(_game):
            return set()

        @staticmethod
        def load_feed_sent(_game):
            return set(sent_ids)

    monkeypatch.setitem(sys.modules, "shooter_vod_segment_feed", _Mod())
    monkeypatch.setattr(sf, "singles_keyboard", lambda *a, **k: {"inline_keyboard": []})
    monkeypatch.setattr(sf, "should_show_assemble_button", lambda *a, **k: False)

    total = sf.singles_first_send_cycle(
        game="pubg",
        token="t",
        chat_id="1",
        vod=tmp_path / "yt_vid.mp4",
        vid="vid",
        state={},
        entry={},
        rows=rows,
        gap_sec=45.0,
        rejected_peaks=[],
        sig="abc",
        mark_exhausted_fn=lambda *_a, **_k: None,
        save_state_fn=lambda *_a, **_k: None,
        record_scan_fn=lambda *_a, **_k: None,
    )
    assert total == 2
    assert sent_ids == ["vid_100", "vid_200"]
