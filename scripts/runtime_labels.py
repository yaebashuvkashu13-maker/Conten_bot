#!/usr/bin/env python3
"""Runtime owner labels outside git checkout — repo file is seed only."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

_PROFILE_RUNTIME: dict[str, tuple[str, str]] = {
    "pubg": ("PUBG_OWNER_LABELS_PATH", "/root/data/pubg/pubg_owner_labels.json"),
    "standoff": ("STANDOFF_OWNER_LABELS_PATH", "/root/data/standoff/standoff_owner_labels.json"),
    "genshin": ("GENSHIN_OWNER_LABELS_PATH", "/root/data/genshin/genshin_owner_labels.json"),
    "wot": ("WOT_OWNER_LABELS_PATH", "/root/data/wot/wot_owner_labels.json"),
    "mobile_legends": ("MLBB_OWNER_LABELS_PATH", "/root/data/mlbb/mobile_legends_owner_labels.json"),
}

_REPO_SEED: dict[str, str] = {
    "pubg": "pubg_owner_labels.json",
    "standoff": "standoff_owner_labels.json",
    "genshin": "genshin_owner_labels.json",
    "wot": "wot_owner_labels.json",
    "mobile_legends": "mobile_legends_owner_labels.json",
}


def _repo_root() -> Path:
    return Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml"))


def runtime_labels_path(profile: str, *, create: bool = False) -> Path | None:
    from highlight_scorer import normalize_profile
    from path_safe import exists as path_exists

    p = normalize_profile(profile)
    if p not in _PROFILE_RUNTIME:
        return None
    env_key, default = _PROFILE_RUNTIME[p]
    override = os.environ.get(env_key, "").strip()
    path = Path(override) if override else Path(default)
    if create or path_exists(path):
        return path
    return path


def seed_labels_path(profile: str) -> Path:
    from highlight_scorer import normalize_profile

    p = normalize_profile(profile)
    name = _REPO_SEED.get(p, f"{p}_owner_labels.json")
    return _repo_root() / "data" / name


def ensure_runtime_labels(profile: str) -> Path | None:
    """Copy git seed into runtime path when runtime file is missing."""
    from path_safe import is_file as path_is_file

    path = runtime_labels_path(profile, create=True)
    if path is None:
        return None
    if path_is_file(path):
        return path
    seed = seed_labels_path(profile)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # CI / non-root hosts cannot write under /root/data — fall back to seed.
        return seed if path_is_file(seed) else path
    if path_is_file(seed):
        try:
            shutil.copy2(seed, path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["seeded_from"] = str(seed)
            payload["seeded_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            return seed
    else:
        try:
            path.write_text(
                json.dumps({"videos": {}, "seeded_at": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=2),
                encoding="utf-8",
            )
        except OSError:
            return path
    return path


def load_runtime_labels(profile: str) -> dict:
    from path_safe import is_file as path_is_file

    path = ensure_runtime_labels(profile)
    if path is None or not path_is_file(path):
        return {"videos": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"videos": {}}
    if not isinstance(data, dict):
        return {"videos": {}}
    data.setdefault("videos", {})
    return data


def save_runtime_labels(profile: str, data: dict) -> None:
    path = ensure_runtime_labels(profile)
    if path is None:
        return
    prev = load_runtime_labels(profile)
    history = list(prev.get("label_history") or [])
    if prev.get("videos") != data.get("videos"):
        history.append(
            {
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "video_count": len((data.get("videos") or {})),
            }
        )
        history = history[-50:]
    data["label_history"] = history
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    data.pop("seeded_from", None)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def is_repo_checkout_path(path: Path) -> bool:
    """True when path lives inside the git repo data/ tree (should not be written at runtime)."""
    try:
        resolved = path.resolve()
        repo_data = (_repo_root() / "data").resolve()
        return str(resolved).startswith(str(repo_data))
    except OSError:
        return False


__all__ = [
    "ensure_runtime_labels",
    "is_repo_checkout_path",
    "load_runtime_labels",
    "runtime_labels_path",
    "save_runtime_labels",
    "seed_labels_path",
]
