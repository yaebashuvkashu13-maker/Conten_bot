from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from mlbb_telegram_handlers import parse_callback_data  # noqa: E402


def test_parse_callback_data() -> None:
    assert parse_callback_data("mlbb_noop") == ("noop", None, "", "")
    mode, good, vid, reason = parse_callback_data("mlbb_yes:abc123")
    assert mode == "shorts" and good is True and vid == "abc123"
    mode, good, sid, reason = parse_callback_data("mlbb_vseg_no:0nvW7JiFr0o_576")
    assert mode == "vseg" and good is False and sid == "0nvW7JiFr0o_576"
    assert reason == "button_dislike"


def test_pending_excludes_labeled(monkeypatch, tmp_path: Path) -> None:
    data_root = tmp_path / "mlbb"
    shorts_root = tmp_path / "shorts"
    data_root.mkdir()
    shorts_root.mkdir()

    monkeypatch.setenv("MLBB_DATA_ROOT", str(data_root))
    monkeypatch.setenv("MLBB_SHORTS_ROOT", str(shorts_root))
    monkeypatch.setenv("CONTENT_BOT_REPO", str(tmp_path))
    monkeypatch.setenv("HIGHLIGHT_EXEMPLAR_ROOT", str(tmp_path / "exemplars"))

    import mlbb_calibration_store as store

    monkeypatch.setattr(store, "DATA_MLBB", data_root)
    monkeypatch.setattr(store, "SHORTS_ROOT", shorts_root)
    monkeypatch.setattr(store, "INDEX_PATH", data_root / "youtube_shorts_index.json")
    monkeypatch.setattr(store, "LABELS_PATH", data_root / "calibration_labels.json")
    monkeypatch.setattr(store, "FEED_SENT_PATH", data_root / "calibration_feed_sent.json")
    monkeypatch.setattr(store, "REPO_LABELS_PATH", tmp_path / "repo_labels.json")

    upsert_candidate = store.upsert_candidate
    pending_candidates = store.pending_candidates
    labeled_ids = store.labeled_ids
    apply_owner_label = store.apply_owner_label

    vid = "testvid1234"
    clip = shorts_root / f"yt_{vid}.mp4"
    clip.write_bytes(b"x" * 20_000)

    upsert_candidate(
        {
            "video_id": vid,
            "path": str(clip),
            "title": "test",
            "url": f"https://youtube.com/shorts/{vid}",
            "score": 0.5,
        }
    )
    pending = pending_candidates(limit=10)
    assert any(r["video_id"] == vid for r in pending)

    apply_owner_label(vid, is_good=True, by_chat="1")
    labeled = labeled_ids()
    assert vid in labeled
    pending2 = pending_candidates(limit=10)
    assert not any(r["video_id"] == vid for r in pending2)
