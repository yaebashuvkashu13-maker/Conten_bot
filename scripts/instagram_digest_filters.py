#!/usr/bin/env python3
"""Filter ads and build RU summaries for Instagram digest."""

from __future__ import annotations

import logging
import re
import tempfile
import urllib.request
from pathlib import Path

import cv2
import numpy as np

AD_EXAMPLES_DIR = Path("/root/data/mlbb/ad_examples")
REJECT_EXAMPLES_DIR = Path("/root/data/mlbb/reject_examples")

_AD_HISTS: list[np.ndarray] | None = None
_AD_MTIME: float = 0.0

AD_CAPTION_RE = re.compile(
    r"("
    r"#ad\b|sponsored|giveaway|promo\b|"
    r"gift\s+starlight|starlight\s+card|order\s+gift|"
    r"harga\s+terbaik|buruan\s+order|stoknya\s+terbatas|"
    r"whatsapp|wa\.me|daftar\s+member|"
    r"agstore|top\s*up|topup|codashop|duniagames|unipin|"
    r"jual\s+skin|murah\s+cuma|diskon\s+|"
    r"website\s*:|\.com\b|\.shop\b|"
    r"free\s+diamond|skin\s+gratis|click\s+link"
    r")",
    re.I,
)

GAMEPLAY_HINT_RE = re.compile(
    r"(patch|nerf|buff|revamp|hero|skin\s+preview|leak|update|meta|build|"
    r"emblem|item|event|allstar|tide|savage|maniac|gameplay|tips)",
    re.I,
)


def _examples_mtime(folder: Path) -> float:
    if not folder.exists():
        return 0.0
    latest = 0.0
    for path in folder.glob("*"):
        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
            latest = max(latest, path.stat().st_mtime)
    return latest


def _image_hist(image_bgr: np.ndarray) -> np.ndarray:
    small = cv2.resize(image_bgr, (320, 180))
    band = small[int(180 * 0.12) : int(180 * 0.88), int(320 * 0.05) : int(320 * 0.95)]
    hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [24, 16], [0, 180, 0, 256])
    cv2.normalize(hist, hist)
    return hist.astype(np.float32)


def _load_ad_histograms() -> list[np.ndarray]:
    global _AD_HISTS, _AD_MTIME
    mtime = max(_examples_mtime(AD_EXAMPLES_DIR), _examples_mtime(REJECT_EXAMPLES_DIR))
    if _AD_HISTS is not None and mtime == _AD_MTIME:
        return _AD_HISTS
    hists: list[np.ndarray] = []
    for folder in (AD_EXAMPLES_DIR, REJECT_EXAMPLES_DIR):
        if not folder.exists():
            continue
        for path in sorted(folder.iterdir()):
            if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                continue
            img = cv2.imread(str(path))
            if img is not None:
                hists.append(_image_hist(img))
    _AD_HISTS = hists
    _AD_MTIME = mtime
    return hists


def _download_image(url: str) -> np.ndarray | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = resp.read()
        arr = np.frombuffer(data, dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception:
        return None


def image_matches_ad_example(thumbnail_url: str | None, threshold: float = 0.80) -> float:
    if not thumbnail_url:
        return 0.0
    refs = _load_ad_histograms()
    if not refs:
        return 0.0
    frame = _download_image(thumbnail_url)
    if frame is None:
        return 0.0
    hist = _image_hist(frame)
    best = 0.0
    for ref in refs:
        best = max(best, float(cv2.compareHist(hist, ref, cv2.HISTCMP_CORREL)))
    return best


def caption_looks_like_ad(caption: str) -> str | None:
    text = (caption or "").strip()
    if not text:
        return None
    if AD_CAPTION_RE.search(text):
        # allow leak/patch posts that mention shop in passing — rare
        if GAMEPLAY_HINT_RE.search(text) and text.lower().count(".com") <= 1:
            if "agstore" not in text.lower() and "order gift" not in text.lower():
                return None
        return "ad_keywords"
    # store CTA without game context
    lower = text.lower()
    if any(x in lower for x in ("whatsapp", "wa.me", "agstore", "order gift", "buruan order")):
        return "store_cta"
    return None


def is_ad_post(caption: str, thumbnail_url: str | None) -> tuple[bool, str]:
    reason = caption_looks_like_ad(caption)
    if reason:
        return True, reason
    sim = image_matches_ad_example(thumbnail_url)
    if sim >= 0.80:
        return True, f"ad_image_sim={sim:.2f}"
    return False, "ok"


def _translate_ru(text: str) -> str:
    text = text.strip()
    if not text:
        return "Пост без подписи."
    cyr = sum(1 for ch in text if "\u0400" <= ch <= "\u04FF")
    if cyr >= max(len(text) * 0.25, 12):
        return text[:380]
    try:
        from deep_translator import GoogleTranslator

        chunks: list[str] = []
        chunk_size = 450
        for i in range(0, min(len(text), 900), chunk_size):
            part = text[i : i + chunk_size]
            chunks.append(GoogleTranslator(source="auto", target="ru").translate(part))
        return " ".join(chunks).strip()[:380]
    except Exception as exc:
        logging.debug("translate failed: %s", exc)
    return text[:380]


def summarize_ru(caption: str, source_name: str) -> str:
    raw = (caption or "").strip()
    translated = _translate_ru(raw)
    # Short digest line
    tags = re.findall(r"#(\w+)", raw)[:6]
    tag_line = " ".join(f"#{t}" for t in tags[:5]) if tags else ""
    lines = [
        f"Кратко ({source_name}):",
        translated,
    ]
    if tag_line:
        lines.append(tag_line)
    body = "\n".join(lines)
    return body[:900]


def build_telegram_caption(source_name: str, post: dict) -> str:
    summary = summarize_ru(post.get("caption", ""), source_name)
    link = post.get("permalink", "")
    return f"📌 {source_name}\n\n{summary}\n\n🔗 {link}"[:1024]
