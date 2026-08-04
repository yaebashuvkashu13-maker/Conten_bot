#!/usr/bin/env python3
"""Hybrid MLBB announcement classifier: OCR rules + visual model + time consensus."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np

FEATURE_VERSION = 1
OWN_STREAK = "own_streak"
ENEMY_STREAK = "enemy_streak"
ALLY_STREAK = "ally_streak"
OBJECTIVE = "objective"
COMMAND = "command"
OTHER = "other"
BLOCKED_KINDS = frozenset({ENEMY_STREAK, ALLY_STREAK, OBJECTIVE, COMMAND, OTHER})

_TIER_PATTERNS: tuple[tuple[re.Pattern[str], int, str], ...] = (
    (re.compile(r"savage|саваж", re.I), 5, "savage"),
    (re.compile(r"maniac|маньяк", re.I), 4, "maniac"),
    (re.compile(r"triple\s*kill|тройн.{0,12}убий", re.I), 3, "triple"),
    (re.compile(r"double\s*kill|двойн.{0,12}убий|ou?ble\s*kill|d0uble", re.I), 2, "double"),
    (re.compile(r"\bkill\b|убийств", re.I), 1, "single"),
)
_ENEMY_RE = re.compile(
    r"\benemy\b|противник|вражеск|has\s+slain\s+(?:you|an\s+ally)|you\s+have\s+been\s+slain",
    re.I,
)
_ALLY_RE = re.compile(r"\bally\b|союзник|our\s+team|teammate", re.I)
_OBJECTIVE_RE = re.compile(
    r"\b(?:lord|turtle)\b|(?:лорд|черепах)|has\s+been\s+slain|"
    r"resurrecting\s+soon|appeared|spawned",
    re.I,
)
_COMMAND_RE = re.compile(
    r"\b(?:gather|retreat|attack|defend|regroup|backup|clear\s+lane|"
    r"on\s+my\s+way|initiate)\b|собрат|отступ|атак|защит|подкреп",
    re.I,
)


@dataclass(frozen=True)
class EventDecision:
    kind: str
    confidence: float
    tier: int = 0
    label: str = ""
    text: str = ""
    source: str = "rules"
    tier_confidence: float = 0.0


def normalize_text(text: str) -> str:
    return " ".join(str(text or "").split())[:240]


def classify_event_text(text: str) -> EventDecision:
    """Classify OCR text with hard-negative rules before streak keywords."""
    blob = normalize_text(text)
    if not blob:
        return EventDecision(OTHER, 0.0, text="")
    if _OBJECTIVE_RE.search(blob):
        return EventDecision(OBJECTIVE, 1.0, label="objective", text=blob)
    if _COMMAND_RE.search(blob):
        return EventDecision(COMMAND, 1.0, label="command", text=blob)
    if _ENEMY_RE.search(blob):
        return EventDecision(ENEMY_STREAK, 1.0, label="enemy", text=blob)
    if _ALLY_RE.search(blob):
        return EventDecision(ALLY_STREAK, 1.0, label="ally", text=blob)
    for pattern, tier, label in _TIER_PATTERNS:
        if pattern.search(blob):
            return EventDecision(
                OWN_STREAK,
                1.0,
                tier=tier,
                label=label,
                text=blob,
                source="ocr_rules",
                tier_confidence=1.0,
            )
    return EventDecision(OTHER, 0.25, text=blob)


def extract_banner_patch(frame: np.ndarray) -> np.ndarray:
    """Return the normalized 160×48 top-center announcement patch."""
    import cv2

    image = np.asarray(frame)
    if image.ndim != 3 or image.shape[0] < 2 or image.shape[1] < 2:
        raise ValueError("expected BGR image")
    if image.shape[0] <= 64 and image.shape[1] <= 220:
        patch = image
    else:
        small = cv2.resize(image, (480, 270), interpolation=cv2.INTER_AREA)
        h, w = small.shape[:2]
        patch = small[int(h * 0.02) : int(h * 0.30), int(w * 0.10) : int(w * 0.90)]
    return cv2.resize(patch, (160, 48), interpolation=cv2.INTER_AREA)


def extract_visual_features(frame: np.ndarray) -> np.ndarray:
    """Compact HOG + HSV + color/edge features used by train and inference."""
    import cv2

    patch = extract_banner_patch(frame)
    gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    magnitude, angle = cv2.cartToPolar(gx, gy, angleInDegrees=True)
    angle = np.mod(angle, 180.0)
    cells: list[np.ndarray] = []
    for y0 in range(0, 48, 8):
        for x0 in range(0, 160, 8):
            cell_angle = angle[y0 : y0 + 8, x0 : x0 + 8]
            cell_magnitude = magnitude[y0 : y0 + 8, x0 : x0 + 8]
            bins = np.minimum((cell_angle / 20.0).astype(np.int32), 8)
            histogram = np.bincount(
                bins.reshape(-1),
                weights=cell_magnitude.reshape(-1),
                minlength=9,
            ).astype(np.float32)
            histogram /= float(np.linalg.norm(histogram) + 1e-6)
            cells.append(histogram)
    hog_features = np.concatenate(cells)

    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [12, 8], [0, 180, 0, 256]).reshape(-1)
    hist = cv2.normalize(hist, None).reshape(-1).astype(np.float32)

    gold = cv2.inRange(hsv, np.array([8, 90, 130]), np.array([42, 255, 255]))
    white = cv2.inRange(hsv, np.array([0, 0, 195]), np.array([180, 65, 255]))
    red_a = cv2.inRange(hsv, np.array([0, 80, 90]), np.array([10, 255, 255]))
    red_b = cv2.inRange(hsv, np.array([168, 80, 90]), np.array([180, 255, 255]))
    cyan = cv2.inRange(hsv, np.array([80, 65, 80]), np.array([110, 255, 255]))
    edges = cv2.Canny(gray, 70, 160)
    denom = float(max(1, patch.shape[0] * patch.shape[1]))
    summary = np.asarray(
        [
            np.count_nonzero(gold) / denom,
            np.count_nonzero(white) / denom,
            np.count_nonzero(red_a | red_b) / denom,
            np.count_nonzero(cyan) / denom,
            np.count_nonzero(edges) / denom,
            float(gray.mean()) / 255.0,
            float(gray.std()) / 128.0,
        ],
        dtype=np.float32,
    )
    return np.concatenate((hog_features, hist, summary)).astype(np.float32)


_MODEL_CACHE: tuple[str, int, dict | None] | None = None


def default_model_path() -> Path:
    repo = Path(os.environ.get("CONTENT_BOT_REPO", Path(__file__).resolve().parents[1]))
    return Path(os.environ.get("MLBB_EVENT_MODEL_PATH", repo / "data" / "mlbb_event_classifier.joblib"))


def _load_model_artifact(path: Path | None = None) -> dict | None:
    global _MODEL_CACHE
    model_path = path or default_model_path()
    if not model_path.exists():
        return None
    mtime = model_path.stat().st_mtime_ns
    key = str(model_path.resolve())
    if _MODEL_CACHE and _MODEL_CACHE[0] == key and _MODEL_CACHE[1] == mtime:
        return _MODEL_CACHE[2]
    try:
        import joblib

        artifact = joblib.load(model_path)
    except Exception:
        artifact = None
    if not isinstance(artifact, dict) or artifact.get("feature_version") != FEATURE_VERSION:
        artifact = None
    _MODEL_CACHE = (key, mtime, artifact)
    return artifact


def _predict_proba(model, features: np.ndarray) -> tuple[str, float]:
    probabilities = np.asarray(model.predict_proba(features.reshape(1, -1))[0], dtype=float)
    index = int(np.argmax(probabilities))
    return str(model.classes_[index]), float(probabilities[index])


def predict_visual_event(frame: np.ndarray, *, artifact: dict | None = None) -> EventDecision | None:
    if os.environ.get("MLBB_EVENT_CLASSIFIER", "1") != "1":
        return None
    artifact = artifact if artifact is not None else _load_model_artifact()
    if not artifact:
        return None
    features = extract_visual_features(frame)
    event_model = artifact["event_model"]
    probabilities = np.asarray(
        event_model.predict_proba(features.reshape(1, -1))[0],
        dtype=float,
    )
    classes = np.asarray(event_model.classes_, dtype=str)
    own_matches = np.flatnonzero(classes == OWN_STREAK)
    own_index = int(own_matches[0]) if own_matches.size else -1
    own_probability = float(probabilities[own_index]) if own_index >= 0 else 0.0
    own_threshold = float(artifact.get("own_threshold") or 0.5)
    if own_index >= 0 and own_probability >= own_threshold:
        kind, confidence = OWN_STREAK, own_probability
    else:
        candidates = [
            index for index, label in enumerate(classes) if label != OWN_STREAK
        ]
        index = max(candidates, key=lambda item: float(probabilities[item]))
        kind, confidence = str(classes[index]), float(probabilities[index])
    tier = 0
    tier_confidence = 0.0
    label = kind
    tier_model = artifact.get("tier_model")
    if kind == OWN_STREAK and tier_model is not None:
        tier_label, tier_confidence = _predict_proba(tier_model, features)
        tier = int(tier_label)
        label = {2: "double", 3: "triple", 4: "maniac", 5: "savage"}.get(tier, "streak")
    return EventDecision(
        kind,
        confidence,
        tier=tier,
        label=label,
        source="event_model",
        tier_confidence=tier_confidence,
    )


def classify_event(text: str, frame: np.ndarray | None = None) -> EventDecision:
    """Fuse hard OCR rules with visual evidence; blocked rules always win."""
    rule = classify_event_text(text)
    visual = predict_visual_event(frame) if frame is not None else None
    block_min = float(os.environ.get("MLBB_EVENT_BLOCK_MIN_CONF", "0.72"))
    own_min = float(os.environ.get("MLBB_EVENT_OWN_MIN_CONF", "0.86"))
    tier_min = float(os.environ.get("MLBB_EVENT_TIER_MIN_CONF", "0.62"))

    if rule.kind in {ENEMY_STREAK, ALLY_STREAK, OBJECTIVE, COMMAND}:
        return rule
    if rule.kind == OWN_STREAK:
        if visual and visual.kind != OWN_STREAK and visual.confidence >= block_min:
            return EventDecision(
                visual.kind,
                visual.confidence,
                label=visual.label,
                text=rule.text,
                source="visual_veto",
            )
        return rule
    if visual and visual.kind == OWN_STREAK:
        if (
            visual.confidence >= own_min
            and visual.tier >= 2
            and visual.tier_confidence >= tier_min
        ):
            return visual
    if visual and visual.kind in BLOCKED_KINDS and visual.confidence >= block_min:
        return visual
    return rule


def temporal_consensus(
    decisions: Iterable[tuple[float, EventDecision]],
    *,
    min_model_frames: int = 2,
    max_span_sec: float = 2.5,
) -> EventDecision | None:
    """Require repeated visual-only evidence; one explicit OCR hit remains sufficient."""
    rows = sorted(decisions, key=lambda row: row[0])
    own_ocr = [decision for _, decision in rows if decision.kind == OWN_STREAK and decision.source != "event_model"]
    if own_ocr:
        return max(own_ocr, key=lambda decision: (decision.tier, decision.confidence))
    model_own = [(sec, decision) for sec, decision in rows if decision.kind == OWN_STREAK]
    for index, (sec, decision) in enumerate(model_own):
        cluster = [
            item
            for later_sec, item in model_own[index:]
            if later_sec - sec <= max_span_sec and item.tier == decision.tier
        ]
        if len(cluster) >= min_model_frames:
            return max(cluster, key=lambda item: (item.confidence, item.tier_confidence))
    return None
