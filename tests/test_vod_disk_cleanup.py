from __future__ import annotations

import json
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from vod_disk_cleanup import collect_candidates  # noqa: E402


def _vod(root: Path, game: str, folder: str, video_id: str, *, age_hours: float = 48) -> Path:
    path = root / game / "youtube_nightly" / folder / f"yt_{video_id}.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 100)
    stamp = time.time() - age_hours * 3600
    path.touch()
    path.chmod(0o600)
    import os

    os.utime(path, (stamp, stamp))
    return path


def test_cleanup_preserves_active_and_owner_labeled_vods(tmp_path: Path) -> None:
    data = tmp_path / "data"
    repo = tmp_path / "repo"
    datasets = tmp_path / "datasets"
    active = _vod(data, "pubg", "inbox", "active12345")
    exhausted = _vod(data, "pubg", "inbox", "exhaust123")
    labeled = _vod(data, "pubg", "parked", "labeled1234")
    parked = _vod(data, "pubg", "parked", "parked12345")
    inactive = _vod(data, "mlbb", "inbox", "inactive123")

    state_path = data / "pubg" / "vod_segment_state.json"
    state_path.write_text(
        json.dumps(
            {
                "vods": [
                    {"path": str(active), "exhausted": False},
                    {"path": str(exhausted), "exhausted": True},
                ]
            }
        ),
        encoding="utf-8",
    )
    labels_path = repo / "data" / "pubg_owner_labels.json"
    labels_path.parent.mkdir(parents=True)
    labels_path.write_text(
        json.dumps({"videos": {"labeled1234": [{"time_sec": 10, "label": "good"}]}}),
        encoding="utf-8",
    )

    candidates = collect_candidates(
        data_root=data,
        datasets_root=datasets,
        repo_root=repo,
        home_root=tmp_path,
        active_game="pubg",
        now=time.time(),
        open_paths=set(),
    )
    paths = {row.path for row in candidates}

    assert active.resolve() not in paths
    assert labeled.resolve() not in paths
    assert exhausted.resolve() in paths
    assert parked.resolve() in paths
    assert inactive.resolve() in paths


def test_cleanup_preserves_open_files(tmp_path: Path) -> None:
    data = tmp_path / "data"
    opened = _vod(data, "pubg", "parked", "opened12345")
    candidates = collect_candidates(
        data_root=data,
        datasets_root=tmp_path / "datasets",
        repo_root=tmp_path / "repo",
        home_root=tmp_path,
        active_game="pubg",
        now=time.time(),
        open_paths={opened.resolve()},
    )
    assert opened.resolve() not in {row.path for row in candidates}
