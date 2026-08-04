#!/usr/bin/env python3
"""
Read MLBB kill-banner text (TRIPLE / MANIAC / SAVAGE / HAS SLAIN / …).

YouTube gold outline glyphs need:
  - a warm RapidOCR worker (cold start was ~6–8s/image → "OCR is blind" myth
    was mostly "we only tried one noisy crop and gave up")
  - gold/white isolation + upscale variants
  - a tight center ROI (streak text), not the whole HUD with KDA/clock junk
  - fuzzy aliases for HASSLAIN / MEGAKILL / common misreads
"""

from __future__ import annotations

import atexit
import difflib
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from functools import lru_cache
from pathlib import Path

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
    ("MEGA KILL", 3, "triple"),
    ("DOUBLE KILL", 2, "double"),
    ("UNSTOPPABLE", 2, "double"),
    ("DOMINATING", 2, "double"),
    ("KILLING SPREE", 1, "single"),
    ("FIRST BLOOD", 1, "single"),
    ("SHUT DOWN", 1, "single"),
    ("RAMPAGE", 1, "single"),
    ("HAS BEEN SLAIN", 1, "single"),
    ("HAS SLAIN", 1, "single"),
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
    "TR1PLEKILL": "TRIPLEKILL",
    "DOUBLEKILL": "DOUBLEKILL",
    "DOUBLKILL": "DOUBLEKILL",
    "DOUBEKILL": "DOUBLEKILL",
    "D0UBLEKILL": "DOUBLEKILL",
    "MEGAKILL": "MEGAKILL",
    "MEGAKIL": "MEGAKILL",
    "MEGAK1LL": "MEGAKILL",
    "UNSTOPPABLE": "UNSTOPPABLE",
    "USTENE": "UNSTOPPABLE",
    "UNSTOPABLE": "UNSTOPPABLE",
    "LEGENDARY": "LEGENDARY",
    "LEGENDAR": "LEGENDARY",
    "FIRSTBLOOD": "FIRSTBLOOD",
    "SHUTDOWN": "SHUTDOWN",
    "KILLINGSPREE": "KILLINGSPREE",
    "HASSLAIN": "HASSLAIN",
    "HASLAIN": "HASSLAIN",
    "HASBEENSLAIN": "HASBEENSLAIN",
    "BEENSLAIN": "HASBEENSLAIN",
}

_WORKER_LOCK = threading.Lock()
_WORKER_PROC: subprocess.Popen | None = None
_WORKER_READY = False


def _letters(s: str) -> str:
    return re.sub(r"[^A-Z]", "", str(s or "").upper())


def _rapid_python() -> Path | None:
    env = os.environ.get("MLBB_RAPID_OCR_PYTHON", "").strip()
    if env and Path(env).is_file():
        return Path(env)
    repo = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml"))
    for cand in (
        repo / ".venv_ocr" / "bin" / "python",
        Path("/root/content_bot_ml/.venv_ocr/bin/python"),
    ):
        if cand.is_file():
            return cand
    return None


def _worker_script() -> Path | None:
    here = Path(__file__).resolve().parent
    for cand in (
        here / "mlbb_rapid_ocr_worker.py",
        Path("/root/content_bot_ml/scripts/mlbb_rapid_ocr_worker.py"),
        Path("/usr/local/bin/mlbb_rapid_ocr_worker.py"),
    ):
        if cand.is_file():
            return cand
    return None


@lru_cache(maxsize=1)
def rapid_ocr_available() -> bool:
    if os.environ.get("MLBB_BANNER_RAPID_OCR", "1") != "1":
        return False
    eng = os.environ.get("MLBB_BANNER_OCR_ENGINE", "auto").strip().lower()
    if eng in {"tess", "tesseract", "off", "0"}:
        return False
    return _rapid_python() is not None and _worker_script() is not None


