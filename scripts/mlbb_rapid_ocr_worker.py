#!/usr/bin/env python3
"""
Persistent RapidOCR worker — load the model once, OCR many images.

Protocol (stdin/stdout, one request per line):
  OCR <abs-path.png>
  -> OK <text...>
  -> ERR <message>

  PING
  -> PONG

  QUIT
  -> bye
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    try:
        import cv2
        from rapidocr_onnxruntime import RapidOCR
    except Exception as exc:
        print(f"ERR import:{exc}", flush=True)
        return 1

    try:
        ocr = RapidOCR()
    except Exception as exc:
        print(f"ERR init:{exc}", flush=True)
        return 1

    print("READY", flush=True)
    for line in sys.stdin:
        line = (line or "").strip()
        if not line:
            continue
        if line.upper() == "QUIT":
            print("bye", flush=True)
            return 0
        if line.upper() == "PING":
            print("PONG", flush=True)
            continue
        if not line.upper().startswith("OCR "):
            print("ERR bad_cmd", flush=True)
            continue
        path = Path(line[4:].strip())
        try:
            img = cv2.imread(str(path))
            if img is None:
                print("OK ", flush=True)
                continue
            result, _ = ocr(img)
            texts = [str(row[1]) for row in (result or []) if row and len(row) > 1]
            print("OK " + " ".join(texts), flush=True)
        except Exception as exc:
            print(f"ERR {exc}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
