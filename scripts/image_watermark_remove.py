#!/usr/bin/env python3
"""Detect watermark phrases on images (OCR) and inpaint them out."""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path

import cv2
import numpy as np

WATERMARK_DIR = Path("/root/data/mlbb/watermark_examples")
PHRASES_FILE = Path("/root/data/mlbb/watermark_phrases.txt")

DEFAULT_PHRASES = [
    "god of mlbb",
    "godofmlbb",
    "god of  mlbb",
]

_phrase_list: list[str] | None = None
_easyocr_reader = None


def load_phrases() -> list[str]:
    global _phrase_list
    if _phrase_list is not None:
        return _phrase_list
    phrases = list(DEFAULT_PHRASES)
    env = os.environ.get("IG_WATERMARK_PHRASES", "")
    if env:
        phrases.extend(p.strip() for p in env.split(",") if p.strip())
    if PHRASES_FILE.exists():
        for line in PHRASES_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                phrases.append(line)
    seen: set[str] = set()
    ordered: list[str] = []
    for p in sorted({p.lower() for p in phrases}, key=len, reverse=True):
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    _phrase_list = ordered
    return ordered


def _normalize_text(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _bottom_frac() -> float:
    return float(os.environ.get("IG_WATERMARK_BOTTOM_FRAC", "0.28"))


def _min_center_y_frac() -> float:
    return float(os.environ.get("IG_WATERMARK_MIN_Y_FRAC", "0.68"))


def _max_box_area_frac() -> float:
    return float(os.environ.get("IG_WATERMARK_MAX_AREA_FRAC", "0.12"))


def _preprocess_gray(image_bgr: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.convertScaleAbs(gray, alpha=1.3, beta=8)
    return cv2.bilateralFilter(gray, 5, 40, 40)


def _dedupe_words(words: list[dict]) -> list[dict]:
    seen: set[tuple[int, int, str]] = set()
    out: list[dict] = []
    for w in words:
        key = (w["left"] // 8, w["top"] // 8, _normalize_text(w["text"]))
        if not key[2] or key in seen:
            continue
        seen.add(key)
        out.append(w)
    return out


def _filter_boxes(
    boxes: list[tuple[int, int, int, int]], width: int, height: int
) -> list[tuple[int, int, int, int]]:
    if not boxes:
        return []
    img_area = width * height
    max_area = img_area * _max_box_area_frac()
    min_cy = int(height * _min_center_y_frac())
    kept: list[tuple[int, int, int, int]] = []
    for x, y, bw, bh in boxes:
        if bw <= 0 or bh <= 0:
            continue
        area = bw * bh
        if area > max_area:
            continue
        if bh > height * 0.22 or bw > width * 0.92:
            continue
        cy = y + bh // 2
        if cy < min_cy:
            continue
        kept.append((max(0, x), max(0, y), min(bw, width - x), min(bh, height - y)))
    return _merge_boxes(kept)


def _merge_boxes(boxes: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    if not boxes:
        return []
    merged: list[tuple[int, int, int, int]] = []
    for x, y, w, h in sorted(boxes, key=lambda b: (b[1], b[0])):
        placed = False
        for i, (mx, my, mw, mh) in enumerate(merged):
            if abs(x - mx) < 24 and abs(y - my) < 24:
                x2 = max(mx + mw, x + w)
                y2 = max(my + mh, y + h)
                merged[i] = (min(mx, x), min(my, y), x2 - min(mx, x), y2 - min(my, y))
                placed = True
                break
        if not placed:
            merged.append((x, y, w, h))
    return merged


def _ocr_data_pytesseract(image_bgr: np.ndarray, y_offset: int = 0) -> list[dict]:
    try:
        import pytesseract
        from pytesseract import Output
    except ImportError:
        return []

    gray = _preprocess_gray(image_bgr)
    rows: list[dict] = []
    try:
        data = pytesseract.image_to_data(gray, output_type=Output.DICT, config="--psm 11 -l eng")
    except Exception:
        return []
    n = len(data["text"])
    for i in range(n):
        txt = (data["text"][i] or "").strip()
        if not txt:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1
        if conf >= 0 and conf < 40:
            continue
        rows.append(
            {
                "text": txt,
                "left": int(data["left"][i]),
                "top": int(data["top"][i]) + y_offset,
                "width": int(data["width"][i]),
                "height": int(data["height"][i]),
            }
        )
    return rows


def _ocr_data(image_bgr: np.ndarray, y_offset: int = 0) -> list[dict]:
    return _ocr_data_pytesseract(image_bgr, y_offset=y_offset)


def _phrase_in_ocr(
    phrase: str, words: list[dict], width: int, height: int
) -> list[tuple[int, int, int, int]]:
    if not words:
        return []
    phrase_norm = _normalize_text(phrase)
    phrase_tokens = phrase_norm.split()
    if not phrase_tokens:
        return []

    boxes: list[tuple[int, int, int, int]] = []
    tokens = [_normalize_text(w["text"]) for w in words]
    max_h_span = int(height * 0.14)
    max_w_span = int(width * 0.85)

    for w in words:
        if phrase_norm in _normalize_text(w["text"]):
            pad = max(4, int(w["height"] * 0.15))
            boxes.append(
                (
                    max(0, w["left"] - pad),
                    max(0, w["top"] - pad),
                    w["width"] + 2 * pad,
                    w["height"] + 2 * pad,
                )
            )

    for i in range(len(words)):
        if tokens[i] != phrase_tokens[0]:
            continue
        matched = True
        for j, pt in enumerate(phrase_tokens[1:], start=1):
            if i + j >= len(tokens) or tokens[i + j] != pt:
                matched = False
                break
        if not matched:
            continue
        chunk = words[i : i + len(phrase_tokens)]
        x0 = min(c["left"] for c in chunk)
        y0 = min(c["top"] for c in chunk)
        x1 = max(c["left"] + c["width"] for c in chunk)
        y1 = max(c["top"] + c["height"] for c in chunk)
        if (y1 - y0) > max_h_span or (x1 - x0) > max_w_span:
            continue
        pad = max(6, int((y1 - y0) * 0.12))
        boxes.append((max(0, x0 - pad), max(0, y0 - pad), (x1 - x0) + 2 * pad, (y1 - y0) + 2 * pad))

    if "god" in phrase_tokens and "mlbb" in phrase_norm:
        for i, tok in enumerate(tokens):
            if tok not in ("god", "goo"):
                continue
            window = tokens[i : i + 5]
            joined = " ".join(window)
            if "mlbb" not in joined and "ml" not in joined:
                continue
            chunk = words[i : min(len(words), i + len(window))]
            if len(chunk) < 2:
                continue
            x0 = min(c["left"] for c in chunk)
            y0 = min(c["top"] for c in chunk)
            x1 = max(c["left"] + c["width"] for c in chunk)
            y1 = max(c["top"] + c["height"] for c in chunk)
            if (y1 - y0) > max_h_span or (x1 - x0) > max_w_span:
                continue
            pad = max(8, int((y1 - y0) * 0.15))
            boxes.append((max(0, x0 - pad), max(0, y0 - pad), (x1 - x0) + 2 * pad, (y1 - y0) + 2 * pad))
    return boxes


def _regex_fallback_boxes(
    roi_bgr: np.ndarray, y_offset: int, phrase: str
) -> list[tuple[int, int, int, int]]:
    try:
        import pytesseract
    except ImportError:
        return []

    rh, rw = roi_bgr.shape[:2]
    gray = _preprocess_gray(roi_bgr)
    try:
        full = pytesseract.image_to_string(gray, config="--psm 11 -l eng")
    except Exception:
        return []

    norm = _normalize_text(full)
    phrase_norm = _normalize_text(phrase)
    hit = phrase_norm in norm
    if not hit and "god" in phrase_norm:
        hit = "god" in norm and "mlbb" in norm
    if not hit:
        return []

    bh = max(28, int(rh * 0.55))
    bw = max(100, int(rw * 0.55))
    x = max(0, (rw - bw) // 2)
    y = max(0, rh - bh)
    return [(x, y + y_offset, min(bw, rw - x), min(bh, rh - y))]


def _find_red_markup_boxes(image_bgr: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Training hint: owner draws a red box around the watermark — use it as exact mask."""
    h, w = image_bgr.shape[:2]
    hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (0, 120, 120), (12, 255, 255)) | cv2.inRange(hsv, (165, 120, 120), (180, 255, 255))
    b, g, r = cv2.split(image_bgr)
    mask |= ((r.astype(np.int16) > 150) & (g < 110) & (b < 110)).astype(np.uint8) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    mask = cv2.dilate(mask, kernel, iterations=2)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes: list[tuple[int, int, int, int]] = []
    min_area = max(400, int(w * h * 0.0015))
    for cnt in contours:
        x, y, bw, bh = cv2.boundingRect(cnt)
        if bw * bh < min_area:
            continue
        pad = max(2, int(min(bw, bh) * 0.05))
        boxes.append((max(0, x - pad), max(0, y - pad), bw + 2 * pad, bh + 2 * pad))
    return _filter_boxes(_merge_boxes(boxes), w, h)


def find_watermark_boxes(image_bgr: np.ndarray) -> list[tuple[int, int, int, int]]:
    h, w = image_bgr.shape[:2]
    red_boxes = _find_red_markup_boxes(image_bgr)
    if red_boxes:
        logging.debug("watermark boxes from red markup: %s", red_boxes)
        return red_boxes

    frac = _bottom_frac()
    crop_y = int(h * (1.0 - frac))
    roi = image_bgr[crop_y:, :]
    words = _dedupe_words(_ocr_data(roi, y_offset=crop_y))

    all_boxes: list[tuple[int, int, int, int]] = []
    for phrase in load_phrases():
        phrase_boxes = _phrase_in_ocr(phrase, words, w, h)
        all_boxes.extend(phrase_boxes)
        if not phrase_boxes:
            all_boxes.extend(_regex_fallback_boxes(roi, crop_y, phrase))

    return _filter_boxes(_merge_boxes(all_boxes), w, h)


def _fill_from_above(image_bgr: np.ndarray, x: int, y: int, bw: int, bh: int) -> None:
    """Replace bottom overlay by stretching pixels from just above (keeps game frame)."""
    h, w = image_bgr.shape[:2]
    x2 = min(w, x + bw)
    src_h = min(bh, y, max(4, bh))
    y0 = y - src_h
    src = image_bgr[y0:y, x:x2]
    if src.size == 0:
        return
    patch = cv2.resize(src, (x2 - x, bh), interpolation=cv2.INTER_LINEAR)
    image_bgr[y : y + bh, x:x2] = patch


def remove_watermarks(image_bgr: np.ndarray) -> tuple[np.ndarray, bool]:
    boxes = find_watermark_boxes(image_bgr)
    if not boxes:
        return image_bgr, False

    h, w = image_bgr.shape[:2]
    out = image_bgr.copy()
    inpaint_mask = np.zeros((h, w), dtype=np.uint8)

    for x, y, bw, bh in boxes:
        cy = y + bh // 2
        if cy >= int(h * 0.72) and bw >= int(w * 0.35):
            _fill_from_above(out, x, y, bw, bh)
        else:
            x2 = min(w, x + bw)
            y2 = min(h, y + bh)
            inpaint_mask[y:y2, x:x2] = 255

    if inpaint_mask.any():
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        inpaint_mask = cv2.dilate(inpaint_mask, kernel, iterations=1)
        out = cv2.inpaint(out, inpaint_mask, 3, cv2.INPAINT_TELEA)

    return out, True


def clean_image_file(path: Path) -> tuple[Path, bool]:
    img = cv2.imread(str(path))
    if img is None:
        return path, False
    cleaned, changed = remove_watermarks(img)
    if not changed:
        return path, False
    out = path.parent / f"{path.stem}_clean{path.suffix}"
    cv2.imwrite(str(out), cleaned, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    return out, True


def clean_image_url(url: str) -> tuple[Path, bool]:
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        fd, tmp = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        return Path(tmp), False
    cleaned, changed = remove_watermarks(img)
    fd, tmp = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    out = Path(tmp)
    cv2.imwrite(str(out), cleaned if changed else img, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    return out, changed