def _stop_worker() -> None:
    global _WORKER_PROC, _WORKER_READY
    proc = _WORKER_PROC
    _WORKER_PROC = None
    _WORKER_READY = False
    if proc is None:
        return
    try:
        if proc.stdin:
            proc.stdin.write("QUIT\n")
            proc.stdin.flush()
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


atexit.register(_stop_worker)


def _ensure_worker() -> subprocess.Popen | None:
    """Keep one RapidOCR process warm across hundreds of banner probes."""
    global _WORKER_PROC, _WORKER_READY
    with _WORKER_LOCK:
        if _WORKER_PROC is not None and _WORKER_PROC.poll() is None and _WORKER_READY:
            return _WORKER_PROC
        _stop_worker()
        py = _rapid_python()
        script = _worker_script()
        if py is None or script is None:
            return None
        try:
            proc = subprocess.Popen(
                [str(py), "-u", str(script)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            log.warning("rapid OCR worker spawn failed: %s", exc)
            return None
        # Wait for READY (model load).
        deadline = time.monotonic() + float(
            os.environ.get("MLBB_RAPID_OCR_WARMUP_SEC", "45") or "45"
        )
        ready = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                err = ""
                try:
                    err = (proc.stderr.read() or "")[:300] if proc.stderr else ""
                except Exception:
                    pass
                log.warning("rapid OCR worker died during warmup: %s", err)
                return None
            assert proc.stdout is not None
            line = proc.stdout.readline()
            if not line:
                time.sleep(0.05)
                continue
            if line.strip().upper().startswith("READY"):
                ready = True
                break
            if line.strip().upper().startswith("ERR"):
                log.warning("rapid OCR worker warmup err: %s", line.strip())
                try:
                    proc.kill()
                except Exception:
                    pass
                return None
        if not ready:
            log.warning("rapid OCR worker warmup timeout")
            try:
                proc.kill()
            except Exception:
                pass
            return None
        _WORKER_PROC = proc
        _WORKER_READY = True
        log.info("rapid OCR worker ready pid=%s", proc.pid)
        return proc


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
    # Prefer the streak-bearing slice — full HUD dumps dilute SequenceMatcher.
    raw = _prefer_streak_slice(raw)
    if sum(ch.isalpha() for ch in raw) < 4:
        return None
    blob = _letters(raw)
    if len(blob) < 4:
        return None

    tokens = [_letters(t) for t in re.findall(r"[A-Za-z0-9]{3,}", raw)]
    tokens = [t for t in tokens if len(t) >= 3]
    for tok in list(tokens):
        alias = _OCR_ALIASES.get(tok)
        if alias and alias not in tokens:
            tokens.append(alias)
            blob = blob.replace(tok, alias) if tok in blob else (blob + alias)

    # Explicit HAS SLAIN / HASSLAIN — do not require "BEEN".
    if "HASSLAIN" in blob or "HASBEENSLAIN" in blob or "BEENSLAIN" in blob:
        return (0.99, "HAS SLAIN", 1, "single")
    if "MEGAKILL" in blob:
        return (0.95, "MEGA KILL", 3, "triple")

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


def _prefer_streak_slice(text: str) -> str:
    """Keep the part of OCR that looks like a banner phrase, drop KDA/clock soup."""
    raw = str(text or "")
    if not raw:
        return raw
    upper = raw.upper()
    keys = (
        "HAS SLAIN",
        "HASSLAIN",
        "BEEN SLAIN",
        "DOUBLE KILL",
        "TRIPLE KILL",
        "MANIAC",
        "SAVAGE",
        "MEGA KILL",
        "MEGAKILL",
        "MEGA KIL",
        "UNSTOPPABLE",
        "FIRST BLOOD",
        "SHUT DOWN",
        "KILLING SPREE",
        "LEGENDARY",
        "DOMINATING",
        "GODLIKE",
    )
    for key in keys:
        idx = upper.find(key)
        if idx >= 0:
            lo = max(0, idx - 8)
            hi = min(len(raw), idx + len(key) + 16)
            return raw[lo:hi]
    # Letter-only glued forms.
    blob = _letters(raw)
    for key in ("HASSLAIN", "DOUBLEKILL", "TRIPLEKILL", "MEGAKILL", "MANIAC", "SAVAGE"):
        idx = blob.find(key)
        if idx >= 0:
            return key
    return raw


def extract_banner_text_zone(frame, *, tight: bool = False):
    """Upper HUD strip where kill-streak announcements sit."""
    import cv2

    if frame is None:
        return None
    h, w = frame.shape[:2]
    if h < 40 or w < 80:
        return None
    if tight:
        # Streak text is centered; wide crop only adds KDA/clock/minimap OCR noise.
        y0, y1 = int(h * 0.04), int(h * 0.22)
        x0, x1 = int(w * 0.20), int(w * 0.80)
    else:
        y0, y1 = int(h * 0.02), int(h * 0.30)
        x0, x1 = int(w * 0.10), int(w * 0.90)
    zone = frame[y0:y1, x0:x1]
    if zone.size == 0:
        return None
    target_h = max(72, int(os.environ.get("MLBB_BANNER_OCR_TARGET_H", "120")))
    if zone.shape[0] < target_h:
        scale = target_h / float(zone.shape[0])
        zone = cv2.resize(
            zone,
            (max(8, int(zone.shape[1] * scale)), target_h),
            interpolation=cv2.INTER_CUBIC,
        )
    return zone


def _ocr_variants(zone) -> list[tuple[str, object]]:
    """
    Preprocess variants tuned on YT gold outline banners (8pbq HAS SLAIN).

    Order: cheap/high-signal first. Stop early when fuzzy hits.
    """
    import cv2
    import numpy as np

    variants: list[tuple[str, object]] = []
    if zone is None:
        return variants

    # Upscale — Tesseract/Rapid need ~3x for thin gold outlines.
    h, w = zone.shape[:2]
    scale = max(2, int(os.environ.get("MLBB_BANNER_OCR_UPSCALE", "3") or "3"))
    big = cv2.resize(zone, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)
    variants.append(("up", big))

    hsv = cv2.cvtColor(big, cv2.COLOR_BGR2HSV)
    gold = cv2.inRange(hsv, np.array([5, 40, 100]), np.array([45, 255, 255]))
    white = cv2.inRange(hsv, np.array([0, 0, 170]), np.array([180, 90, 255]))
    mask = cv2.bitwise_or(gold, white)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    mask = cv2.dilate(mask, np.ones((2, 2), np.uint8), iterations=1)

    if os.environ.get("MLBB_BANNER_OCR_GOLD_MASK", "1") == "1":
        # Black glyphs on white — best recall on HAS SLAIN (8pbq@577).
        bw = np.full(big.shape[:2], 255, dtype=np.uint8)
        bw[mask > 0] = 0
        variants.append(("mask_bw", cv2.cvtColor(bw, cv2.COLOR_GRAY2BGR)))
        # Gold kept on white background.
        on_white = np.full_like(big, 255)
        on_white[mask > 0] = big[mask > 0]
        variants.append(("mask_whitebg", on_white))
        # V-channel otsu of the upscaled zone.
        v = hsv[:, :, 2]
        _, vt = cv2.threshold(v, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(("v_otsu", cv2.cvtColor(vt, cv2.COLOR_GRAY2BGR)))
        gray = cv2.cvtColor(cv2.bitwise_and(big, big, mask=mask), cv2.COLOR_BGR2GRAY)
        adapt = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 5
        )
        variants.append(("adapt", cv2.cvtColor(adapt, cv2.COLOR_GRAY2BGR)))

    return variants


def _rapid_read_image(img) -> str:
    """OCR one BGR image via warm RapidOCR worker (fallback: one-shot)."""
    import cv2

    if img is None:
        return ""
    timeout = max(5, int(os.environ.get("MLBB_RAPID_OCR_TIMEOUT_SEC", "20") or "20"))
    tmp: str | None = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        if not cv2.imwrite(tmp, img):
            return ""
        use_worker = os.environ.get("MLBB_BANNER_OCR_WARM_WORKER", "1") == "1"
        if use_worker:
            proc = _ensure_worker()
            if proc is not None and proc.stdin and proc.stdout:
                with _WORKER_LOCK:
                    try:
                        proc.stdin.write(f"OCR {tmp}\n")
                        proc.stdin.flush()
                        # Bound wait without killing the warm process on slow frames.
                        deadline = time.monotonic() + timeout
                        line = ""
                        while time.monotonic() < deadline:
                            if proc.poll() is not None:
                                global _WORKER_PROC, _WORKER_READY
                                _WORKER_PROC = None
                                _WORKER_READY = False
                                break
                            # Non-blocking-ish: readline blocks; rely on timeout kill only
                            # for one-shot fallback. For warm path use select if available.
                            try:
                                import select

                                r, _, _ = select.select([proc.stdout], [], [], 0.25)
                                if not r:
                                    continue
                            except Exception:
                                pass
                            line = proc.stdout.readline()
                            if line:
                                break
                        if line.upper().startswith("OK"):
                            return " ".join(line[2:].split())
                        if line.upper().startswith("ERR"):
                            log.debug("rapid worker err: %s", line.strip()[:160])
                    except Exception as exc:
                        log.debug("rapid worker IO failed: %s", exc)
                        _stop_worker()

        # One-shot fallback (cold) — still better than empty.
        py = _rapid_python()
        if py is None:
            return ""
        worker = _worker_script()
        # Inline minimal one-shot to avoid depending on worker READY path.
        cold = r"""
import sys
from pathlib import Path
import cv2
from rapidocr_onnxruntime import RapidOCR
img = cv2.imread(sys.argv[1])
if img is None:
    raise SystemExit(0)
ocr = RapidOCR()
result, _ = ocr(img)
print(" ".join(str(row[1]) for row in (result or []) if row and len(row) > 1))
"""
        proc2 = subprocess.run(
            [str(py), "-c", cold, tmp],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if proc2.returncode != 0:
            return ""
        return " ".join((proc2.stdout or "").split())
    except Exception as exc:
        log.debug("rapid OCR failed: %s", exc)
        return ""
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _rapid_read_zone(zone) -> str:
    if zone is None:
        return ""
    texts: list[str] = []
    max_variants = max(1, int(os.environ.get("MLBB_BANNER_OCR_MAX_VARIANTS", "4") or "4"))
    for name, img in _ocr_variants(zone)[:max_variants]:
        chunk = _rapid_read_image(img)
        if chunk:
            texts.append(chunk)
            if fuzzy_match_banner_label(chunk) is not None:
                log.debug("rapid OCR hit via variant=%s text=%s", name, chunk[:60])
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
    parts: list[str] = []
    # Tight center crop first — less KDA soup, more streak glyphs.
    zones = [
        extract_banner_text_zone(frame, tight=True),
        extract_banner_text_zone(frame, tight=False),
    ]
    seen = set()
    for zone in zones:
        if zone is None:
            continue
        key = (zone.shape[0], zone.shape[1], int(zone.mean()))
        if key in seen:
            continue
        seen.add(key)
        if prefer_rapid and rapid_ocr_available():
            chunk = _rapid_read_zone(zone)
            if chunk:
                parts.append(chunk)
                if fuzzy_match_banner_label(chunk) is not None:
                    break
    if not any(fuzzy_match_banner_label(p) for p in parts):
        for zone in zones:
            if zone is None:
                continue
            tess = _tesseract_read_zone(zone)
            if tess:
                parts.append(tess)
                if fuzzy_match_banner_label(tess) is not None:
                    break
    return " ".join(p for p in parts if p).strip()


def ocr_engine_ready() -> bool:
    return rapid_ocr_available()
