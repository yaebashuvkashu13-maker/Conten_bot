"""Path helpers that treat PermissionError like missing (GitHub CI /root)."""

from __future__ import annotations

from pathlib import Path


def is_file(path: Path | str) -> bool:
    try:
        return Path(path).is_file()
    except OSError:
        return False


def exists(path: Path | str) -> bool:
    try:
        return Path(path).exists()
    except OSError:
        return False


def is_dir(path: Path | str) -> bool:
    try:
        return Path(path).is_dir()
    except OSError:
        return False
