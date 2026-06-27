#!/usr/bin/env python3
"""Light post-job cleanup: temp files, stale locks, orphan ffmpeg children."""

from __future__ import annotations

import glob
import os
import subprocess
import time
from pathlib import Path


def cleanup_tmp(*, max_age_sec: float = 7200) -> int:
    removed = 0
    now = time.time()
    patterns = (
        "/tmp/mlbb_split_*",
        "/tmp/hero-shorts-*.txt",
        "/tmp/etalon-mlbb-*.txt",
        "/tmp/single-hero-*.txt",
        "/tmp/hero-evening-*.txt",
    )
    for pattern in patterns:
        for path in glob.glob(pattern):
            p = Path(path)
            try:
                if now - p.stat().st_mtime > max_age_sec:
                    if p.is_dir():
                        for child in p.rglob("*"):
                            child.unlink(missing_ok=True)
                        p.rmdir()
                    else:
                        p.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                pass
    return removed


def cleanup_stale_locks() -> int:
    removed = 0
    for lock in Path("/tmp").glob("mlbb_*.lock"):
        try:
            if time.time() - lock.stat().st_mtime > 7200:
                lock.unlink(missing_ok=True)
                removed += 1
        except OSError:
            pass
    return removed


def main() -> int:
    n_tmp = cleanup_tmp()
    n_lock = cleanup_stale_locks()
    subprocess.run(["sync"], check=False)
    print(f"cleanup tmp={n_tmp} locks={n_lock}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
