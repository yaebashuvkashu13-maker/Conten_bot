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
    "god of ml",
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


def _preprocess_gray(image_bgr: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.convertScaleAbs(gray, alpha=1.25, beta=12)
    return cv2.bilateralFilter(gray, 5, 40, 40)


def _merge_boxes(boxes: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    if not boxes:
        return []
    merged: list[tuple[int, int, int, int]] = []
    for x, y, w, h in sorted(boxes):
        placed = False
        for i, (mx, my, mw, mh) in enumerate(merged):
            if abs(x - mx) < 40 and abs(y - my) < 40:
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
    for psm in (11, 6, 3):
        try:
            data = pytesseract.image_to_data(
                gray, output_type=Output.DICT, config=f"--psm {psm} -l eng"
            )
        except Exception:
            continue
        n = len(data["text"])
        for i in range(n):
            txt = (data["text"][i] or "").strip()
            if not txt:
                continue
            try:
                conf = float(data["conf"][i])
            except (TypeError, ValueError):
                conf = -1
            if conf >= 0 and conf < 30:
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


def _ocr_data_easyocr(image_bgr: np.ndarray, y_offset: int = 0) -> list[dict]:
    global _easyocr_reader
    try:
        import easyocr
    except ImportError:
        return []
    try:
        if _easyocr_reader is None:
            _easyocr_reader = easyocr.Reader(["en"], gpu=False, verbose=False)
        results = _easyocr_reader.readtext(image_bgr)
    except Exception as exc:
        logging.debug("easyocr failed: %s", exc)
        return []
    rows: list[dict] = []
    for bbox, text, conf in results:
        if conf < 0.3:
            continue
        xs = [int(p[0]) for p in bbox]
        ys = [int(p[1]) for p in bbox]
        rows.append(
            {
                "text": text.strip(),
                "left": min(xs),
                "top": min(ys) + y_offset,
                "width": max(xs) - min(xs),
                "height": max(ys) - min(ys),
            }
        )
    return rows


def _ocr_data(image_bgr: np.ndarray, y_offset: int = 0) -> list[dict]:
    rows = _ocr_data_pytesseract(image_bgr, y_offset=y_offset)
    if len(rows) < 2:
        rows.extend(_ocr_data_easyocr(image_bgr, y_offset=y_offset))
    return rows


def _phrase_in_ocr(phrase: str, words: list[dict]) -> list[tuple[int, int, int, int]]:
    if not words:
        return []
    phrase_norm = _normalize_text(phrase)
    phrase_tokens = phrase_norm.split()
    if not phrase_tokens:
        return []

    boxes: list[tuple[int, int, int, int]] = []
    tokens = [_normalize_text(w["text"]) for w in words]

    for w in words:
        if phrase_norm in _normalize_text(w["text"]):
            boxes.append((w["left"], w["top"], w["width"], w["height"]))

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
        pad = max(8, int((y1 - y0) * 0.2))
        boxes.append((max(0, x0 - pad), max(0, y0 - pad), (x1 - x0) + 2 * pad, (y1 - y0) + 2 * pad))

    # fuzzy: god ... mlbb within a few words
    if "god" in phrase_tokens and "mlbb" in phrase_norm:
        for i, tok in enumerate(tokens):
            if tok not in ("god", "goo"):
                continue
            window = tokens[i : i + 6]
            joined = " ".join(window)
            if "mlbb" in joined or ("ml" in joined and "bb" in joined):
                chunk = words[i : min(len(words), i + len(window))]
                if chunk:
                    x0 = min(c["left"] for c in chunk)
                    y0 = min(c["top"] for c in chunk)
                    x1 = max(c["left"] + c["width"] for c in chunk)
                    y1 = max(c["top"] + c["height"] for c in chunk)
                    pad = max(10, int((y1 - y0) * 0.25))
                    boxes.append(
                        (max(0, x0 - pad), max(0, y0 - pad), (x1 - x0) + 2 * pad, (y1 - y0) + 2 * pad)
                    )
    return boxes


def _regex_fallback_boxes(image_bgr: np.ndarray, phrase: str) -> list[tuple[int, int, int, int]]:
    """When OCR finds text but not word boxes (stylized fonts), use full-string match + layout."""
    try:
        import pytesseract
    except ImportError:
        return []

    h, w = image_bgr.shape[:2]
    gray = _preprocess_gray(image_bgr)
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

    # Typical overlay: lower third, centered (Instagram MLBB montage watermarks)
    bh = max(32, int(h * 0.11))
    bw = max(120, int(w * 0.62))
    x = max(0, (w - bw) // 2)
    y = max(0, h - int(h * 0.16) - bh // 2)
    return [(x, y, min(bw, w - x), min(bh, h - y))]


def find_watermark_boxes(image_bgr: np.ndarray) -> list[tuple[int, int, int, int]]:
    h, w = image_bgr.shape[:2]
    words = _ocr_data(image_bgr, y_offset=0)
    # bottom strip — many "god of mlbb" overlays sit here
    crop_y = int(h * 0.55)
    bottom = image_bgr[crop_y:, :]
    if bottom.size:
        words.extend(_ocr_data(bottom, y_offset=crop_y))

    all_boxes: list[tuple[int, int, int, int]] = []
    for phrase in load_phrases():
        phrase_boxes = _phrase_in_ocr(phrase, words)
        all_boxes.extend(phrase_boxes)
        if not phrase_boxes:
            all_boxes.extend(_regex_fallback_boxes(image_bgr, phrase))
    return _merge_boxes(all_boxes)


def remove_watermarks(image_bgr: np.ndarray) -> tuple[np.ndarray, bool]:
    boxes = find_watermark_boxes(image_bgr)
    if not boxes:
        return image_bgr, False
    h, w = image_bgr.shape[:2]
    mask = np.zeros((h, w), dtype=np.uint8)
    for x, y, bw, bh in boxes:
        x2 = min(w, x + bw)
        y2 = min(h, y + bh)
        mask[y:y2, x:x2] = 255
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    mask = cv2.dilate(mask, kernel, iterations=2)
    cleaned = cv2.inpaint(image_bgr, mask, 7, cv2.INPAINT_TELEA)
    return cleaned, True


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
