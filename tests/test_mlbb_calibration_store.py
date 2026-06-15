from __future__ import annotations

import json
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from mlbb_telegram_handlers import parse_callback_data  # noqa: E402


def _setup_store(monkeypatch, tmp_path: Path):
    data_root = tmp_path / "mlbb"
    shorts_root = tmp_path / "shorts"
    data_root.mkdir()
    shorts_root.mkdir()

    monkeypatch.setenv("MLBB_DATA_ROOT", str(data_root))
    monkeypatch.setenv("MLBB_SHORTS_ROOT", str(shorts_root))
    monkeypatch.setenv("CONTENT_BOT_REPO", str(tmp_path))
    monkeypatch.setenv("HIGHLIGHT_EXEMPLAR_ROOT", str(tmp_path / "exemplars"))
    monkeypatch.setenv("MLBB_TRAINING_ARCHIVE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("MLBB_TRAINING_ARCHIVE", "1")
    monkeypatch.setenv("MLBB_ACTIVE_LEARNING", "1")

    import mlbb_calibration_store as store

    monkeypatch.setattr(store, "DATA_MLBB", data_root)
    monkeypatch.setattr(store, "SHORTS_ROOT", shorts_root)
    monkeypatch.setattr(store, "INDEX_PATH", data_root / "youtube_shorts_index.json")
    monkeypatch.setattr(store, "LABELS_PATH", data_root / "calibration_labels.json")
    monkeypatch.setattr(store, "FEED_SENT_PATH", data_root / "calibration_feed_sent.json")
    monkeypatch.setattr(store, "INGEST_SKIP_PATH", data_root / "ingest_skip_ids.json")
    monkeypatch.setattr(store, "REPO_LABELS_PATH", tmp_path / "repo_labels.json")
    return store


def _add_clip(store, shorts_root, vid: str, *, score: float, title: str = "clip") -> Path:
    clip = shorts_root / f"yt_{vid}.mp4"
    clip.write_bytes(b"x" * 20_000)
    store.upsert_candidate(
        {
            "video_id": vid,
            "path": str(clip),
            "title": title,
            "url": f"https://youtube.com/shorts/{vid}",
            "score": score,
            "ingest_verified": 1,
            "identity_pass": 1,
            "gameplay_pass": 1,
            "rule_pass": 1,
        }
    )
    return clip


def test_parse_callback_data() -> None:
    assert parse_callback_data("mlbb_noop") == ("noop", None, "", "")
    mode, good, vid, reason = parse_callback_data("mlbb_yes:abc123")
    assert mode == "shorts" and good is True and vid == "abc123"
    mode, good, sid, reason = parse_callback_data("mlbb_vseg_no:0nvW7JiFr0o_576")
    assert mode == "vseg" and good is False and sid == "0nvW7JiFr0o_576"
    assert reason == "button_dislike"
    mode, good, vid, _ = parse_callback_data("mlbb_hq_shorts:abc123")
    assert mode == "hq_shorts" and good is None and vid == "abc123"


def test_pending_excludes_labeled(monkeypatch, tmp_path: Path) -> None:
    store = _setup_store(monkeypatch, tmp_path)
    shorts_root = store.SHORTS_ROOT

    vid = "testvid1234"[:11]
    _add_clip(store, shorts_root, vid, score=0.5)
    pending = store.pending_candidates(limit=10)
    assert any(r["video_id"] == vid for r in pending)

    store.apply_owner_label(vid, is_good=True, by_chat="1")
    assert vid in store.labeled_ids()
    pending2 = store.pending_candidates(limit=10)
    assert not any(r["video_id"] == vid for r in pending2)

    vid2 = "sentvid12345"[:11]
    clip2 = _add_clip(store, shorts_root, vid2, score=0.4, title="sent clip")
    store.mark_feed_sent([vid2], paths=[clip2])
    monkeypatch.setenv("MLBB_RESEND_UNLABELED_HOURS", "0")
    pending3 = store.pending_candidates(limit=10)
    assert not any(r["video_id"] == vid2 for r in pending3)

    store.save_index({"candidates": [], "updated_at": ""})
    ok, _ = store.apply_owner_label(vid, is_good=False, by_chat="1", reason="retest")
    assert ok


def test_resend_after_hours(monkeypatch, tmp_path: Path) -> None:
    store = _setup_store(monkeypatch, tmp_path)
    shorts_root = store.SHORTS_ROOT
    vid = "resendvid12"
    clip = _add_clip(store, shorts_root, vid, score=0.42)

    store.mark_feed_sent([vid], paths=[clip])
    sent = store.load_feed_sent()
    sent["at"][vid] = time.time() - 72 * 3600
    store._write_json(store.FEED_SENT_PATH, {
        "sent_ids": sorted(sent["ids"]),
        "sent_file_ids": sorted(sent["file_ids"]),
        "sent_at": sent["at"],
        "updated_at": "",
    })

    monkeypatch.setenv("MLBB_RESEND_UNLABELED_HOURS", "48")
    pending = store.pending_candidates(limit=10, repair=False)
    assert any(r["video_id"] == vid for r in pending)


def test_active_learning_prefers_boundary_scores(monkeypatch, tmp_path: Path) -> None:
    store = _setup_store(monkeypatch, tmp_path)
    shorts_root = store.SHORTS_ROOT
    high = "highscore12"
    mid = "midscore123"
    low = "lowscore123"
    _add_clip(store, shorts_root, high, score=0.82, title="high")
    _add_clip(store, shorts_root, mid, score=0.36, title="mid")
    _add_clip(store, shorts_root, low, score=0.08, title="low")

    pending = store.pending_candidates(limit=3, repair=False)
    assert pending[0]["video_id"] == mid
    assert pending[0]["uncertainty"] >= pending[1]["uncertainty"]


def test_ingest_gate_stats(monkeypatch, tmp_path: Path) -> None:
    store = _setup_store(monkeypatch, tmp_path)
    store.mark_ingest_skip("aaaaaaaaaaa", "activity:static")
    store.mark_ingest_skip("bbbbbbbbbbb", "kill_ui:no_kill")
    store.mark_ingest_skip("ccccccccccc", "activity:music_bed")

    stats = store.ingest_gate_stats()
    assert stats["total_skipped"] == 3
    assert stats["by_gate"].get("activity") == 2
    assert stats["by_gate"].get("kill_ui") == 1


def test_retrain_debounce(monkeypatch, tmp_path: Path) -> None:
    state_path = tmp_path / "mlbb_retrain_state.json"
    monkeypatch.setenv("MLBB_RETRAIN_STATE", str(state_path))
    monkeypatch.setenv("MLBB_RETRAIN_MIN_LABELS", "3")
    monkeypatch.setenv("MLBB_RETRAIN_MIN_HOURS", "6")

    import importlib.util

    spec = importlib.util.spec_from_file_location("mlf_real", SCRIPTS / "mlbb_learning_first.py")
    lf = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(lf)

    lf.save_retrain_state({"labels_since_retrain": 0, "last_retrain_at": 0.0, "pending": False})
    lf.record_label_for_retrain()
    lf.record_label_for_retrain()
    ok, reason = lf.should_run_retrain()
    assert not ok

    lf.record_label_for_retrain()
    ok, reason = lf.should_run_retrain()
    assert ok
    assert "labels=3" in reason

    lf.mark_retrain_finished(ok=True)
    lf.record_label_for_retrain()
    ok, _ = lf.should_run_retrain()
    assert not ok


def test_shorts_focus_pauses_vod(monkeypatch) -> None:
    import mlbb_continuous_worker as worker

    monkeypatch.setenv("MLBB_SHORTS_FOCUS", "1")
    monkeypatch.setenv("MLBB_TARGET_PENDING", "40")
    assert worker.should_start_vod(pending=10, target_pending=40) is False
    assert worker.should_start_vod(pending=50, target_pending=40) is True
    assert worker.should_start_ingest(pending=10, target_pending=40) is True
