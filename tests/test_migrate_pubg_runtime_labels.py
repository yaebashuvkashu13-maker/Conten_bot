from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from migrate_pubg_runtime_labels import atomic_write, merge_labels  # noqa: E402


def test_merge_preserves_seed_legacy_and_runtime_history(tmp_path: Path) -> None:
    paths = [tmp_path / name for name in ("seed.json", "legacy.json", "runtime.json")]
    paths[0].write_text(
        json.dumps({"videos": {"abc": [{"time_sec": 10, "label": "good"}]}})
    )
    paths[1].write_text(
        json.dumps({"videos": {"abc": [{"time_sec": 20, "label": "bad"}]}})
    )
    paths[2].write_text(
        json.dumps({"videos": {"abc": [{"time_sec": 10, "label": "bad", "source": "feedback"}]}})
    )
    merged = merge_labels(paths)
    assert len(merged["videos"]["abc"]) == 3


def test_atomic_write_creates_runtime_file(tmp_path: Path) -> None:
    path = tmp_path / "pubg" / "pubg_owner_labels.json"
    atomic_write(path, {"videos": {"abc": []}})
    assert json.loads(path.read_text())["videos"] == {"abc": []}
