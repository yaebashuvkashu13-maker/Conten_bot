#!/usr/bin/env python3
"""
Read MLBB kill-banner text (TRIPLE / MANIAC / SAVAGE / …).

Tesseract is usually blind on YouTube gold outline glyphs. RapidOCR + fuzzy
label matching recovers common OCR garbage (SAWAGE→SAVAGE, DOUBLKILL→DOUBLE).
"""

from __future__ import annotations

import difflib
import logging
import os
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

log = logging.getLogger("mlbb_banner_ocr")

# Canonical Latin labels → (tier, short name). Fuzzy match ignores spaces.
_BANNER_LABELS: tuple[tuple[str, int, str], ...] = (
    ("SAVAGE", 5, "savage"),
    ("LEGENDARY", 5, "legendary"),
    ("MANIAC", 4, "maniac"),
    ("PENTA KILL", 4, "maniac"),
    ("RUTHLESS", 4, "ruthless"),
    ("TRIPLE KILL", 3, "triple"),
    ("QUADRA KILL", 3, "triple"),
    ("ULTRA KILL", 3, "triple"),
    ("GODLIKE", 3, "triple"),
    ("DOUBLE KILL", 2, "double"),
    ("UNSTOPPABLE", 2, "double"),
    ("DOMINATING", 2, "double"),
    ("KILLING SPREE", 1, "single"),
    ("FIRST BLOOD", 1, "single"),
    ("SHUT DOWN", 1, "single"),
    ("RAMPAGE", 1, "single"),
    ("HAS BEEN SLAIN", 1, "single"),
)

# Frequent RapidOCR / Tesseract misreads → canonical label letters.
_OCR_ALIASES: dict[str, str] = {
    "SAWAGE": "SAVAGE",
    "SAVAG": "SAVAGE",
    "SAVAGF": "SAVAGE",
    "MANIAG": "MANIAC",
    "MANLAG": "MANIAC",
    "MANIA": "MANIAC",
    "TRIPLEKILL": "TRIPLEKILL",
    "TRIBLEKILL": "TRIPLEKILL",
    "TRLPLEKILL": "TRIPLEKILL",
    "DOUBLEKILL": "DOUBLEKILL",
    "DOUBLKILL": "DOUBLEKILL",
    "DOUBEKILL": "DOUBLEKILL",
    "D0UBLEKILL": "DOUBLEKILL",
    "UNSTOPPABLE": "UNSTOPPABLE",
    "USTENE": "UNSTOPPABLE",
    "UNSTOPABLE": "UNSTOPPABLE",
    "LEGENDARY": "LEGENDARY",
    "LEGENDAR": "LEGENDARY",
    "FIRSTBLOOD": "FIRSTBLOOD",
    "SHUTDOWN": "SHUTDOWN",
    "KILLINGSPREE": "KILLINGSPREE",
}

_RAPID: Any | None = None
_RAPID_TRIED = False


def _letters(s: str) -> str:
    return re.sub(r"[^A-Z]", "", str(s or "").upper())


def _inject_rapid_site_packages() -> None:
    """Allow system python to import RapidOCR from the OCR venv."""
    env = os.environ.get("MLBB_RAPID_OCR_SITE", "").strip()
    cands: list[Path] = []
    if env:
        cands.append(Path(env))
    repo = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml"))
    cands.extend(repo.glob(".venv_ocr/lib/python*/site-packages"))
    cands.extend(Path("/root/content_bot_ml").glob(".venv_ocr/lib/python*/site-packages"))
    for sp in cands:
        if sp.is_dir() and str(sp) not in sys.path:
            sys.path.insert(0, str(sp))
            return


def rapid_ocr_available() -> bool:
    if os.environ.get("MLBB_BANNER_RAPID_OCR", "1") != "1":
        return False
    eng = os.environ.get("MLBB_BANNER_OCR_ENGINE", "auto").strip().lower()
    if eng in {"tess", "tesseract", "off", "0"}:
        return False
    try:
        return _rapid_engine() is not None
    except Exception:
        return False


def _rapid_engine():
    global _RAPID, _RAPID_TRIED
    if _RAPID is not None:
        return _RAPID
    if _RAPID_TRIED:
        return None
    _RAPID_TRIED = True
    try:
        _inject_rapid_site_packages()
        from rapidocr_onnxruntime import RapidOCR

        _RAPID = RapidOCR()
        return _RAPID
    except Exception as exc:
        log.info("RapidOCR unavailable: %s", exc)
        _RAPID = None
        return None


