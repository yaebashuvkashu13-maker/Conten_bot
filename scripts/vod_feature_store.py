#!/usr/bin/env python3
"""Unified per-VOD feature store: one PCM decode, shared mmap + metadata cache."""

from __future__ import annotations

import hashlib
import json
import mmap
import os
import time
from pathlib import Path
from typing import Any

import numpy as np

STORE_VERSION = 1
DEFAULT_ROOT = "/root/data/vod_feature_store"
GUN_SAMPLE_RATE = 11025


def store_enabled() -> bool:
    return os.environ.get("VOD_FEATURE_STORE", "1") == "1"


def store_root() -> Path:
    return Path(os.environ.get("VOD_FEATURE_STORE_DIR", DEFAULT_ROOT))


def store_ttl_sec() -> int:
    return max(3600, int(os.environ.get("VOD_FEATURE_STORE_TTL_SEC", str(12 * 3600))))


def _vod_identity(path: Path) -> tuple[str, int, int]:
    p = path.resolve()
    st = p.stat()
    return str(p), int(st.st_mtime_ns), int(st.st_size)


def store_key(path: Path, *, skip_intro: float = 0.0) -> str:
    path_s, mtime_ns, size = _vod_identity(path)
    blob = f"v{STORE_VERSION}|{path_s}|{mtime_ns}|{size}|skip={skip_intro:.1f}"
    return hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()[:32]


def _meta_path(key: str) -> Path:
    return store_root() / "meta" / f"{key}.json"


def _pcm_path(key: str) -> Path:
    return store_root() / "pcm" / f"{key}.s16le"


class VodFeatureStore:
    """Disk-backed PCM + JSON metadata for one VOD scan session."""

    def __init__(self, video_path: Path, *, skip_intro: float = 0.0):
        self.video_path = video_path.resolve()
        self.skip_intro = float(skip_intro)
        self.key = store_key(self.video_path, skip_intro=self.skip_intro)
        self._pcm_mmap: mmap.mmap | None = None
        self._pcm_array: np.ndarray | None = None
        self._meta: dict[str, Any] = {}

    def _load_meta(self) -> dict[str, Any] | None:
        meta_file = _meta_path(self.key)
        if not meta_file.is_file():
            return None
        try:
            payload = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        saved_at = float(payload.get("saved_at") or 0)
        if saved_at <= 0 or (time.time() - saved_at) > store_ttl_sec():
            return None
        if int(payload.get("version") or 0) != STORE_VERSION:
            return None
        return payload

    def has_pcm(self) -> bool:
        return _pcm_path(self.key).is_file() and self._load_meta() is not None

    def pcm_duration_sec(self) -> float:
        meta = self._load_meta() or self._meta
        return float(meta.get("pcm_duration_sec") or 0.0)

    def get_pcm_s16(self, *, copy: bool = False) -> np.ndarray:
        """Return PCM samples. Default is a zero-copy mmap view; set copy=True to own memory."""
        if self._pcm_array is not None and not copy:
            return self._pcm_array
        if self._pcm_array is not None and copy:
            return np.array(self._pcm_array, copy=True)
        pcm_file = _pcm_path(self.key)
        if not pcm_file.is_file():
            return np.array([], dtype=np.int16)
        with pcm_file.open("rb") as handle:
            mm = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
            self._pcm_mmap = mm
            view = np.frombuffer(mm, dtype=np.int16)
            self._pcm_array = view
            return np.array(view, copy=True) if copy else view

    def get_pcm_float(self) -> np.ndarray:
        pcm = self.get_pcm_s16()
        if pcm.size == 0:
            return np.array([], dtype=np.float32)
        return pcm.astype(np.float32) / 32768.0

    def get_feature(self, name: str) -> Any:
        meta = self._load_meta() or self._meta
        features = meta.get("features") or {}
        return features.get(name)

    def put_features(self, features: dict[str, Any]) -> None:
        meta = self._load_meta() or {
            "version": STORE_VERSION,
            "saved_at": time.time(),
            "vod": str(self.video_path),
            "skip_intro": self.skip_intro,
            "features": {},
        }
        merged = dict(meta.get("features") or {})
        merged.update(features)
        meta["features"] = merged
        meta["saved_at"] = time.time()
        self._meta = meta
        meta_file = _meta_path(self.key)
        meta_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = meta_file.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, meta_file)

    def ensure_pcm(self, duration_sec: float) -> bool:
        if self.has_pcm():
            return True
        from vod_audio_batch import extract_vod_pcm_s16

        pcm = extract_vod_pcm_s16(
            self.video_path,
            self.skip_intro,
            max(0.35, float(duration_sec)),
            sample_rate=GUN_SAMPLE_RATE,
        )
        if pcm.size == 0:
            return False
        pcm_file = _pcm_path(self.key)
        pcm_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = pcm_file.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_bytes(pcm.tobytes())
        os.replace(tmp, pcm_file)
        meta = {
            "version": STORE_VERSION,
            "saved_at": time.time(),
            "vod": str(self.video_path),
            "skip_intro": self.skip_intro,
            "pcm_duration_sec": round(float(pcm.size) / GUN_SAMPLE_RATE, 3),
            "sample_rate": GUN_SAMPLE_RATE,
            "features": {},
        }
        self._meta = meta
        meta_file = _meta_path(self.key)
        meta_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_meta = meta_file.with_suffix(f".{os.getpid()}.tmp")
        tmp_meta.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_meta, meta_file)
        self._pcm_array = pcm
        return True

    def slice_pcm_float(self, start_sec: float, duration_sec: float) -> np.ndarray:
        pcm = self.get_pcm_float()
        if pcm.size == 0:
            return pcm
        rel_start = float(start_sec) - self.skip_intro
        i0 = max(0, int(rel_start * GUN_SAMPLE_RATE))
        i1 = min(len(pcm), int((rel_start + float(duration_sec)) * GUN_SAMPLE_RATE))
        return pcm[i0:i1]

    def close(self) -> None:
        if self._pcm_mmap is not None:
            try:
                self._pcm_mmap.close()
            except OSError:
                pass
            self._pcm_mmap = None
        self._pcm_array = None


def open_store(video_path: Path, *, skip_intro: float = 0.0) -> VodFeatureStore | None:
    if not store_enabled():
        return None
    return VodFeatureStore(video_path, skip_intro=skip_intro)


__all__ = ["VodFeatureStore", "open_store", "store_enabled", "store_key"]
