#!/usr/bin/env python3
"""MLBB epic-moment UI detector — YOLO model from frendyrachman/mlbb-ai-clipper."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_MODEL = None
_MODEL_PATH: str | None = None

# High-value montage labels from the HF model (class id -> name).
HIGH_TIER_LABELS = frozenset(
    {
        "Double Kill",
        "Triple Kill",
        "Maniac",
        "Savage",
        "Legendary",
        "God-like",
        "Monster Kill",
        "Unstoppable",
        "Mega Kill",
        "Wiped Out",
        "Killing Spree",
    }
)

MID_TIER_LABELS = frozenset({"Slain", "Shutdown", "First Blood", "Executed", "Destroyed"})


@dataclass
class YoloEpicResult:
    score: float = 0.0
    detected: bool = False
    best_label: str = ""
    best_conf: float = 0.0
    hits: int = 0
    reason: str = "yolo_unavailable"

    def to_dict(self) -> dict:
        return {
            "yolo_epic_score": round(self.score, 4),
            "yolo_epic_label": self.best_label,
            "yolo_epic_conf": round(self.best_conf, 4),
            "yolo_epic_hits": self.hits,
            "yolo_epic_reason": self.reason,
        }


def default_model_path() -> Path:
    root = Path(os.environ.get("MLBB_MODELS_ROOT", "/root/datasets/mlbb/models"))
    nano = root / "mlbb_epic_ui_nano.pt"
    if nano.exists():
        return nano
    full = root / "mlbb_epic_ui.pt"
    if full.exists():
        return full
    return nano


def download_epic_ui_model(*, prefer_nano: bool = True) -> Path:
    """Download YOLO weights from HuggingFace into MLBB_MODELS_ROOT."""
    from huggingface_hub import hf_hub_download

    dest = default_model_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    rel = "train_size_n/weights/best.pt" if prefer_nano else "best.pt"
    cached = hf_hub_download("frendyrachman/mlbb-ai-clipper", rel)
    import shutil

    shutil.copy2(cached, dest)
    return dest


def _load_model():
    global _MODEL, _MODEL_PATH
    if _MODEL is not None:
        return _MODEL
    if os.environ.get("MLBB_YOLO_EPIC_UI", "1") == "0":
        return None
    path = Path(os.environ.get("MLBB_YOLO_EPIC_UI_MODEL", str(default_model_path())))
    if not path.exists():
        try:
            path = download_epic_ui_model(prefer_nano=True)
        except Exception:
            return None
    try:
        from ultralytics import YOLO

        _MODEL = YOLO(str(path))
        _MODEL_PATH = str(path)
        return _MODEL
    except Exception:
        return None


def _label_weight(label: str, conf: float) -> float:
    if label in HIGH_TIER_LABELS:
        return min(1.0, conf * 1.15 + 0.12)
    if label in MID_TIER_LABELS:
        return min(0.75, conf * 0.85)
    if label in ("Victory!", "Defeat"):
        return min(0.35, conf * 0.4)
    return min(0.5, conf * 0.5)


def score_yolo_epic_ui_on_frames(frames: list) -> YoloEpicResult:
    """Run YOLO on sampled BGR frames — detects Savage/Maniac/etc. UI banners."""
    if not frames:
        return YoloEpicResult(reason="no_frames")
    model = _load_model()
    if model is None:
        return YoloEpicResult(reason="model_missing")

    conf_floor = float(os.environ.get("MLBB_YOLO_EPIC_UI_CONF", "0.35"))
    best_label = ""
    best_conf = 0.0
    hits = 0
    score = 0.0

    for frame in frames:
        if frame is None:
            continue
        try:
            results = model.predict(
                frame,
                conf=conf_floor,
                verbose=False,
                device=os.environ.get("MLBB_YOLO_DEVICE", "cpu"),
            )
        except Exception:
            continue
        if not results:
            continue
        boxes = results[0].boxes
        if boxes is None or len(boxes) == 0:
            continue
        for box in boxes:
            cls_id = int(box.cls.item())
            label = str(model.names.get(cls_id, cls_id))
            conf = float(box.conf.item())
            w = _label_weight(label, conf)
            if w > score:
                score = w
                best_label = label
                best_conf = conf
            if label in HIGH_TIER_LABELS or label in MID_TIER_LABELS:
                hits += 1

    if score <= 0:
        return YoloEpicResult(reason="no_detection")
    detected = best_label in HIGH_TIER_LABELS or (best_label in MID_TIER_LABELS and best_conf >= 0.5)
    return YoloEpicResult(
        score=score,
        detected=detected,
        best_label=best_label,
        best_conf=best_conf,
        hits=hits,
        reason=f"yolo:{best_label}:{best_conf:.2f}",
    )


def score_yolo_epic_ui(
    video_path: Path | str,
    start_sec: float,
    duration_sec: float,
    *,
    sample_frames: int | None = None,
) -> YoloEpicResult:
    from mlbb_kill_ui import _sample_frames

    n = sample_frames or int(os.environ.get("MLBB_YOLO_EPIC_UI_SAMPLES", "4"))
    frames = _sample_frames(Path(video_path), start_sec, duration_sec, n)
    return score_yolo_epic_ui_on_frames(frames)