def fuzzy_match_banner_label(
    text: str,
    *,
    min_score: float | None = None,
) -> tuple[float, str, int, str] | None:
    """
    Map OCR garbage to a known kill-banner label.

    Returns (score, canonical_label, tier, short_name) or None.
    """
    thr = float(
        min_score
        if min_score is not None
        else os.environ.get("MLBB_BANNER_OCR_FUZZY_MIN", "0.72")
    )
    raw = " ".join(str(text or "").split())
    if sum(ch.isalpha() for ch in raw) < 4:
        return None
    blob = _letters(raw)
    if len(blob) < 4:
        return None

    # Alias rewrite on whole blob and tokens.
    tokens = [_letters(t) for t in re.findall(r"[A-Za-z0-9]{3,}", raw)]
    tokens = [t for t in tokens if len(t) >= 3]
    for tok in list(tokens):
        alias = _OCR_ALIASES.get(tok)
        if alias and alias not in tokens:
            tokens.append(alias)
            blob = blob.replace(tok, alias) if tok in blob else (blob + alias)

    best: tuple[float, str, int, str] | None = None
    for label, tier, name in _BANNER_LABELS:
        L = _letters(label)
        if not L:
            continue
        score = 0.0
        if len(blob) >= max(4, int(len(L) * 0.7)) and L in blob:
            score = 0.98
        score = max(score, difflib.SequenceMatcher(None, L, blob).ratio())
        if len(blob) >= len(L):
            win = len(L)
            for i in range(0, len(blob) - win + 1):
                score = max(
                    score,
                    difflib.SequenceMatcher(None, L, blob[i : i + win]).ratio(),
                )
        for tok in tokens:
            if abs(len(tok) - len(L)) > max(3, len(L) // 2):
                continue
            score = max(score, difflib.SequenceMatcher(None, L, tok).ratio())
            if tok == L or (len(tok) >= len(L) - 1 and (L in tok or tok in L)):
                score = max(score, 0.92)
        if best is None or score > best[0]:
            best = (score, label, tier, name)
    if best is None or best[0] < thr:
        return None
    return best


def extract_banner_text_zone(frame):
    """Upper HUD strip where kill-streak announcements sit."""
    import cv2

    if frame is None:
        return None
    h, w = frame.shape[:2]
    if h < 40 or w < 80:
        return None
    y0, y1 = int(h * 0.02), int(h * 0.30)
    x0, x1 = int(w * 0.10), int(w * 0.90)
    zone = frame[y0:y1, x0:x1]
    if zone.size == 0:
        return None
    # Prefer readable height for stylized gold glyphs.
    target_h = max(72, int(os.environ.get("MLBB_BANNER_OCR_TARGET_H", "96")))
    if zone.shape[0] < target_h:
        scale = target_h / float(zone.shape[0])
        zone = cv2.resize(
            zone,
            (max(8, int(zone.shape[1] * scale)), target_h),
            interpolation=cv2.INTER_CUBIC,
        )
    return zone


def _ocr_variants(zone) -> list[tuple[str, object]]:
    import cv2
    import numpy as np

    variants: list[tuple[str, object]] = [("up", zone)]
    if os.environ.get("MLBB_BANNER_OCR_GOLD_MASK", "1") == "1":
        hsv = cv2.cvtColor(zone, cv2.COLOR_BGR2HSV)
        gold = cv2.inRange(hsv, np.array([5, 40, 100]), np.array([45, 255, 255]))
        white = cv2.inRange(hsv, np.array([0, 0, 170]), np.array([180, 90, 255]))
        mask = cv2.bitwise_or(gold, white)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        masked = cv2.bitwise_and(zone, zone, mask=mask)
        variants.append(("mask", masked))
    return variants


def _rapid_read_zone(zone) -> str:
    engine = _rapid_engine()
    if engine is None or zone is None:
        return ""
    texts: list[str] = []
    for _name, img in _ocr_variants(zone):
        try:
            result, _ = engine(img)
        except Exception:
            continue
        chunk = " ".join(str(row[1]) for row in (result or []) if row and len(row) > 1)
        if chunk:
            texts.append(chunk)
            if fuzzy_match_banner_label(chunk) is not None:
                break
    return " ".join(texts)


def _tesseract_read_zone(zone) -> str:
    import cv2

    try:
        import pytesseract
    except ImportError:
        return ""
    if zone is None:
        return ""
    gray = cv2.cvtColor(zone, cv2.COLOR_BGR2GRAY)
    try:
        clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
        gray = clahe.apply(gray)
    except Exception:
        pass
    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    texts: list[str] = []
    timeout = max(2, int(os.environ.get("MLBB_TESSERACT_TIMEOUT_SEC", "6") or "6"))
    for psm in (7, 6):
        try:
            text = pytesseract.image_to_string(
                otsu,
                config=f"--psm {psm} -l eng",
                timeout=timeout,
            )
        except Exception:
            continue
        text = " ".join(text.split())
        if text:
            texts.append(text)
            if fuzzy_match_banner_label(text) is not None:
                break
    return " ".join(texts)


def read_banner_text(frame, *, prefer_rapid: bool = True) -> str:
    """OCR the kill-banner HUD strip. Prefer RapidOCR; fall back to Tesseract."""
    zone = extract_banner_text_zone(frame)
    if zone is None:
        return ""
    parts: list[str] = []
    if prefer_rapid and rapid_ocr_available():
        parts.append(_rapid_read_zone(zone))
    if not any(fuzzy_match_banner_label(p) for p in parts):
        tess = _tesseract_read_zone(zone)
        if tess:
            parts.append(tess)
    return " ".join(p for p in parts if p).strip()


@lru_cache(maxsize=1)
def _engine_probe_cached() -> bool:
    return rapid_ocr_available()


def ocr_engine_ready() -> bool:
    """True when RapidOCR (or forced tess) can actually read banners."""
    return _engine_probe_cached()
