from __future__ import annotations

import json
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import standoff_exemplar_ingest as ingest  # noqa: E402


def test_save_and_count_exemplar(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "exemplars"
    monkeypatch.setenv("HIGHLIGHT_EXEMPLAR_ROOT", str(root))
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video-bytes-here" * 100)
    dest = ingest.save_standoff_exemplar_video(source, chat_id="123", label="duel")
    assert dest.exists()
    assert dest.parent.name == "good"
    assert dest.parent.parent.name == "standoff"
    meta = dest.with_name(f"{dest.name}.meta.json")
    assert meta.exists()
    payload = json.loads(meta.read_text(encoding="utf-8"))
    assert payload["game"] == "standoff"
    assert ingest.count_standoff_exemplars() == 1


def test_import_recent_owner_videos(tmp_path: Path, monkeypatch) -> None:
    upload_root = tmp_path / "telegram_uploads"
    pending = upload_root / "pending" / "999"
    pending.mkdir(parents=True)
    root = tmp_path / "exemplars"
    monkeypatch.setenv("HIGHLIGHT_EXEMPLAR_ROOT", str(root))
    monkeypatch.setattr(ingest, "UPLOAD_ROOT", upload_root)
    monkeypatch.setattr(ingest, "PENDING_ROOT", upload_root / "pending")
    monkeypatch.setattr(ingest, "ARCHIVE_ROOT", upload_root / "archive")

    clips = []
    for idx in range(3):
        clip = pending / f"{idx}_clip.mp4"
        clip.write_bytes(b"x" * 100_000)
        clips.append(clip)
        time.sleep(0.01)

    saved, skipped = ingest.import_recent_standoff_exemplars("999", limit=9)
    assert len(saved) == 3
    assert ingest.count_standoff_exemplars() == 3
    saved2, skipped2 = ingest.import_recent_standoff_exemplars("999", limit=9)
    assert saved2 == []
    assert len(skipped2) == 3
